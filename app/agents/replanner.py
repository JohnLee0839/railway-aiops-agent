"""
Replanner — 根据 Verifier 结果路由

路由逻辑:
- SUCCESS → RESOLVED
- RETRY → ActionOrchestrator
- COMPENSATE → COMPENSATING
- ESCALATE → ESCALATED

与原 replanner 的 key 区别:
- 不只支持 continue / replan / respond
- 必须支持补偿与超时升级
- 必须与 TimeoutManager 协同工作
"""

from enum import Enum
from typing import Optional
from loguru import logger

from app.models.incident import (
    VerificationResult,
    ActionStatus,
    IncidentState,
    SSEEventType,
)
from app.core.audit_store import audit_store
from app.core.state_machine import state_machine
from app.core.incident_store import incident_store


class ReplanAction(str, Enum):
    """重规划决策"""
    RESOLVE = "resolve"          # → RESOLVED
    RETRY = "retry"              # → 重新执行
    COMPENSATE = "compensate"    # → COMPENSATING
    ESCALATE = "escalate"        # → ESCALATED
    FAIL = "fail"                # → FAILED


class Replanner:
    """
    Replanner — 升级版重规划器。

    与旧版的核心区别:
    1. 不再使用 continue/replan/respond 三态决策
    2. 基于 Verifier 结果进行 5 态路由
    3. 包含 COMPENSATE 和 ESCALATE 分支
    4. 与 TimeoutManager 和 StateMachine 协同
    """

    def __init__(self):
        self.retry_count: int = 0
        self.max_retries: int = 3

    async def decide(
        self,
        incident_id: str,
        thread_id: str,
        trace_id: str,
        verification: VerificationResult,
    ) -> ReplanAction:
        """
        根据 Verifier 结果做出路由决策。

        Args:
            incident_id: 事件 ID
            thread_id: LangGraph thread ID
            trace_id: 追踪 ID
            verification: Verifier 输出

        Returns:
            ReplanAction 枚举
        """
        logger.info(
            f"[Replanner] 决策: incident={incident_id}, "
            f"verification_status={verification.action_status.value}"
        )

        action = self._map_status(verification)

        # 更新事件存储状态
        record = incident_store.get(incident_id)
        if record:
            try:
                state_machine.transition(
                    record,
                    verification.next_state,
                    reason=f"Replanner 决策: {action.value}",
                    triggered_by="Replanner",
                )
                incident_store.update(record)
            except ValueError as e:
                logger.error(f"[Replanner] 状态迁移失败: {e}")

        # 审计
        audit_store.record(
            trace_id=trace_id,
            incident_id=incident_id,
            thread_id=thread_id,
            event_type=self._event_type_for_action(action),
            actor="Replanner",
            action=action.value,
            detail={
                "verification_status": verification.action_status.value,
                "next_state": verification.next_state.value,
                "reason": verification.reason,
                "retry_count": self.retry_count,
            },
            message=f"决策: {action.value} → {verification.next_state.value}",
        )

        return action

    def _map_status(self, verification: VerificationResult) -> ReplanAction:
        """将 Verifier 结果映射为 ReplanAction"""
        status = verification.action_status

        if status == ActionStatus.SUCCESS:
            return ReplanAction.RESOLVE

        elif status == ActionStatus.RETRY:
            self.retry_count += 1
            if self.retry_count > self.max_retries:
                logger.warning(
                    f"[Replanner] 重试次数超过上限 ({self.max_retries}), 改为 ESCALATE"
                )
                return ReplanAction.ESCALATE
            return ReplanAction.RETRY

        elif status == ActionStatus.COMPENSATE:
            return ReplanAction.COMPENSATE

        elif status == ActionStatus.ESCALATE:
            return ReplanAction.ESCALATE

        else:  # FAILED
            return ReplanAction.FAIL

    @staticmethod
    def _event_type_for_action(action: ReplanAction) -> SSEEventType:
        """将 ReplanAction 映射为 SSE 事件类型"""
        mapping = {
            ReplanAction.RESOLVE: SSEEventType.INCIDENT_RESOLVED,
            ReplanAction.RETRY: SSEEventType.RETRY_SCHEDULED,
            ReplanAction.COMPENSATE: SSEEventType.COMPENSATION_STARTED,
            ReplanAction.ESCALATE: SSEEventType.INCIDENT_ESCALATED,
            ReplanAction.FAIL: SSEEventType.INCIDENT_FAILED,
        }
        return mapping.get(action, SSEEventType.STATE_CHANGED)

    def reset_retry_count(self) -> None:
        """重置重试计数器"""
        self.retry_count = 0
