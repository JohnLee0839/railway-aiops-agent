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
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
from enum import Enum

from app.models.incident import MockActionResult


# ============================================================
# 故障注入配置
# ============================================================

class FailureMode(str, Enum):
    """故障模式"""
    SUCCESS = "success"        # 正常成功
    FAILURE = "failure"        # 返回失败
    TIMEOUT = "timeout"        # 模拟超时
    EXCEPTION = "exception"    # 抛出异常


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
    return _simulate_execution("switch_backup_link")


def restart_gateway(
    gateway_id: str = "",
    reason: str = "",
    **kwargs,
) -> MockActionResult:
    """
    重启网关。

    副作用：短暂断连（无回滚——重启不可逆）
    """
    return _simulate_execution("restart_gateway")


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
    return _simulate_execution("block_suspicious_source")


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
    return _simulate_execution("rollback_switch_backup_link")


def rollback_block_suspicious_source(
    source_ip: str = "",
    reason: str = "rollback",
    **kwargs,
) -> MockActionResult:
    """
    解除 IP 封禁。

    恢复被封禁的 IP。
    """
    return _simulate_execution("rollback_block_suspicious_source")


# ============================================================
# 动作注册表（用于 ActionOrchestrator 查找）
# ============================================================

# 基础动作 → 回滚动作映射
ROLLBACK_MAP: Dict[str, str] = {
    "switch_backup_link": "rollback_switch_backup_link",
    "block_suspicious_source": "rollback_block_suspicious_source",
    # restart_gateway 无回滚（不可逆）
    # notify_dispatcher 无回滚（无副作用）
    # generate_ticket 无回滚（工单可关闭）
    # verify_network_health 无回滚（只读）
}

# 所有可用动作
ALL_MOCK_ACTIONS: Dict[str, callable] = {
    "switch_backup_link": switch_backup_link,
    "restart_gateway": restart_gateway,
    "block_suspicious_source": block_suspicious_source,
    "notify_dispatcher": notify_dispatcher,
    "generate_ticket": generate_ticket,
    "verify_network_health": verify_network_health,
    "rollback_switch_backup_link": rollback_switch_backup_link,
    "rollback_block_suspicious_source": rollback_block_suspicious_source,
}

# 需要审批的高风险动作
HIGH_RISK_ACTIONS = {
    "STOP_TRAIN",
    "BLOCK_SECTION",
    "EMERGENCY_SHUTDOWN",
}


def get_action(name: str) -> Optional[callable]:
    """通过名称获取动作函数"""
    return ALL_MOCK_ACTIONS.get(name)


def get_rollback_action(name: str) -> Optional[callable]:
    """获取某个动作对应的回滚动作"""
    rollback_name = ROLLBACK_MAP.get(name)
    if rollback_name:
        return ALL_MOCK_ACTIONS.get(rollback_name)
    return None


def has_rollback(name: str) -> bool:
    """判断某个动作是否有回滚"""
    return name in ROLLBACK_MAP


def requires_approval(name: str) -> bool:
    """判断动作是否需要人工审批"""
    return name in HIGH_RISK_ACTIONS


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
