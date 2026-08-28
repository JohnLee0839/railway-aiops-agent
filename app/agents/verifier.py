"""
Verifier — 验证 Mock 执行结果

职责:
- 验证 Mock 执行结果
- 判断动作是否生效
- 输出结果类型: SUCCESS / RETRY / COMPENSATE / ESCALATE
"""

from typing import List, Optional
from loguru import logger

from app.models.incident import (
    Incident,
    RunbookPlan,
    MockActionResult,
    VerificationResult,
    ActionStatus,
    IncidentState,
    SSEEventType,
)
from app.core.audit_store import audit_store


class Verifier:
    """
    Verifier — 验证执行结果。

    判断标准:
    - 全部成功 → SUCCESS → RESOLVED
    - 部分失败但可重试 → RETRY → ActionOrchestrator
    - 全部失败或误报 → COMPENSATE → COMPENSATING
    - 超时/连续失败 → ESCALATE → ESCALATED
    """

    def __init__(self):
        self.max_retry_cycles = 2  # 最多重试 2 轮

    async def verify(
        self,
        incident: Incident,
        plan: RunbookPlan,
        results: List[MockActionResult],
        thread_id: str,
        retry_cycle: int = 0,
    ) -> VerificationResult:
        """
        验证执行结果。

        Args:
            incident: 事件
            plan: 处置计划
            results: 执行结果列表
            thread_id: LangGraph thread ID
            retry_cycle: 当前重试轮次

        Returns:
            VerificationResult（动作状态、下一步建议）
        """
        logger.info(
            f"[Verifier] 开始验证: incident_id={incident.incident_id}, "
            f"steps={len(results)}, retry_cycle={retry_cycle}"
        )

        success_count = sum(1 for r in results if r.success)
        failure_count = len(results) - success_count
        escalated = any(
            r.error_type in ("escalated", "circuit_open") for r in results
        )

        # === 情况 1: 全部成功 ===
        if success_count == len(results):
            return self._make_result(
                incident, thread_id,
                action_status=ActionStatus.SUCCESS,
                next_state=IncidentState.VERIFIED,
                reason=f"全部 {success_count} 个动作执行成功",
            )

        # === 情况 2: 已升级 ===
        if escalated:
            return self._make_result(
                incident, thread_id,
                action_status=ActionStatus.ESCALATE,
                next_state=IncidentState.ESCALATED,
                reason="动作已升级为 ESCALATE（超时/熔断）",
            )

        # === 情况 3: 部分失败，检查是否可重试 ===
        if retry_cycle < self.max_retry_cycles:
            logger.info(
                f"[Verifier] 部分失败 ({failure_count}/{len(results)}), "
                f"建议重试 (cycle {retry_cycle + 1})"
            )
            return self._make_result(
                incident, thread_id,
                action_status=ActionStatus.RETRY,
                next_state=IncidentState.EXECUTING,
                reason=f"{failure_count}/{len(results)} 个动作失败，建议重试（第 {retry_cycle + 1} 轮）",
                retry_recommended=True,
            )

        # === 情况 4: 重试耗尽，需要补偿 ===
        if failure_count > 0:
            compensation_actions = [
                r.action_name for r in results
                if not r.success or r.error_type in ("failure", "timeout")
            ]

            return self._make_result(
                incident, thread_id,
                action_status=ActionStatus.COMPENSATE,
                next_state=IncidentState.COMPENSATING,
                reason=f"重试 {retry_cycle} 轮后仍有 {failure_count} 个动作失败，触发补偿",
                compensation_needed=True,
                compensation_actions=compensation_actions,
            )

        # === 情况 5: 未知状态（防御性） ===
        return self._make_result(
            incident, thread_id,
            action_status=ActionStatus.FAILED,
            next_state=IncidentState.FAILED,
            reason="验证异常: 无法确定动作状态",
        )

    async def verify_compensation(
        self,
        incident: Incident,
        compensation_results: List[MockActionResult],
        thread_id: str,
    ) -> VerificationResult:
        """
        验证补偿结果。

        Args:
            incident: 事件
            compensation_results: 补偿动作结果
            thread_id: LangGraph thread ID

        Returns:
            VerificationResult
        """
        success_count = sum(1 for r in compensation_results if r.success)
        total = len(compensation_results)

        if total == 0:
            return self._make_result(
                incident, thread_id,
                action_status=ActionStatus.SUCCESS,
                next_state=IncidentState.VERIFIED,
                reason="无需补偿",
            )

        if success_count == total:
            return self._make_result(
                incident, thread_id,
                action_status=ActionStatus.SUCCESS,
                next_state=IncidentState.VERIFIED,
                reason=f"补偿成功: {success_count}/{total} 个回滚动作完成",
            )
        else:
            return self._make_result(
                incident, thread_id,
                action_status=ActionStatus.FAILED,
                next_state=IncidentState.FAILED,
                reason=f"补偿失败: {total - success_count}/{total} 个回滚动作失败",
            )

    def _make_result(
        self,
        incident: Incident,
        thread_id: str,
        action_status: ActionStatus,
        next_state: IncidentState,
        reason: str,
        retry_recommended: bool = False,
        compensation_needed: bool = False,
        compensation_actions: Optional[List[str]] = None,
    ) -> VerificationResult:
        """构建 VerificationResult 并记录审计"""
        result = VerificationResult(
            incident_id=incident.incident_id,
            action_status=action_status,
            reason=reason,
            retry_recommended=retry_recommended,
            compensation_needed=compensation_needed,
            compensation_actions=compensation_actions or [],
            next_state=next_state,
        )

        audit_store.record(
            trace_id=incident.trace_id,
            incident_id=incident.incident_id,
            thread_id=thread_id,
            event_type=SSEEventType.VERIFICATION_FINISHED,
            actor="Verifier",
            action=action_status.value,
            detail={
                "action_status": action_status.value,
                "next_state": next_state.value,
                "reason": reason,
                "retry_recommended": retry_recommended,
                "compensation_needed": compensation_needed,
            },
            message=f"验证完成: {action_status.value} → {next_state.value}",
        )

        return result
