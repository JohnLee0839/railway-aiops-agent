"""
事件驱动状态机（修正版）

状态流转:
NEW → TRIAGED → PLANNED → EXECUTING → VERIFIED → RESOLVED

异常分支:
- EXECUTING 失败/误报 → COMPENSATING
- COMPENSATING → VERIFIED / FAILED
- 超时/连续失败 → ESCALATED

终态语义:
- FAILED 和 ESCALATED 是严格终态（不可再迁移）
- RESOLVED 不是绝对终态 — 若后续发现误报或副作用，允许 RESOLVED → COMPENSATING
- 补偿成功后可回到 VERIFIED → RESOLVED，失败后进入 FAILED 或 ESCALATED
"""

from typing import Dict, Set, Optional, List, Any
from datetime import datetime
from loguru import logger

from app.models.incident import (
    IncidentState,
    IncidentRecord,
    StateTransition,
    SSEEventType,
)


class StateMachine:
    """
    事件处理状态机。

    每次 transition() 自动生成审计事件
    (通过注入的 _audit_callback 或 _emit_sse_callback)。
    """

    # ================================================================
    # 合法状态迁移表（最终版）
    # ================================================================
    VALID_TRANSITIONS: Dict[IncidentState, Set[IncidentState]] = {
        IncidentState.NEW: {
            IncidentState.NEW,            # 初始化自身
            IncidentState.TRIAGED,
            IncidentState.FAILED,
            IncidentState.ESCALATED,
        },
        IncidentState.TRIAGED: {
            IncidentState.PLANNED,
            IncidentState.FAILED,
            IncidentState.ESCALATED,
        },
        IncidentState.PLANNED: {
            IncidentState.EXECUTING,
            IncidentState.FAILED,
            IncidentState.ESCALATED,
        },
        IncidentState.EXECUTING: {
            IncidentState.VERIFIED,
            IncidentState.COMPENSATING,
            IncidentState.FAILED,
            IncidentState.ESCALATED,
        },
        IncidentState.COMPENSATING: {
            IncidentState.VERIFIED,       # 补偿成功
            IncidentState.FAILED,         # 补偿失败
            IncidentState.ESCALATED,      # 补偿超时升级
        },
        IncidentState.VERIFIED: {
            IncidentState.RESOLVED,
            IncidentState.COMPENSATING,   # 后续发现副作用
            IncidentState.FAILED,
        },
        IncidentState.RESOLVED: {
            IncidentState.COMPENSATING,   # 误报/副作用触发补偿
        },
        IncidentState.FAILED: {
            # FAILED 是严格终态 — 不可再迁移
        },
        IncidentState.ESCALATED: {
            # ESCALATED 是严格终态 — 人工介入后通过 API 重新打开
        },
    }

    # ================================================================
    # 终态集合（修正版）
    # ================================================================
    # RESOLVED 不在 TERMINAL_STATES 中，因为它可以被 COMPENSATING 打断
    TERMINAL_STATES: Set[IncidentState] = {
        IncidentState.FAILED,
        IncidentState.ESCALATED,
    }

    # RESOLVED 是"软终态"——正常流程的终点，但允许被后续补偿流程重新打开
    SOFT_TERMINAL_STATES: Set[IncidentState] = {
        IncidentState.RESOLVED,
    }

    def __init__(self):
        # 可选的外部回调（由 AuditStore 注入）
        self._audit_callback: Optional[callable] = None

    def set_audit_callback(self, callback: callable) -> None:
        """
        注入审计回调函数。

        每次 transition() 成功后调用:
        callback(trace_id, incident_id, thread_id, event_type, actor, action, detail, state_from, state_to, message)
        """
        self._audit_callback = callback

    # ================================================================
    # 公共方法
    # ================================================================

    def can_transition(self, from_state: IncidentState, to_state: IncidentState) -> bool:
        """检查状态迁移是否合法"""
        valid_targets = self.VALID_TRANSITIONS.get(from_state, set())
        return to_state in valid_targets

    def transition(
        self,
        record: IncidentRecord,
        to_state: IncidentState,
        reason: str = "",
        triggered_by: str = "",
        trace_id: str = "",
        thread_id: str = "",
    ) -> IncidentRecord:
        """
        执行状态迁移，并自动写入审计。

        Args:
            record: 事件记录
            to_state: 目标状态
            reason: 迁移原因
            triggered_by: 触发者（agent 名称）
            trace_id: 追踪 ID（审计必需）
            thread_id: LangGraph thread ID（审计必需）

        Returns:
            更新后的事件记录

        Raises:
            ValueError: 非法状态迁移
        """
        from_state = record.state

        if not self.can_transition(from_state, to_state):
            msg = (
                f"非法状态迁移: {from_state.value} → {to_state.value}. "
                f"合法目标: {[s.value for s in self.VALID_TRANSITIONS.get(from_state, set())]}"
            )
            logger.error(f"[StateMachine] {msg}")
            raise ValueError(msg)

        # 记录迁移
        transition_record = StateTransition(
            from_state=from_state,
            to_state=to_state,
            timestamp=datetime.utcnow(),
            reason=reason,
            triggered_by=triggered_by,
        )

        record.state = to_state
        record.state_history.append(transition_record)
        record.updated_at = datetime.utcnow()

        if to_state == IncidentState.RESOLVED:
            record.resolved_at = datetime.utcnow()

        logger.info(
            f"[StateMachine] {record.incident_id}: "
            f"{from_state.value} → {to_state.value} "
            f"(by {triggered_by}, reason: {reason})"
        )

        # —— 自动审计回调 ——
        if self._audit_callback:
            try:
                self._audit_callback(
                    trace_id=trace_id,
                    incident_id=record.incident_id,
                    thread_id=thread_id,
                    event_type=SSEEventType.STATE_CHANGED,
                    actor=triggered_by,
                    action=f"transition:{from_state.value}→{to_state.value}",
                    detail={
                        "from_state": from_state.value,
                        "to_state": to_state.value,
                        "reason": reason,
                    },
                    state_from=from_state,
                    state_to=to_state,
                    message=f"状态迁移: {from_state.value} → {to_state.value} by {triggered_by}",
                )
            except Exception as e:
                logger.warning(f"[StateMachine] 审计回调异常（不影响主流程）: {e}")

        return record

    def is_terminal(self, state: IncidentState) -> bool:
        """检查是否为严格终态（FAILED / ESCALATED）"""
        return state in self.TERMINAL_STATES

    def is_soft_terminal(self, state: IncidentState) -> bool:
        """检查是否为软终态（RESOLVED — 可被补偿打破）"""
        return state in self.SOFT_TERMINAL_STATES

    def is_final(self, state: IncidentState) -> bool:
        """检查是否为任意终态（严格 + 软终态）"""
        return self.is_terminal(state) or self.is_soft_terminal(state)

    def get_valid_targets(self, state: IncidentState) -> List[IncidentState]:
        """获取当前状态的合法目标状态"""
        return list(self.VALID_TRANSITIONS.get(state, set()))

    def get_state_history(self, record: IncidentRecord) -> List[Dict]:
        """获取状态迁移历史（序列化格式）"""
        return [
            {
                "from": t.from_state.value,
                "to": t.to_state.value,
                "timestamp": t.timestamp.isoformat(),
                "reason": t.reason,
                "triggered_by": t.triggered_by,
            }
            for t in record.state_history
        ]


# 全局单例
state_machine = StateMachine()
