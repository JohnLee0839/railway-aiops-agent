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
    SSEEventType,
)
from app.events.timeout_manager import TimeoutManager, ExecutionResult
from app.tools.mock_actions import (
    get_action_metadata,
    get_rollback_action,
    get_registered_action,
    has_rollback,
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
        """从动作注册表读取审批要求，避免独立高风险名单漂移。"""
        metadata = get_action_metadata(action_name)
        return bool(metadata and metadata.requires_approval)

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

    def __init__(self, journal_store=None):
        self.timeout_manager = TimeoutManager()
        self.approval_gate = ApprovalGate()
        self.journal_store = journal_store

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
        plan.bind_execution_identities(incident.incident_id)

        for i, instruction in enumerate(plan.actions):
            action_name = instruction.action.value
            step = instruction.description or action_name
            action_metadata = get_action_metadata(action_name)
            logger.info(
                f"[ActionOrchestrator] 执行步骤 {i + 1}/{len(plan.actions)}: {step}"
            )

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
                        "plan_revision": plan.plan_revision,
                        "step_id": instruction.step_id,
                        "action_id": instruction.action_id,
                        "idempotency_key": instruction.idempotency_key,
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
            if self.journal_store is not None:
                self.journal_store.start_action_journal(
                    incident_id=incident.incident_id,
                    plan_id=plan.plan_id,
                    plan_revision=plan.plan_revision,
                    step_id=instruction.step_id,
                    action_id=instruction.action_id,
                    idempotency_key=instruction.idempotency_key,
                    action_name=action_name,
                    target=self._target_from_arguments(instruction.arguments),
                    request_metadata=instruction.arguments,
                )
            result = await self._execute_single_action(
                action_name, incident, thread_id, step, instruction.arguments, instruction.action_id
            )
            if self.journal_store is not None:
                self.journal_store.finish_action_journal(
                    instruction.action_id, result, result.metadata
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
                    "plan_id": plan.plan_id,
                    "plan_revision": plan.plan_revision,
                    "step_id": instruction.step_id,
                    "action_id": instruction.action_id,
                    "idempotency_key": instruction.idempotency_key,
                    "arguments": instruction.arguments,
                    "risk": action_metadata.risk.value if action_metadata else "unknown",
                    "success": result.success,
                    "message": result.message,
                    "duration_ms": result.duration_ms,
                    "retry_count": result.retry_count,
                    "retry_exhausted": result.retry_exhausted,
                    "retryable": result.retryable,
                    "side_effect_possible": result.side_effect_possible,
                    "side_effect_confirmed": result.side_effect_confirmed,
                    "outcome": result.outcome,
                },
                message=result.message,
            )

            # 如果动作失败，后续动作是否继续取决于策略
            if not result.success:
                logger.warning(
                    f"[ActionOrchestrator] 步骤 {i + 1} 失败: {result.message}"
                )
                # A later plan step is never safe to execute after the durable
                # cursor is blocked on this one. Verifier/Replanner decide what
                # to do with this terminal failure.
                break

        return results

    async def execute_instruction(
        self,
        incident: Incident,
        plan: RunbookPlan,
        instruction,
        thread_id: str,
    ) -> MockActionResult:
        """Execute one existing plan instruction through the Phase 3 journal path."""
        action_name = instruction.action.value
        if self.approval_gate.requires_approval(action_name):
            approval_req = self.approval_gate.create_request(
                incident.incident_id,
                action_name,
                reason=f"计划步骤: {instruction.description or action_name}",
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
                detail={"approval_id": approval_req.request_id, "action_id": instruction.action_id},
                message=f"等待审批: {action_name}",
            )
            # Keep the existing Mock-mode approval semantics; production callers
            # still replace this gate through the established approval flow.
            self.approval_gate.approve(approval_req.request_id)
        if self.journal_store is not None:
            self.journal_store.start_action_journal(
                incident_id=incident.incident_id,
                plan_id=plan.plan_id,
                plan_revision=plan.plan_revision,
                step_id=instruction.step_id,
                action_id=instruction.action_id,
                idempotency_key=instruction.idempotency_key,
                action_name=action_name,
                target=self._target_from_arguments(instruction.arguments),
                request_metadata=instruction.arguments,
            )
        result = await self._execute_single_action(
            action_name,
            incident,
            thread_id,
            instruction.description or action_name,
            instruction.arguments,
            instruction.action_id,
        )
        if self.journal_store is not None:
            self.journal_store.finish_action_journal(
                instruction.action_id,
                result,
                result.metadata,
            )
        return result

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
        plan.bind_execution_identities(incident.incident_id)

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

        # 1. 执行计划中声明的 rollback_actions
        for rollback_index, instruction in enumerate(plan.rollback_actions):
            action_name = instruction.action.value
            step = instruction.description or action_name
            if self.journal_store is not None:
                self.journal_store.start_action_journal(
                    incident_id=incident.incident_id,
                    plan_id=plan.plan_id,
                    plan_revision=plan.plan_revision,
                    step_id=instruction.step_id,
                    action_id=instruction.action_id,
                    idempotency_key=instruction.idempotency_key,
                    action_name=action_name,
                    target=self._target_from_arguments(instruction.arguments),
                    request_metadata=instruction.arguments,
                )
            result = await self._execute_single_action(
                action_name,
                incident,
                thread_id,
                f"回滚: {step}",
                instruction.arguments,
                instruction.action_id,
            )
            compensation_results.append(result)
            if self.journal_store is not None:
                self.journal_store.finish_action_journal(
                    instruction.action_id, result, result.metadata
                )

            audit_store.record(
                trace_id=incident.trace_id,
                incident_id=incident.incident_id,
                thread_id=thread_id,
                event_type=SSEEventType.COMPENSATION_STARTED,
                actor="ActionOrchestrator",
                action=f"compensate:{action_name}",
                detail={
                    "success": result.success,
                    "message": result.message,
                    "plan_id": plan.plan_id,
                    "plan_revision": plan.plan_revision,
                    "step_id": instruction.step_id,
                    "action_id": instruction.action_id,
                    "idempotency_key": instruction.idempotency_key,
                    "rollback_index": rollback_index,
                },
                message=f"补偿动作: {action_name} → {'成功' if result.success else '失败'}",
            )

        # 2. 对已执行的有副作用的动作自动回滚
        for result in executed_results:
            # 未确认副作用时禁止猜测性回滚，避免重复或反向修改状态。
            if result.side_effect_confirmed and has_rollback(result.action_name):
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
        action_arguments: Optional[Dict[str, Any]] = None,
        action_id: Optional[str] = None,
        attempt_offset: int = 0,
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
        registration = get_registered_action(action_name)
        if registration is None:
            return MockActionResult(
                action_name=action_name,
                success=False,
                message=f"未知动作: {action_name}",
                error_type="unknown_action",
            )

        # 执行策略由注册表定义，不能由调用方以统一默认值覆盖。
        exec_result: ExecutionResult = await self.timeout_manager.execute_with_timeout(
            # 传入 callable 和参数；不要在这里提前创建 coroutine object。
            coro_func=self._wrap_sync_action,
            operation_name=f"action:{action_name}",
            timeout_seconds=registration.metadata.timeout_seconds,
            retry_policy=registration.metadata.retry_policy,
            uncertain_on_timeout=not registration.metadata.idempotent,
            args=(registration.handler, action_arguments or {}),
            attempt_observer=self._attempt_observer(
                action_id, action_arguments, attempt_offset
            ),
        )

        if exec_result.success and isinstance(exec_result.result, MockActionResult):
            result = exec_result.result
            result.retry_count = exec_result.retry_count
            result.duration_ms = exec_result.total_duration_ms
            # 兼容旧调用方手工构造的 success=True、未填写 outcome 的结果。
            result.outcome = (
                exec_result.outcome.value
                if exec_result.outcome.value != "unknown"
                else "success"
            )
            result.retry_exhausted = exec_result.retry_exhausted
            result.retryable = exec_result.retryable
            result.side_effect_possible = exec_result.side_effect_possible or not registration.metadata.idempotent
            result.target = result.target or (action_arguments or {}).get("target")
            result.metadata = {
                **result.metadata,
                "action_arguments": action_arguments or {},
            }
            return result
        elif exec_result.escalated:
            error_message = exec_result.error_message or exec_result.error or "执行已升级"
            return MockActionResult(
                action_name=action_name,
                success=False,
                message=f"动作执行已升级 ESCALATE: {error_message}",
                error_type=exec_result.error_type or (
                    "escalated" if exec_result.final_action == "escalate" else exec_result.final_action
                ),
                retry_count=exec_result.retry_count,
                duration_ms=exec_result.total_duration_ms,
                outcome=exec_result.outcome.value,
                retry_exhausted=exec_result.retry_exhausted,
                retryable=exec_result.retryable,
                side_effect_possible=exec_result.side_effect_possible or not registration.metadata.idempotent,
                target=exec_result.target or (action_arguments or {}).get("target"),
                metadata={"action_arguments": action_arguments or {}},
            )
        elif exec_result.final_action == "unknown":
            error_message = exec_result.error_message or exec_result.error or "执行状态不确定"
            return MockActionResult(
                action_name=action_name,
                success=False,
                message=f"动作执行状态不确定，需对账后决定: {error_message}",
                error_type=exec_result.error_type or "unknown",
                retry_count=exec_result.retry_count,
                duration_ms=exec_result.total_duration_ms,
                outcome=exec_result.outcome.value,
                retry_exhausted=exec_result.retry_exhausted,
                retryable=exec_result.retryable,
                side_effect_possible=exec_result.side_effect_possible or not registration.metadata.idempotent,
                target=exec_result.target or (action_arguments or {}).get("target"),
                metadata={"action_arguments": action_arguments or {}},
            )
        else:
            error_message = exec_result.error_message or exec_result.error or "未知执行错误"
            return MockActionResult(
                action_name=action_name,
                success=False,
                message=f"执行异常: {error_message}",
                error_type=exec_result.error_type or "exception",
                retry_count=exec_result.retry_count,
                duration_ms=exec_result.total_duration_ms,
                outcome=exec_result.outcome.value,
                retry_exhausted=exec_result.retry_exhausted,
                retryable=exec_result.retryable,
                side_effect_possible=exec_result.side_effect_possible or not registration.metadata.idempotent,
                target=exec_result.target or (action_arguments or {}).get("target"),
                metadata={"action_arguments": action_arguments or {}},
            )

    @staticmethod
    async def _wrap_sync_action(fn, kwargs: Dict[str, Any]):
        """将同步 Mock 动作包装为异步"""
        import asyncio
        from functools import partial

        return await asyncio.get_running_loop().run_in_executor(None, partial(fn, **kwargs))

    @staticmethod
    def _target_from_arguments(arguments: Dict[str, Any]) -> Optional[str]:
        for key in ("target", "gateway_id", "source_ip", "source"):
            if arguments.get(key):
                return str(arguments[key])
        return None

    async def resume_action(
        self,
        incident: Incident,
        instruction,
        thread_id: str,
        attempt_offset: int,
    ) -> MockActionResult:
        """Execute an existing durable action identity during reconciliation recovery.

        The caller owns final journal persistence. This method intentionally does
        not create a second action journal or generate a new plan identity.
        """
        return await self._execute_single_action(
            instruction.action.value,
            incident,
            thread_id,
            instruction.description or instruction.action.value,
            instruction.arguments,
            instruction.action_id,
            attempt_offset,
        )

    def _attempt_observer(
        self,
        action_id: Optional[str],
        arguments: Optional[Dict[str, Any]],
        attempt_offset: int = 0,
    ):
        if self.journal_store is None or action_id is None:
            return None

        def observe(phase: str, attempt_no: int, result: Optional[ExecutionResult]) -> None:
            attempt_no += attempt_offset
            if phase == "STARTED":
                self.journal_store.start_action_attempt(action_id, attempt_no, arguments or {})
            elif result is not None:
                metadata = result.result.metadata if isinstance(result.result, MockActionResult) else {}
                self.journal_store.finish_action_attempt(action_id, attempt_no, result, metadata)

        return observe
