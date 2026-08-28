"""
ActionOrchestrator — 原 Executor 的升级版

职责:
- 仅执行 Mock 动作
- 支持失败、重试、补偿、超时升级
- 所有动作写入审计
- 高风险动作触发 ApprovalGate
"""

from typing import List, Optional, Dict, Any
from datetime import datetime
from loguru import logger

from app.models.incident import (
    Incident,
    RunbookPlan,
    MockActionResult,
    ApprovalAction,
    ApprovalRequest,
    ApprovalStatus,
    IncidentState,
    SSEEventType,
    ActionStatus,
)
from app.events.timeout_manager import TimeoutManager, ExecutionResult
from app.tools.mock_actions import (
    get_action,
    get_rollback_action,
    has_rollback,
    requires_approval,
    ALL_MOCK_ACTIONS,
    set_failure_rate,
    get_action as _get_action,
)
from app.core.audit_store import audit_store


class ApprovalGate:
    """
    审批门禁 — 高风险动作需人工确认。

    高风险动作:
    - STOP_TRAIN
    - BLOCK_SECTION
    - EMERGENCY_SHUTDOWN

    默认等待 10 分钟，超时未审批自动转 ESCALATE。
    """

    def __init__(self, timeout_minutes: int = 10):
        self.timeout_minutes = timeout_minutes
        self._pending: Dict[str, ApprovalRequest] = {}

    def requires_approval(self, action_name: str) -> bool:
        """判断动作是否需要审批"""
        return action_name in {
            ApprovalAction.STOP_TRAIN.value,
            ApprovalAction.BLOCK_SECTION.value,
            ApprovalAction.EMERGENCY_SHUTDOWN.value,
        }

    def create_request(
        self,
        incident_id: str,
        action: str,
        reason: str,
        trace_id: str = "",
        thread_id: str = "",
    ) -> ApprovalRequest:
        """创建审批请求（绑定 trace_id 和 thread_id）"""
        request = ApprovalRequest(
            incident_id=incident_id,
            trace_id=trace_id,
            thread_id=thread_id,
            action=ApprovalAction(action),
            reason=reason,
            timeout_minutes=self.timeout_minutes,
            status=ApprovalStatus.PENDING,
        )
        self._pending[request.request_id] = request
        logger.info(
            f"[ApprovalGate] 审批请求已创建: {request.request_id}, "
            f"action={action}, status=PENDING"
        )
        # 审计
        audit_store.record(
            trace_id=trace_id,
            incident_id=incident_id,
            thread_id=thread_id,
            event_type=SSEEventType.APPROVAL_REQUESTED,
            actor="ApprovalGate",
            action=f"approval_requested:{action}",
            detail={"approval_id": request.request_id, "action": action, "status": "PENDING"},
            message=f"审批请求: {action}",
        )
        return request

    def approve(self, request_id: str, approved_by: str = "admin") -> ApprovalRequest:
        """批准"""
        req = self._pending.get(request_id)
        if req:
            req.status = ApprovalStatus.APPROVED
            req.approved_by = approved_by
            req.approved_at = datetime.utcnow()
            logger.info(f"[ApprovalGate] 已批准: {request_id}")
            # 审计
            audit_store.record(
                trace_id=req.trace_id,
                incident_id=req.incident_id,
                thread_id=req.thread_id,
                event_type=SSEEventType.STATE_CHANGED,
                actor="ApprovalGate",
                action="approval_approved",
                detail={"approval_id": request_id, "action": req.action.value, "approved_by": approved_by},
                message=f"审批已批准: {req.action.value}",
            )
        return req

    def deny(self, request_id: str, approved_by: str = "admin") -> ApprovalRequest:
        """拒绝"""
        req = self._pending.get(request_id)
        if req:
            req.status = ApprovalStatus.DENIED
            req.approved_by = approved_by
            req.approved_at = datetime.utcnow()
            logger.info(f"[ApprovalGate] 已拒绝: {request_id}")
            # 审计
            audit_store.record(
                trace_id=req.trace_id,
                incident_id=req.incident_id,
                thread_id=req.thread_id,
                event_type=SSEEventType.STATE_CHANGED,
                actor="ApprovalGate",
                action="approval_denied",
                detail={"approval_id": request_id, "action": req.action.value, "denied_by": approved_by},
                message=f"审批已拒绝: {req.action.value}",
            )
        return req

    def timeout(self, request_id: str) -> ApprovalRequest:
        """审批超时 → 自动转 ESCALATE"""
        req = self._pending.get(request_id)
        if req:
            req.status = ApprovalStatus.TIMEOUT
            req.escalated_at = datetime.utcnow()
            logger.warning(f"[ApprovalGate] 审批超时: {request_id}, 转 ESCALATE")
            # 审计: 超时升级
            audit_store.record(
                trace_id=req.trace_id,
                incident_id=req.incident_id,
                thread_id=req.thread_id,
                event_type=SSEEventType.APPROVAL_TIMEOUT,
                actor="ApprovalGate",
                action="approval_timeout",
                detail={
                    "approval_id": request_id,
                    "action": req.action.value,
                    "timeout_minutes": req.timeout_minutes,
                },
                message=f"审批超时 ({req.timeout_minutes}min): {req.action.value} → ESCALATE",
            )
        return req

    def get_pending(self, incident_id: str) -> List[ApprovalRequest]:
        """获取待审批请求"""
        return [
            r for r in self._pending.values()
            if r.incident_id == incident_id and r.status == ApprovalStatus.PENDING
        ]


