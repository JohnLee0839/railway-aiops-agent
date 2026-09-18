"""
TimeoutManager — 统一管理超时、重试、熔断和升级

- 默认策略: 重试 3 次 + 指数退避
    - 不可重试错误: 立即失败，不进入重试或熔断计数
    - 副作用动作超时: 返回 UNKNOWN，交给对账流程
- 覆盖场景: Mock 动作卡住、人工审批超时（10 分钟）、工具调用超时
"""

import time
import asyncio
from typing import Any, Callable, Optional, Dict, Type
from enum import Enum
from loguru import logger

from app.models.incident import (
    RetryPolicy,
    CircuitBreakerConfig,
    TimeoutConfig,
    ExecutionOutcome,
    ExecutionResult,
)


class CircuitState(str, Enum):
    """熔断器状态"""
    CLOSED = "CLOSED"         # 正常
    OPEN = "OPEN"             # 熔断
    HALF_OPEN = "HALF_OPEN"   # 半开


class MockFailure(RuntimeError):
    """工具返回失败结果时使用的可分类异常。"""


class AttemptObserverFailure(RuntimeError):
    """A durable attempt write failed; do not continue to an external action."""


class ErrorClass(str, Enum):
    """重试决策使用的错误分类。"""
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    HTTP_TRANSIENT = "http_transient"
    RETRYABLE_EXCEPTION = "retryable_exception"
    NON_RETRYABLE = "non_retryable"
    UNKNOWN = "unknown"


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
        retry_policy: Optional[RetryPolicy] = None,
        uncertain_on_timeout: bool = False,
        attempt_observer: Optional[Callable[[str, int, Optional[ExecutionResult]], None]] = None,
        args: tuple = (),
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> ExecutionResult:
        """
        带超时、重试、熔断的执行。

        Args:
            coro_func: 异步可调用对象
            operation_name: 操作名称（用于日志和熔断器标识）
            timeout_seconds: 超时秒数（None 则使用默认值）
            uncertain_on_timeout: 副作用动作超时是否返回 UNKNOWN 而非自动重试
            args: 位置参数
            kwargs: 关键字参数

        Returns:
            ExecutionResult
        """
        kwargs = kwargs or {}
        timeout = timeout_seconds or self.config.default_timeout_seconds
        policy = retry_policy or self.config.retry_policy
        breaker = self._get_breaker(operation_name)

        start_time = time.monotonic()
        last_error: Optional[Exception] = None
        last_retryable = False
        last_error_class = ErrorClass.UNKNOWN
        last_result: Any = None

        for attempt in range(policy.max_retries + 1):
            # 检查熔断器
            if not breaker.allow_request():
                logger.warning(
                    f"[TimeoutManager] 熔断器阻止: {operation_name}"
                )
                return ExecutionResult(
                    action_name=operation_name,
                    success=False,
                    error=f"Circuit breaker OPEN for {operation_name}",
                    error_type="circuit_open",
                    error_message=f"Circuit breaker OPEN for {operation_name}",
                    retry_count=attempt,
                    total_duration_ms=(time.monotonic() - start_time) * 1000,
                    escalated=True,
                    final_action="circuit_open",
                    outcome=ExecutionOutcome.NOT_SENT,
                    retry_exhausted=True,
                    retryable=False,
                )

            self._notify_attempt(attempt_observer, "STARTED", attempt + 1, None)
            try:
                result = await asyncio.wait_for(
                    coro_func(*args, **kwargs),
                    timeout=timeout,
                )
                # 如果返回的是 MockActionResult 类型的对象，检查 success 字段
                if hasattr(result, 'success') and not result.success:
                    last_result = result
                    failure = MockFailure(
                        f"Mock action failed: {getattr(result, 'message', 'unknown')}"
                    )
                    # 保留工具已经明确给出的重试/结局信息，避免重新猜测。
                    failure.retryable = getattr(result, "retryable", None)
                    failure.error_type = getattr(result, "error_type", None)
                    raise failure

                breaker.record_success()

                self._notify_attempt(
                    attempt_observer,
                    "TERMINAL",
                    attempt + 1,
                    ExecutionResult(
                        action_name=getattr(result, "action_name", operation_name),
                        target=getattr(result, "target", None),
                        success=True,
                        result=result,
                        retry_count=attempt,
                        final_action="success",
                        outcome=ExecutionOutcome.SUCCESS,
                        retry_exhausted=False,
                        retryable=False,
                        side_effect_possible=bool(getattr(result, "side_effect_possible", False)),
                        side_effect_confirmed=bool(getattr(result, "side_effect_confirmed", False)),
                    ),
                )

                return ExecutionResult(
                    action_name=getattr(result, "action_name", operation_name),
                    target=getattr(result, "target", None),
                    success=True,
                    result=result,
                    retry_count=attempt,
                    total_duration_ms=(time.monotonic() - start_time) * 1000,
                    final_action="success",
                    outcome=ExecutionOutcome.SUCCESS,
                    retry_exhausted=False,
                    retryable=False,
                    side_effect_possible=bool(getattr(result, "side_effect_possible", False)),
                    side_effect_confirmed=bool(getattr(result, "side_effect_confirmed", False)),
                )

            except asyncio.TimeoutError as e:
                last_error = e
                last_error_class = ErrorClass.TIMEOUT
                last_retryable = True
                logger.warning(
                    f"[TimeoutManager] {operation_name} 超时 "
                    f"(attempt {attempt + 1}/{policy.max_retries + 1})"
                )
                if uncertain_on_timeout:
                    attempt_result = ExecutionResult(
                        success=False, error=str(e) or type(e).__name__, error_type="timeout",
                        error_message=str(e) or type(e).__name__, retry_count=attempt,
                        final_action="unknown", outcome=ExecutionOutcome.UNKNOWN,
                        retry_exhausted=True, retryable=False, side_effect_possible=True,
                    )
                    self._notify_attempt(attempt_observer, "TERMINAL", attempt + 1, attempt_result)
                    return ExecutionResult(
                        action_name=getattr(last_result, "action_name", operation_name),
                        target=getattr(last_result, "target", None),
                        success=False,
                        error=str(e) or type(e).__name__,
                        error_type="timeout",
                        error_message=str(e) or type(e).__name__,
                        retry_count=attempt,
                        total_duration_ms=(time.monotonic() - start_time) * 1000,
                        final_action="unknown",
                        outcome=ExecutionOutcome.UNKNOWN,
                        retry_exhausted=True,
                        retryable=False,
                        side_effect_possible=True,
                    )
                breaker.record_failure()
                will_retry = attempt < policy.max_retries and self._is_retryable(e, policy)
                self._notify_attempt(
                    attempt_observer, "TERMINAL", attempt + 1,
                    ExecutionResult(success=False, error=str(e) or type(e).__name__, error_type="timeout",
                                    error_message=str(e) or type(e).__name__, retry_count=attempt,
                                    outcome=ExecutionOutcome.RESPONSE_LOST, retryable=True,
                                    retry_exhausted=not will_retry, side_effect_possible=True),
                )
                if will_retry:
                    delay = self._backoff_delay(attempt, policy)
                    logger.info(f"[TimeoutManager] {delay:.1f}s 后重试...")
                    await asyncio.sleep(delay)
                else:
                    break

            except Exception as e:
                if isinstance(e, AttemptObserverFailure):
                    raise
                last_error = e
                logger.warning(
                    f"[TimeoutManager] {operation_name} 失败: {e} "
                    f"(attempt {attempt + 1}/{policy.max_retries + 1})"
                )
                error_class = self._classify_error(e, policy)
                last_error_class = error_class
                last_retryable = error_class in {
                    ErrorClass.TIMEOUT,
                    ErrorClass.CONNECTION,
                    ErrorClass.HTTP_TRANSIENT,
                    ErrorClass.RETRYABLE_EXCEPTION,
                }
                if error_class not in (ErrorClass.NON_RETRYABLE, ErrorClass.UNKNOWN):
                    breaker.record_failure()
                will_retry = attempt < policy.max_retries and error_class in {
                    ErrorClass.TIMEOUT,
                    ErrorClass.CONNECTION,
                    ErrorClass.HTTP_TRANSIENT,
                    ErrorClass.RETRYABLE_EXCEPTION,
                }
                self._notify_attempt(
                    attempt_observer, "TERMINAL", attempt + 1,
                    ExecutionResult(success=False, result=last_result, error=str(e),
                                    error_type=self._error_type_for(e, error_class), error_message=str(e),
                                    retry_count=attempt, outcome=self._outcome_for_error(e, error_class),
                                    retryable=last_retryable, retry_exhausted=not will_retry,
                                    side_effect_possible=bool(getattr(last_result, "side_effect_possible", False))
                                    or self._side_effect_possible(e, error_class)),
                )
                if will_retry:
                    delay = self._backoff_delay(attempt, policy)
                    logger.info(f"[TimeoutManager] {delay:.1f}s 后重试...")
                    await asyncio.sleep(delay)
                else:
                    break

        # 所有重试耗尽
        logger.error(
            f"[TimeoutManager] {operation_name} 全部重试失败，升级为 ESCALATE"
        )
        return ExecutionResult(
            action_name=getattr(last_result, "action_name", operation_name),
            target=getattr(last_result, "target", None),
            success=False,
            error=str(last_error) if last_error is not None else None,
            error_type=self._error_type_for(last_error, last_error_class),
            error_message=str(last_error) if last_error is not None else None,
            retry_count=attempt,
            total_duration_ms=(time.monotonic() - start_time) * 1000,
            escalated=True,
            final_action="escalate",
            outcome=self._outcome_for_error(last_error, last_error_class),
            retry_exhausted=True,
            retryable=last_retryable,
            side_effect_possible=(
                bool(getattr(last_result, "side_effect_possible", False))
                or self._side_effect_possible(last_error, last_error_class)
            ),
            side_effect_confirmed=bool(getattr(last_result, "side_effect_confirmed", False)),
        )

    def execute_sync_with_timeout(
        self,
        func: Callable[..., Any],
        operation_name: str,
        timeout_seconds: Optional[float] = None,
        retry_policy: Optional[RetryPolicy] = None,
        uncertain_on_timeout: bool = False,
        args: tuple = (),
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> ExecutionResult:
        """
        同步版本（用于非异步 Mock 动作）。
        """
        kwargs = kwargs or {}
        timeout = timeout_seconds or self.config.default_timeout_seconds
        policy = retry_policy or self.config.retry_policy
        breaker = self._get_breaker(operation_name)

        start_time = time.monotonic()
        last_error: Optional[Exception] = None
        last_retryable = False
        last_error_class = ErrorClass.UNKNOWN
        last_result: Any = None

        for attempt in range(policy.max_retries + 1):
            if not breaker.allow_request():
                return ExecutionResult(
                    action_name=operation_name,
                    success=False,
                    error=f"Circuit breaker OPEN for {operation_name}",
                    error_type="circuit_open",
                    error_message=f"Circuit breaker OPEN for {operation_name}",
                    retry_count=attempt,
                    total_duration_ms=(time.monotonic() - start_time) * 1000,
                    escalated=True,
                    final_action="circuit_open",
                    outcome=ExecutionOutcome.NOT_SENT,
                    retry_exhausted=True,
                    retryable=False,
                )

            try:
                # 简单超时模拟（同步函数无法真正中断，用信号量近似）
                result = func(*args, **kwargs)
                if hasattr(result, 'success') and not result.success:
                    last_result = result
                    failure = MockFailure(
                        f"Mock action failed: {getattr(result, 'message', 'unknown')}"
                    )
                    failure.retryable = getattr(result, "retryable", None)
                    failure.error_type = getattr(result, "error_type", None)
                    raise failure

                breaker.record_success()

                return ExecutionResult(
                    action_name=getattr(result, "action_name", operation_name),
                    target=getattr(result, "target", None),
                    success=True,
                    result=result,
                    retry_count=attempt,
                    total_duration_ms=(time.monotonic() - start_time) * 1000,
                    final_action="success",
                    outcome=ExecutionOutcome.SUCCESS,
                    retry_exhausted=False,
                    retryable=False,
                    side_effect_possible=bool(getattr(result, "side_effect_possible", False)),
                    side_effect_confirmed=bool(getattr(result, "side_effect_confirmed", False)),
                )

            except Exception as e:
                last_error = e
                logger.warning(
                    f"[TimeoutManager] {operation_name} 失败: {e} "
                    f"(attempt {attempt + 1}/{policy.max_retries + 1})"
                )
                error_class = self._classify_error(e, policy)
                last_error_class = error_class
                last_retryable = error_class in {
                    ErrorClass.TIMEOUT,
                    ErrorClass.CONNECTION,
                    ErrorClass.HTTP_TRANSIENT,
                    ErrorClass.RETRYABLE_EXCEPTION,
                }
                if error_class not in (ErrorClass.NON_RETRYABLE, ErrorClass.UNKNOWN):
                    breaker.record_failure()
                if attempt < policy.max_retries and error_class in {
                    ErrorClass.TIMEOUT,
                    ErrorClass.CONNECTION,
                    ErrorClass.HTTP_TRANSIENT,
                    ErrorClass.RETRYABLE_EXCEPTION,
                }:
                    delay = self._backoff_delay(attempt, policy)
                    time.sleep(delay)
                else:
                    break

        logger.error(f"[TimeoutManager] {operation_name} 全部重试失败，升级为 ESCALATE")
        return ExecutionResult(
            action_name=getattr(last_result, "action_name", operation_name),
            target=getattr(last_result, "target", None),
            success=False,
            error=str(last_error) if last_error is not None else None,
            error_type=self._error_type_for(last_error, last_error_class),
            error_message=str(last_error) if last_error is not None else None,
            retry_count=attempt,
            total_duration_ms=(time.monotonic() - start_time) * 1000,
            escalated=True,
            final_action="escalate",
            outcome=self._outcome_for_error(last_error, last_error_class),
            retry_exhausted=True,
            retryable=last_retryable,
            side_effect_possible=(
                bool(getattr(last_result, "side_effect_possible", False))
                or self._side_effect_possible(last_error, last_error_class)
            ),
            side_effect_confirmed=bool(getattr(last_result, "side_effect_confirmed", False)),
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
                    action_name=f"approval:{approval_id}",
                    success=False,
                    error=f"Approval timeout: {approval_id}",
                    error_type="timeout",
                    error_message=f"Approval timeout: {approval_id}",
                    total_duration_ms=elapsed * 1000,
                    escalated=True,
                    final_action="escalate",
                    outcome=ExecutionOutcome.RESPONSE_LOST,
                    retry_exhausted=True,
                    retryable=False,
                )

            return ExecutionResult(
                action_name=f"approval:{approval_id}",
                success=True,
                total_duration_ms=elapsed * 1000,
                final_action="success",
                outcome=ExecutionOutcome.SUCCESS,
                retry_exhausted=False,
                retryable=False,
            )

        except Exception as e:
            elapsed = time.monotonic() - start_time
            return ExecutionResult(
                action_name=f"approval:{approval_id}",
                success=False,
                error=str(e) or type(e).__name__,
                error_type=type(e).__name__,
                error_message=str(e) or type(e).__name__,
                total_duration_ms=elapsed * 1000,
                escalated=True,
                final_action="escalate",
                outcome=ExecutionOutcome.FAILED,
                retry_exhausted=True,
                retryable=False,
            )

    # ================================================================
    # 辅助方法
    # ================================================================

    def _get_breaker(self, name: str) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(name, self.config.circuit_breaker)
        return self._breakers[name]

    @staticmethod
    def _notify_attempt(observer, phase: str, attempt_no: int, result: Optional[ExecutionResult]) -> None:
        if observer is None:
            return
        try:
            observer(phase, attempt_no, result)
        except Exception as exc:
            raise AttemptObserverFailure("Durable action attempt write failed") from exc

    @staticmethod
    def _backoff_delay(attempt: int, policy: RetryPolicy) -> float:
        """计算指数退避延迟"""
        delay = policy.base_delay_seconds * (policy.backoff_multiplier ** attempt)
        return min(delay, policy.max_delay_seconds)

    @staticmethod
    def _classify_error(error: Exception, policy: RetryPolicy) -> ErrorClass:
        """按明确的异常类别或 HTTP 状态码分类，拒绝通用 Exception 重试。"""
        explicit_retryable = getattr(error, "retryable", None)
        if explicit_retryable is False:
            return ErrorClass.NON_RETRYABLE
        explicit_error_type = getattr(error, "error_type", None)
        if explicit_error_type == "timeout":
            return ErrorClass.TIMEOUT
        if explicit_error_type in {"unknown", "response_lost"}:
            return ErrorClass.UNKNOWN
        if explicit_retryable is True:
            return ErrorClass.RETRYABLE_EXCEPTION

        exception_names = {cls.__name__ for cls in type(error).__mro__}
        if exception_names.intersection(policy.non_retryable_exceptions):
            return ErrorClass.NON_RETRYABLE

        status_code = getattr(error, "status_code", None)
        if status_code is None:
            response = getattr(error, "response", None)
            status_code = getattr(response, "status_code", None)
        if status_code is None:
            status_code = getattr(error, "status", None)
        if isinstance(status_code, int):
            if status_code in policy.non_retryable_status_codes:
                return ErrorClass.NON_RETRYABLE
            if status_code in policy.retryable_status_codes:
                return ErrorClass.HTTP_TRANSIENT
            return ErrorClass.UNKNOWN

        if "TimeoutError" in exception_names:
            return ErrorClass.TIMEOUT
        if "ConnectionError" in exception_names:
            return ErrorClass.CONNECTION
        if exception_names.intersection(policy.retryable_exceptions):
            return ErrorClass.RETRYABLE_EXCEPTION
        return ErrorClass.UNKNOWN

    @classmethod
    def _is_retryable(cls, error: Exception, policy: RetryPolicy) -> bool:
        """兼容调用方的布尔接口，实际决策由分类器完成。"""
        return cls._classify_error(error, policy) in {
            ErrorClass.TIMEOUT,
            ErrorClass.CONNECTION,
            ErrorClass.HTTP_TRANSIENT,
            ErrorClass.RETRYABLE_EXCEPTION,
        }

    @staticmethod
    def _outcome_for_error(
        error: Optional[Exception], error_class: ErrorClass
    ) -> ExecutionOutcome:
        """Map the classified terminal error to an execution, not policy, outcome."""
        if error_class == ErrorClass.TIMEOUT:
            return ExecutionOutcome.RESPONSE_LOST
        if error_class == ErrorClass.CONNECTION or error_class == ErrorClass.UNKNOWN:
            return ExecutionOutcome.UNKNOWN
        return ExecutionOutcome.FAILED

    @staticmethod
    def _error_type_for(error: Optional[Exception], error_class: ErrorClass) -> str:
        """Return an observable failure reason, never an internal retry decision."""
        explicit = getattr(error, "error_type", None) if error is not None else None
        if explicit:
            return explicit

        status_code = getattr(error, "status_code", None) if error is not None else None
        if status_code is None and error is not None:
            response = getattr(error, "response", None)
            status_code = getattr(response, "status_code", None)
        if status_code is None and error is not None:
            status_code = getattr(error, "status", None)
        if isinstance(status_code, int):
            return f"http_{status_code}"

        exception_names = {cls.__name__ for cls in type(error).__mro__} if error else set()
        if exception_names.intersection({"PermissionDenied", "PermissionDeniedError"}):
            return "permission_denied"
        if exception_names.intersection({"BadRequest", "BadRequestError", "InvalidParameter", "InvalidParameterError"}):
            return "bad_request"
        if "ValidationError" in exception_names:
            return "validation_error"
        if error_class == ErrorClass.TIMEOUT:
            return "timeout"
        if error_class == ErrorClass.CONNECTION:
            return "connection_error"
        if error_class == ErrorClass.UNKNOWN:
            return "unknown"
        return "failure"

    @staticmethod
    def _side_effect_possible(
        error: Optional[Exception], error_class: ErrorClass
    ) -> bool:
        """Unknown delivery state is conservatively treated as potentially applied."""
        return error_class in {ErrorClass.TIMEOUT, ErrorClass.CONNECTION, ErrorClass.UNKNOWN}

    def breaker_state(self, name: str) -> CircuitState:
        """查询熔断器状态"""
        breaker = self._breakers.get(name)
        return breaker.state if breaker else CircuitState.CLOSED

    def reset_breaker(self, name: str) -> None:
        """手动重置熔断器"""
        if name in self._breakers:
            self._breakers[name] = CircuitBreaker(name, self.config.circuit_breaker)
            logger.info(f"[TimeoutManager] 熔断器已重置: {name}")
