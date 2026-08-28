"""
TimeoutManager — 统一管理超时、重试、熔断和升级

- 默认策略: 重试 3 次 + 指数退避
- 超时默认动作: 转 ESCALATE
- 覆盖场景: Mock 动作卡住、人工审批超时（10 分钟）、工具调用超时
"""

import time
import asyncio
from typing import Any, Callable, Optional, Dict, Type
from dataclasses import dataclass, field
from enum import Enum
from loguru import logger

from app.models.incident import (
    RetryPolicy,
    CircuitBreakerConfig,
    TimeoutConfig,
)


class CircuitState(str, Enum):
    """熔断器状态"""
    CLOSED = "CLOSED"         # 正常
    OPEN = "OPEN"             # 熔断
    HALF_OPEN = "HALF_OPEN"   # 半开


@dataclass
class ExecutionResult:
    """执行结果"""
    success: bool
    result: Any = None
    error: Optional[Exception] = None
    retry_count: int = 0
    total_duration_ms: float = 0.0
    escalated: bool = False
    final_action: str = ""  # "success" / "escalate" / "timeout" / "circuit_open"


class CircuitBreaker:
    """熔断器"""

    def __init__(self, name: str, config: CircuitBreakerConfig):
        self.name = name
        self.config = config
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: float = 0.0
        self.half_open_successes = 0

    def record_success(self) -> None:
        if self.state == CircuitState.HALF_OPEN:
            self.half_open_successes += 1
            if self.half_open_successes >= self.config.half_open_max_requests:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                logger.info(f"[CircuitBreaker:{self.name}] 半开→闭合（恢复）")
        else:
            self.failure_count = 0

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.monotonic()

        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            logger.warning(f"[CircuitBreaker:{self.name}] 半开失败，重新熔断")
        elif self.failure_count >= self.config.failure_threshold:
            self.state = CircuitState.OPEN
            logger.warning(
                f"[CircuitBreaker:{self.name}] 熔断触发: "
                f"{self.failure_count} 次失败 >= 阈值 {self.config.failure_threshold}"
            )

    def allow_request(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            elapsed = time.monotonic() - self.last_failure_time
            if elapsed >= self.config.recovery_timeout_seconds:
                self.state = CircuitState.HALF_OPEN
                self.half_open_successes = 0
                logger.info(f"[CircuitBreaker:{self.name}] 熔断→半开（尝试恢复）")
                return True
            return False
        # HALF_OPEN
        return True


class TimeoutManager:
    """
    超时管理器。

    功能：
    - 带超时的函数执行
    - 自动重试 + 指数退避
    - 熔断器
    - 超时/失败自动升级
    """

    def __init__(self, config: Optional[TimeoutConfig] = None):
        self.config = config or TimeoutConfig()
        # 每个操作一个熔断器
        self._breakers: Dict[str, CircuitBreaker] = {}

    # ================================================================
    # 公共接口
    # ================================================================

    async def execute_with_timeout(
        self,
        coro_func: Callable[..., Any],
        operation_name: str,
        timeout_seconds: Optional[float] = None,
        args: tuple = (),
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> ExecutionResult:
        """
        带超时、重试、熔断的执行。

        Args:
            coro_func: 异步可调用对象
            operation_name: 操作名称（用于日志和熔断器标识）
            timeout_seconds: 超时秒数（None 则使用默认值）
            args: 位置参数
            kwargs: 关键字参数

        Returns:
            ExecutionResult
        """
        kwargs = kwargs or {}
        timeout = timeout_seconds or self.config.default_timeout_seconds
        policy = self.config.retry_policy
        breaker = self._get_breaker(operation_name)

        start_time = time.monotonic()
        last_error: Optional[Exception] = None

        for attempt in range(policy.max_retries + 1):
            # 检查熔断器
            if not breaker.allow_request():
                logger.warning(
                    f"[TimeoutManager] 熔断器阻止: {operation_name}"
                )
                return ExecutionResult(
                    success=False,
                    error=Exception(f"Circuit breaker OPEN for {operation_name}"),
                    retry_count=attempt,
                    total_duration_ms=(time.monotonic() - start_time) * 1000,
                    escalated=True,
                    final_action="circuit_open",
                )

            try:
                result = await asyncio.wait_for(
                    coro_func(*args, **kwargs),
                    timeout=timeout,
                )
                breaker.record_success()

                # 如果返回的是 MockActionResult 类型的对象，检查 success 字段
                if hasattr(result, 'success') and not result.success:
                    raise RuntimeError(f"Mock action failed: {getattr(result, 'message', 'unknown')}")

                return ExecutionResult(
                    success=True,
                    result=result,
                    retry_count=attempt,
                    total_duration_ms=(time.monotonic() - start_time) * 1000,
                    final_action="success",
                )

            except asyncio.TimeoutError as e:
                last_error = e
                logger.warning(
                    f"[TimeoutManager] {operation_name} 超时 "
                    f"(attempt {attempt + 1}/{policy.max_retries + 1})"
                )
                breaker.record_failure()
                if attempt < policy.max_retries:
                    delay = self._backoff_delay(attempt, policy)
                    logger.info(f"[TimeoutManager] {delay:.1f}s 后重试...")
                    await asyncio.sleep(delay)

            except Exception as e:
                last_error = e
                logger.warning(
                    f"[TimeoutManager] {operation_name} 失败: {e} "
                    f"(attempt {attempt + 1}/{policy.max_retries + 1})"
                )
                breaker.record_failure()
                if attempt < policy.max_retries:
                    delay = self._backoff_delay(attempt, policy)
                    logger.info(f"[TimeoutManager] {delay:.1f}s 后重试...")
                    await asyncio.sleep(delay)

        # 所有重试耗尽
        logger.error(
            f"[TimeoutManager] {operation_name} 全部重试失败，升级为 ESCALATE"
        )
        return ExecutionResult(
            success=False,
            error=last_error,
            retry_count=policy.max_retries,
            total_duration_ms=(time.monotonic() - start_time) * 1000,
            escalated=True,
            final_action="escalate",
        )

    def execute_sync_with_timeout(
        self,
        func: Callable[..., Any],
        operation_name: str,
        timeout_seconds: Optional[float] = None,
        args: tuple = (),
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> ExecutionResult:
        """
        同步版本（用于非异步 Mock 动作）。
        """
        kwargs = kwargs or {}
        timeout = timeout_seconds or self.config.default_timeout_seconds
        policy = self.config.retry_policy
        breaker = self._get_breaker(operation_name)

        start_time = time.monotonic()
        last_error: Optional[Exception] = None

        for attempt in range(policy.max_retries + 1):
            if not breaker.allow_request():
                return ExecutionResult(
                    success=False,
                    error=Exception(f"Circuit breaker OPEN for {operation_name}"),
                    retry_count=attempt,
                    total_duration_ms=(time.monotonic() - start_time) * 1000,
                    escalated=True,
                    final_action="circuit_open",
                )

            try:
                # 简单超时模拟（同步函数无法真正中断，用信号量近似）
                result = func(*args, **kwargs)
                breaker.record_success()

                if hasattr(result, 'success') and not result.success:
                    raise RuntimeError(f"Mock action failed: {getattr(result, 'message', 'unknown')}")

                return ExecutionResult(
                    success=True,
                    result=result,
                    retry_count=attempt,
                    total_duration_ms=(time.monotonic() - start_time) * 1000,
                    final_action="success",
                )

            except Exception as e:
                last_error = e
                logger.warning(
                    f"[TimeoutManager] {operation_name} 失败: {e} "
                    f"(attempt {attempt + 1}/{policy.max_retries + 1})"
                )
                breaker.record_failure()
                if attempt < policy.max_retries:
                    delay = self._backoff_delay(attempt, policy)
                    time.sleep(delay)

        logger.error(f"[TimeoutManager] {operation_name} 全部重试失败，升级为 ESCALATE")
        return ExecutionResult(
            success=False,
            error=last_error,
            retry_count=policy.max_retries,
            total_duration_ms=(time.monotonic() - start_time) * 1000,
            escalated=True,
            final_action="escalate",
        )

    async def await_approval(
        self,
        approval_id: str,
        timeout_minutes: Optional[int] = None,
    ) -> ExecutionResult:
        """
        等待人工审批，超时自动转 ESCALATE。

        Args:
            approval_id: 审批请求 ID
            timeout_minutes: 超时分钟数（默认 10 分钟）

        Returns:
            ExecutionResult
        """
        timeout = (timeout_minutes or self.config.approval_timeout_minutes) * 60
        logger.info(
            f"[TimeoutManager] 等待审批: {approval_id}, timeout={timeout / 60:.0f}min"
        )

        start_time = time.monotonic()
        try:
            # 模拟等待审批（真实环境会用 SSE 回调或消息队列）
            await asyncio.sleep(min(timeout, 600))  # 实际场景由事件驱动

            elapsed = time.monotonic() - start_time
            if elapsed >= timeout:
                logger.warning(
                    f"[TimeoutManager] 审批超时: {approval_id}, 转 ESCALATE"
                )
                return ExecutionResult(
                    success=False,
                    error=TimeoutError(f"Approval timeout: {approval_id}"),
                    total_duration_ms=elapsed * 1000,
                    escalated=True,
                    final_action="escalate",
                )

            return ExecutionResult(
                success=True,
                total_duration_ms=elapsed * 1000,
                final_action="success",
            )

        except Exception as e:
            elapsed = time.monotonic() - start_time
            return ExecutionResult(
                success=False,
                error=e,
                total_duration_ms=elapsed * 1000,
                escalated=True,
                final_action="escalate",
            )

    # ================================================================
    # 辅助方法
    # ================================================================

    def _get_breaker(self, name: str) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(name, self.config.circuit_breaker)
        return self._breakers[name]

    @staticmethod
    def _backoff_delay(attempt: int, policy: RetryPolicy) -> float:
        """计算指数退避延迟"""
        delay = policy.base_delay_seconds * (policy.backoff_multiplier ** attempt)
        return min(delay, policy.max_delay_seconds)

    def breaker_state(self, name: str) -> CircuitState:
        """查询熔断器状态"""
        breaker = self._breakers.get(name)
        return breaker.state if breaker else CircuitState.CLOSED

    def reset_breaker(self, name: str) -> None:
        """手动重置熔断器"""
        if name in self._breakers:
            self._breakers[name] = CircuitBreaker(name, self.config.circuit_breaker)
            logger.info(f"[TimeoutManager] 熔断器已重置: {name}")