class ActionOrchestrator:
    """
    ActionOrchestrator — 原 Executor 的升级版

    仅执行 Mock 动作，支持:
    - 失败 → 重试（最多 3 次）
    - 重试耗尽 → ESCALATE
    - 误报/副作用 → COMPENSATE（回滚）
    - 所有动作写入审计（通过 AuditStore）
    """

    def __init__(self):
        self.timeout_manager = TimeoutManager()
        self.approval_gate = ApprovalGate()

    async def execute_plan(
        self,
        incident: Incident,
        plan: RunbookPlan,
        thread_id: str,
    ) -> List[MockActionResult]:
        """
        执行处置计划的所有步骤。

        Args:
            incident: 事件
            plan: 处置计划
            thread_id: LangGraph thread ID

        Returns:
            MockActionResult 列表
        """
        results: List[MockActionResult] = []

        for i, step in enumerate(plan.steps):
            logger.info(
                f"[ActionOrchestrator] 执行步骤 {i + 1}/{len(plan.steps)}: {step}"
            )

            # 解析步骤中的动作名
            action_name = self._parse_action_name(step)

            # 检查是否需要审批
            if self.approval_gate.requires_approval(action_name):
                approval_req = self.approval_gate.create_request(
                    incident.incident_id, action_name,
                    reason=f"计划步骤: {step}",
                    trace_id=incident.trace_id,
                    thread_id=thread_id,
                )
                audit_store.record(
                    trace_id=incident.trace_id,
                    incident_id=incident.incident_id,
                    thread_id=thread_id,
                    event_type=SSEEventType.APPROVAL_TIMEOUT,
                    actor="ActionOrchestrator",
                    action=f"awaiting_approval:{action_name}",
                    detail={
                        "approval_id": approval_req.request_id,
                        "action": action_name,
                        "step": step,
                    },
                    message=f"等待审批: {action_name}",
                )
                # 模拟审批等待（真实环境通过 SSE 回调）
                # 此处跳过审批等待，直接执行（Mock 模式下默认批准）
                logger.info(
                    f"[ActionOrchestrator] Mock 模式: 自动批准 {action_name}"
                )
                self.approval_gate.approve(approval_req.request_id)

            # 执行动作
            result = await self._execute_single_action(
                action_name, incident, thread_id, step
            )
            results.append(result)

            # 审计记录
            audit_store.record(
                trace_id=incident.trace_id,
                incident_id=incident.incident_id,
                thread_id=thread_id,
                event_type=SSEEventType.ACTION_EXECUTED,
                actor="ActionOrchestrator",
                action=action_name,
                detail={
                    "step_index": i,
                    "step": step,
                    "success": result.success,
                    "message": result.message,
                    "duration_ms": result.duration_ms,
                    "retry_count": result.retry_count,
                },
                message=result.message,
            )

            # 如果动作失败，后续动作是否继续取决于策略
            if not result.success:
                logger.warning(
                    f"[ActionOrchestrator] 步骤 {i + 1} 失败: {result.message}"
                )
                # 不中断：继续执行剩余步骤（由 Verifier 统一判断）

        return results

    async def execute_compensation(
        self,
        incident: Incident,
        plan: RunbookPlan,
        executed_results: List[MockActionResult],
        thread_id: str,
    ) -> List[MockActionResult]:
        """
        执行补偿（回滚）操作。

        只回滚有副作用且提供了 rollback 的动作。

        Args:
            incident: 事件
            plan: 原始计划（含 rollback_steps）
            executed_results: 已执行的动作结果
            thread_id: LangGraph thread ID

        Returns:
            补偿结果列表
        """
        compensation_results: List[MockActionResult] = []

        audit_store.record(
            trace_id=incident.trace_id,
            incident_id=incident.incident_id,
            thread_id=thread_id,
            event_type=SSEEventType.COMPENSATION_STARTED,
            actor="ActionOrchestrator",
            action="compensate",
            detail={"rollback_steps": plan.rollback_steps},
            message="开始执行补偿",
        )

        # 1. 执行计划中声明的 rollback_steps
        for step in plan.rollback_steps:
            action_name = self._parse_action_name(step)
            result = await self._execute_single_action(
                action_name, incident, thread_id, f"回滚: {step}"
            )
            compensation_results.append(result)

            audit_store.record(
                trace_id=incident.trace_id,
                incident_id=incident.incident_id,
                thread_id=thread_id,
                event_type=SSEEventType.COMPENSATION_STARTED,
                actor="ActionOrchestrator",
                action=f"compensate:{action_name}",
                detail={"success": result.success, "message": result.message},
                message=f"补偿动作: {action_name} → {'成功' if result.success else '失败'}",
            )

        # 2. 对已执行的有副作用的动作自动回滚
        for result in executed_results:
            if result.success and has_rollback(result.action_name):
                rollback_fn = get_rollback_action(result.action_name)
                if rollback_fn:
                    rollback_result = await self._execute_single_action(
                        f"rollback_{result.action_name}",
                        incident,
                        thread_id,
                        f"自动回滚: {result.action_name}",
                    )
                    compensation_results.append(rollback_result)

        return compensation_results

    async def _execute_single_action(
        self,
        action_name: str,
        incident: Incident,
        thread_id: str,
        step_description: str,
    ) -> MockActionResult:
        """
        执行单个 Mock 动作（含超时、重试）。

        Args:
            action_name: 动作名称
            incident: 事件
            thread_id: LangGraph thread ID
            step_description: 步骤描述

        Returns:
            MockActionResult
        """
        action_fn = get_action(action_name)
        if action_fn is None:
            return MockActionResult(
                action_name=action_name,
                success=False,
                message=f"未知动作: {action_name}",
                error_type="unknown_action",
            )

        # 使用 TimeoutManager 包装执行
        exec_result: ExecutionResult = await self.timeout_manager.execute_with_timeout(
            coro_func=self._wrap_sync_action(action_fn),
            operation_name=f"action:{action_name}",
            timeout_seconds=15.0,  # 工具调用超时
        )

        if exec_result.success and isinstance(exec_result.result, MockActionResult):
            result = exec_result.result
            result.retry_count = exec_result.retry_count
            result.duration_ms = exec_result.total_duration_ms
            return result
        elif exec_result.escalated:
            return MockActionResult(
                action_name=action_name,
                success=False,
                message=f"动作超时/重试耗尽，已升级 ESCALATE: {exec_result.error}",
                error_type="escalated" if exec_result.final_action == "escalate" else exec_result.final_action,
                retry_count=exec_result.retry_count,
                duration_ms=exec_result.total_duration_ms,
            )
        else:
            return MockActionResult(
                action_name=action_name,
                success=False,
                message=f"执行异常: {exec_result.error}",
                error_type="exception",
                retry_count=exec_result.retry_count,
                duration_ms=exec_result.total_duration_ms,
            )

    @staticmethod
    async def _wrap_sync_action(fn):
        """将同步 Mock 动作包装为异步"""
        import asyncio
        return await asyncio.get_running_loop().run_in_executor(None, fn)

    @staticmethod
    def _parse_action_name(step: str) -> str:
        """
        从步骤描述中解析动作名称。

        示例:
        - "使用 switch_backup_link 切换到备用链路" → "switch_backup_link"
        - "switch_backup_link" → "switch_backup_link"
        """
        step_lower = step.lower()
        for action_name in ALL_MOCK_ACTIONS:
            if action_name in step_lower:
                return action_name
        # 尝试匹配高风险动作
        for high_risk in ["STOP_TRAIN", "BLOCK_SECTION", "EMERGENCY_SHUTDOWN"]:
            if high_risk.lower() in step_lower:
                return high_risk
        # 回退: 返回整个 step 作为 action_name
        return step.strip().split()[0] if step.strip() else "unknown"
