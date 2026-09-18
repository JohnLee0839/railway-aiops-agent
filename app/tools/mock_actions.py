"""
Mock 运维动作工具 — 所有运维动作 Mock 实现

功能:
- 6 个基础动作
- 对应回滚动作
- MockFailureRate = 0.2 (20% 概率随机失败)
- 支持返回 success / failure / timeout / exception 等不同结果
- 每个动作可单独设置失败率
- 严禁永远返回 success=True
"""

import random
import time
import uuid
from typing import Callable, Optional, Dict, Any
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock

from app.models.incident import MockActionResult, RetryPolicy


# ============================================================
# 故障注入配置
# ============================================================

class FailureMode(str, Enum):
    """故障模式"""
    SUCCESS = "success"        # 正常成功
    FAILURE = "failure"        # 返回失败
    TIMEOUT = "timeout"        # 模拟超时
    EXCEPTION = "exception"    # 抛出异常


class ActionRisk(str, Enum):
    """动作对运营系统的风险级别。"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class ActionMetadata:
    """由注册表统一管理的动作执行与治理策略。"""
    timeout_seconds: float
    retry_policy: RetryPolicy
    risk: ActionRisk
    requires_approval: bool = False
    rollback_action: Optional[str] = None
    idempotent: bool = False
    # 状态探针是独立的只读 callable；预期状态由本次动作参数确定。
    state_verifier: Optional[Callable[[str], Dict[str, Any]]] = None
    expected_state: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
    verifier_timeout_seconds: float = 3.0


@dataclass(frozen=True)
class RegisteredAction:
    """一个可执行动作及其不可分离的治理元数据。"""
    handler: Callable[..., MockActionResult]
    metadata: ActionMetadata


@dataclass
class MockActionConfig:
    """单个 Mock 动作配置"""
    action_name: str
    failure_rate: float = 0.2  # 默认 20% 失败率
    min_duration_ms: float = 50.0
    max_duration_ms: float = 500.0
    # 故障模式概率分布（总和必须 ≤ 1.0）
    failure_mode_weights: Dict[FailureMode, float] = field(default_factory=lambda: {
        FailureMode.FAILURE: 0.6,
        FailureMode.TIMEOUT: 0.2,
        FailureMode.EXCEPTION: 0.2,
    })

    def roll_failure_mode(self) -> FailureMode:
        """随机决定本次是否失败及失败模式"""
        if random.random() > self.failure_rate:
            return FailureMode.SUCCESS
        # 按权重选择失败模式
        modes = list(self.failure_mode_weights.keys())
        weights = list(self.failure_mode_weights.values())
        return random.choices(modes, weights=weights, k=1)[0]


# ============================================================
# 全局 Mock 配置
# ============================================================

# 默认故障率：20%
DEFAULT_FAILURE_RATE = 0.2

# 每个动作的独立配置
ACTION_CONFIGS: Dict[str, MockActionConfig] = {
    "switch_backup_link": MockActionConfig(
        action_name="switch_backup_link",
        failure_rate=0.2,
        min_duration_ms=100,
        max_duration_ms=800,
    ),
    "restart_gateway": MockActionConfig(
        action_name="restart_gateway",
        failure_rate=0.25,  # 重启网关稍微容易失败
        min_duration_ms=200,
        max_duration_ms=1500,
    ),
    "block_suspicious_source": MockActionConfig(
        action_name="block_suspicious_source",
        failure_rate=0.15,
        min_duration_ms=50,
        max_duration_ms=300,
    ),
    "notify_dispatcher": MockActionConfig(
        action_name="notify_dispatcher",
        failure_rate=0.05,  # 通知调度员很少失败
        min_duration_ms=30,
        max_duration_ms=200,
    ),
    "generate_ticket": MockActionConfig(
        action_name="generate_ticket",
        failure_rate=0.05,
        min_duration_ms=20,
        max_duration_ms=150,
    ),
    "verify_network_health": MockActionConfig(
        action_name="verify_network_health",
        failure_rate=0.1,
        min_duration_ms=100,
        max_duration_ms=600,
    ),
    # 回滚动作（通常更可靠，失败率更低）
    "rollback_switch_backup_link": MockActionConfig(
        action_name="rollback_switch_backup_link",
        failure_rate=0.1,
        min_duration_ms=80,
        max_duration_ms=600,
    ),
    "rollback_block_suspicious_source": MockActionConfig(
        action_name="rollback_block_suspicious_source",
        failure_rate=0.08,
        min_duration_ms=40,
        max_duration_ms=250,
    ),
}


# ============================================================
# Mock 运行状态与只读状态探针
# ============================================================

_STATE_LOCK = Lock()
_MOCK_OPERATIONAL_STATE: Dict[str, Dict[str, Any]] = {
    "links": {},
    "gateways": {},
    "blocked_sources": {},
}


def _target(value: str) -> str:
    return value or "default"


def reset_mock_operational_state() -> None:
    """仅供测试和本地 Mock 场景重置运行状态。"""
    with _STATE_LOCK:
        for state in _MOCK_OPERATIONAL_STATE.values():
            state.clear()


def query_device_link_state(target: str) -> Dict[str, Any]:
    """只读查询指定设备当前激活的链路。"""
    with _STATE_LOCK:
        return {"link": _MOCK_OPERATIONAL_STATE["links"].get(_target(target), "primary")}


def query_gateway_health(target: str) -> Dict[str, Any]:
    """只读查询网关健康状态。"""
    with _STATE_LOCK:
        return {"health": _MOCK_OPERATIONAL_STATE["gateways"].get(_target(target), "unhealthy")}


def query_source_block_state(target: str) -> Dict[str, Any]:
    """只读查询来源 IP 的封禁状态。"""
    with _STATE_LOCK:
        return {"blocked": _MOCK_OPERATIONAL_STATE["blocked_sources"].get(_target(target), False)}


def expected_backup_link(_: Dict[str, Any]) -> Dict[str, Any]:
    return {"link": "backup"}


def expected_healthy_gateway(_: Dict[str, Any]) -> Dict[str, Any]:
    return {"health": "healthy"}


def expected_blocked_source(_: Dict[str, Any]) -> Dict[str, Any]:
    return {"blocked": True}


# ============================================================
# Mock 动作实现
# ============================================================

def _simulate_execution(action_name: str) -> MockActionResult:
    """
    通用执行模拟器。

    根据配置随机注入故障，返回 MockActionResult。
    严禁始终返回 success=True。
    """
    config = ACTION_CONFIGS.get(action_name)
    if config is None:
        config = MockActionConfig(action_name=action_name, failure_rate=DEFAULT_FAILURE_RATE)

    mode = config.roll_failure_mode()
    duration = random.uniform(config.min_duration_ms, config.max_duration_ms)

    # 模拟执行耗时
    time.sleep(duration / 1000.0)

    if mode == FailureMode.SUCCESS:
        return MockActionResult(
            action_name=action_name,
            success=True,
            message=f"[Mock] {action_name} 执行成功",
            duration_ms=duration,
            metadata={"mode": mode.value, "failure_rate": config.failure_rate},
        )
    elif mode == FailureMode.FAILURE:
        return MockActionResult(
            action_name=action_name,
            success=False,
            message=f"[Mock] {action_name} 执行失败（模拟故障注入）",
            duration_ms=duration,
            error_type="failure",
            metadata={"mode": mode.value, "failure_rate": config.failure_rate},
        )
    elif mode == FailureMode.TIMEOUT:
        # 模拟更长的等待再超时
        time.sleep(2.0)  # 额外等待模拟超时感
        return MockActionResult(
            action_name=action_name,
            success=False,
            message=f"[Mock] {action_name} 执行超时",
            duration_ms=duration + 2000,
            error_type="timeout",
            metadata={"mode": mode.value, "failure_rate": config.failure_rate},
        )
    else:  # EXCEPTION
        # 不返回结果，直接抛异常（由上层 TimeoutManager 捕获）
        raise RuntimeError(
            f"[Mock] {action_name} 抛出模拟异常（failure_rate={config.failure_rate}）"
        )


# ============================================================
# 6 个基础 Mock 动作
# ============================================================

def switch_backup_link(
    source: str = "",
    target: str = "",
    reason: str = "",
    **kwargs,
) -> MockActionResult:
    """
    切换到备用链路。

    副作用：改变链路状态（需 rollback_switch_backup_link 回滚）
    """
    result = _simulate_execution("switch_backup_link")
    device = _target(target or source)
    result.target = device
    if result.success:
        with _STATE_LOCK:
            _MOCK_OPERATIONAL_STATE["links"][device] = "backup"
        result.side_effect_possible = True
    return result


def restart_gateway(
    gateway_id: str = "",
    reason: str = "",
    **kwargs,
) -> MockActionResult:
    """
    重启网关。

    副作用：短暂断连（无回滚——重启不可逆）
    """
    result = _simulate_execution("restart_gateway")
    target = _target(gateway_id or kwargs.get("target", ""))
    result.target = target
    if result.success:
        with _STATE_LOCK:
            _MOCK_OPERATIONAL_STATE["gateways"][target] = "healthy"
        result.side_effect_possible = True
    return result


def block_suspicious_source(
    source_ip: str = "",
    reason: str = "",
    duration_seconds: int = 3600,
    **kwargs,
) -> MockActionResult:
    """
    封禁可疑来源 IP。

    副作用：IP 被封禁（需 rollback_block_suspicious_source 回滚）
    """
    result = _simulate_execution("block_suspicious_source")
    target = _target(source_ip)
    result.target = target
    if result.success:
        with _STATE_LOCK:
            _MOCK_OPERATIONAL_STATE["blocked_sources"][target] = True
        result.side_effect_possible = True
    return result


def notify_dispatcher(
    message: str = "",
    incident_id: str = "",
    severity: str = "",
    **kwargs,
) -> MockActionResult:
    """
    通知调度员。

    无副作用。
    """
    return _simulate_execution("notify_dispatcher")


def generate_ticket(
    title: str = "",
    description: str = "",
    incident_id: str = "",
    severity: str = "",
    **kwargs,
) -> MockActionResult:
    """
    生成工单。

    无直接副作用（工单可关闭）。
    """
    return _simulate_execution("generate_ticket")


def verify_network_health(
    target: str = "",
    expected_state: str = "healthy",
    **kwargs,
) -> MockActionResult:
    """
    验证网络健康状态。

    用于 Verifier 验证动作是否生效。
    """
    return _simulate_execution("verify_network_health")


# ============================================================
# 回滚动作
# ============================================================

def rollback_switch_backup_link(
    original_source: str = "",
    original_target: str = "",
    reason: str = "rollback",
    **kwargs,
) -> MockActionResult:
    """
    回滚链路切换。

    恢复原始链路配置。
    """
    result = _simulate_execution("rollback_switch_backup_link")
    device = _target(original_target or original_source)
    result.target = device
    if result.success:
        with _STATE_LOCK:
            _MOCK_OPERATIONAL_STATE["links"][device] = "primary"
        result.side_effect_possible = True
    return result


def rollback_block_suspicious_source(
    source_ip: str = "",
    reason: str = "rollback",
    **kwargs,
) -> MockActionResult:
    """
    解除 IP 封禁。

    恢复被封禁的 IP。
    """
    result = _simulate_execution("rollback_block_suspicious_source")
    target = _target(source_ip)
    result.target = target
    if result.success:
        with _STATE_LOCK:
            _MOCK_OPERATIONAL_STATE["blocked_sources"][target] = False
        result.side_effect_possible = True
    return result


# ============================================================
# 动作注册表（用于 ActionOrchestrator 查找）
# ============================================================

# 每个注册项同时定义工具、超时、重试、风险和回滚关系。
# 重试状态码仅包含可恢复的请求/服务错误，4xx 参数错误不会重试。
ACTION_REGISTRY: Dict[str, RegisteredAction] = {
    "switch_backup_link": RegisteredAction(
        handler=switch_backup_link,
        metadata=ActionMetadata(
            timeout_seconds=12.0,
            retry_policy=RetryPolicy(max_retries=1, base_delay_seconds=1.0),
            risk=ActionRisk.MEDIUM,
            rollback_action="rollback_switch_backup_link",
            state_verifier=query_device_link_state,
            expected_state=expected_backup_link,
        ),
    ),
    "restart_gateway": RegisteredAction(
        handler=restart_gateway,
        metadata=ActionMetadata(
            timeout_seconds=30.0,
            retry_policy=RetryPolicy(
                max_retries=1,
                base_delay_seconds=2.0,
                retryable_status_codes=[408, 429, 502, 503, 504],
            ),
            risk=ActionRisk.HIGH,
            requires_approval=True,
            state_verifier=query_gateway_health,
            expected_state=expected_healthy_gateway,
        ),
    ),
    "block_suspicious_source": RegisteredAction(
        handler=block_suspicious_source,
        metadata=ActionMetadata(
            timeout_seconds=8.0,
            retry_policy=RetryPolicy(max_retries=1, base_delay_seconds=1.0),
            risk=ActionRisk.HIGH,
            requires_approval=True,
            rollback_action="rollback_block_suspicious_source",
            state_verifier=query_source_block_state,
            expected_state=expected_blocked_source,
        ),
    ),
    "notify_dispatcher": RegisteredAction(
        handler=notify_dispatcher,
        metadata=ActionMetadata(
            timeout_seconds=5.0,
            retry_policy=RetryPolicy(max_retries=3, base_delay_seconds=0.5),
            risk=ActionRisk.LOW,
            idempotent=True,
        ),
    ),
    "generate_ticket": RegisteredAction(
        handler=generate_ticket,
        metadata=ActionMetadata(
            timeout_seconds=5.0,
            retry_policy=RetryPolicy(max_retries=1, base_delay_seconds=1.0),
            risk=ActionRisk.LOW,
        ),
    ),
    "verify_network_health": RegisteredAction(
        handler=verify_network_health,
        metadata=ActionMetadata(
            timeout_seconds=10.0,
            retry_policy=RetryPolicy(max_retries=2, base_delay_seconds=0.5),
            risk=ActionRisk.LOW,
            idempotent=True,
        ),
    ),
    "rollback_switch_backup_link": RegisteredAction(
        handler=rollback_switch_backup_link,
        metadata=ActionMetadata(
            timeout_seconds=10.0,
            retry_policy=RetryPolicy(max_retries=1, base_delay_seconds=1.0),
            risk=ActionRisk.MEDIUM,
            idempotent=True,
        ),
    ),
    "rollback_block_suspicious_source": RegisteredAction(
        handler=rollback_block_suspicious_source,
        metadata=ActionMetadata(
            timeout_seconds=6.0,
            retry_policy=RetryPolicy(max_retries=2, base_delay_seconds=0.5),
            risk=ActionRisk.MEDIUM,
            idempotent=True,
        ),
    ),
}

# 兼容现有调用方；新增代码应通过 get_registered_action 读取元数据。
ALL_MOCK_ACTIONS: Dict[str, Callable[..., MockActionResult]] = {
    name: registration.handler for name, registration in ACTION_REGISTRY.items()
}
ROLLBACK_MAP: Dict[str, str] = {
    name: registration.metadata.rollback_action
    for name, registration in ACTION_REGISTRY.items()
    if registration.metadata.rollback_action
}
HIGH_RISK_ACTIONS = {
    name
    for name, registration in ACTION_REGISTRY.items()
    if registration.metadata.requires_approval
}


def get_registered_action(name: str) -> Optional[RegisteredAction]:
    """获取动作及其执行、风险和回滚元数据。"""
    return ACTION_REGISTRY.get(name)


def get_action_metadata(name: str) -> Optional[ActionMetadata]:
    """获取动作元数据，不存在时返回 None。"""
    registration = get_registered_action(name)
    return registration.metadata if registration else None


def get_action(name: str) -> Optional[callable]:
    """通过名称获取动作函数"""
    registration = get_registered_action(name)
    return registration.handler if registration else None


def get_rollback_action(name: str) -> Optional[callable]:
    """获取某个动作对应的回滚动作"""
    metadata = get_action_metadata(name)
    if metadata and metadata.rollback_action:
        rollback = get_registered_action(metadata.rollback_action)
        return rollback.handler if rollback else None
    return None


def has_rollback(name: str) -> bool:
    """判断某个动作是否有回滚"""
    metadata = get_action_metadata(name)
    return bool(metadata and metadata.rollback_action)


def requires_approval(name: str) -> bool:
    """判断动作是否需要人工审批"""
    metadata = get_action_metadata(name)
    return bool(metadata and metadata.requires_approval)


def set_failure_rate(action_name: str, rate: float) -> None:
    """
    动态调整某个动作的故障率（用于测试）。

    Args:
        action_name: 动作名称
        rate: 新的故障率 (0.0 ~ 1.0)
    """
    if action_name in ACTION_CONFIGS:
        ACTION_CONFIGS[action_name].failure_rate = max(0.0, min(1.0, rate))
    else:
        ACTION_CONFIGS[action_name] = MockActionConfig(
            action_name=action_name,
            failure_rate=max(0.0, min(1.0, rate)),
        )
