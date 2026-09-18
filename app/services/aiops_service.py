"""
Event-driven AIOps service.

Public architecture:
  IncidentRouter -> TriageAgent -> RunbookAgent
  -> ActionOrchestrator -> Verifier -> Replanner

Internal recovery architecture:
  Planner -> Executor -> Replanner

The Plan-Execute-Replan graph is preserved only as a recovery strategy that is
triggered after the incident workflow fails. There is no public entrypoint that
can directly start the legacy workflow anymore.
"""

from typing import Any, AsyncGenerator, Dict, Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from loguru import logger

from app.agent.aiops import PlanExecuteState, executor, planner, replanner
from app.core.audit_store import audit_store
from app.core.incident_router import IncidentRouter
from app.core.incident_store import incident_store
from app.core.recovery import recover_pending_workflows
from app.models.incident import (
    MAX_RECOVERY_ATTEMPTS,
    FailureContext,
    IncidentSource,
    IncidentState,
    RecoveryState,
    SSEEventType,
)


NODE_PLANNER = "planner"
NODE_EXECUTOR = "executor"
NODE_REPLANNER = "replanner"


class AIOpsService:
    """Event-driven incident workflow plus an internal PRP recovery engine."""

    def __init__(self) -> None:
        self.router = IncidentRouter()
        self.recovery_checkpointer = MemorySaver()
        self.recovery_graph = self._build_recovery_graph()

        logger.info(
            "AIOpsService initialized "
            "(incident workflow + internal PRP recovery + safety control)"
        )

    async def process_incident(
        self,
        raw_event: Dict[str, Any],
        source: IncidentSource = IncidentSource.MANUAL,
        session_id: str = "default",
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Run the event-driven incident workflow.

        If the workflow reports a failure context, switch to the internal
        PRP recovery engine. If recovery also fails, run safety control.
        """
        thread_id = f"thread-{session_id}"
        logger.info(
            f"[AIOpsService] process_incident: session={session_id}, "
            f"source={source.value}, thread={thread_id}"
        )

        workflow_failed = False
        failure_context: Optional[Dict[str, Any]] = None
        incident_id: Optional[str] = None

        try:
            async for event in self.router.route(raw_event, source, thread_id):
                if event.get("incident_id"):
                    incident_id = event["incident_id"]

                if event.get("type") == "complete":
                    workflow_failed = event.get("workflow_failed", False)
                    failure_context = event.get("failure_context")

                    if workflow_failed and failure_context:
                        logger.warning(
                            "[AIOpsService] workflow failed: "
                            f"incident_id={incident_id}, "
                            f"category={failure_context.get('failure_category')}"
                        )
                        yield {
                            "type": "workflow_failed",
                            "stage": "workflow_failed",
                            "message": (
                                "Event driven workflow failed: "
                                f"{failure_context.get('failure_reason', 'unknown')}"
                            ),
                            "incident_id": incident_id,
                            "thread_id": thread_id,
                            "failure_category": failure_context.get("failure_category"),
                            "data": failure_context,
                        }
                        continue

                    yield event
                    return

                yield event

            if workflow_failed and failure_context:
                yield {
                    "type": "recovery_start",
                    "stage": "recovery_start",
                    "message": "Entering recovery mode and starting PRP engine",
                    "incident_id": incident_id,
                    "thread_id": thread_id,
                    "data": {"recovery_state": RecoveryState.RECOVERY_STARTED.value},
                }

                recovery_success = False
                async for recovery_event in self._execute_recovery(
                    failure_context=failure_context,
                    session_id=f"{session_id}-recovery",
                ):
                    recovery_event["stage"] = (
                        f"recovery:{recovery_event.get('stage', 'unknown')}"
                    )
                    yield recovery_event

                    if recovery_event.get("type") == "complete":
                        recovery_result = recovery_event.get("recovery_result", "")
                        recovery_success = recovery_result == "recovery_success"
                        break

                if not recovery_success:
                    logger.error(
                        "[AIOpsService] recovery failed; entering safety control: "
                        f"incident_id={incident_id}"
                    )
                    async for safety_event in self._execute_safety_control(
                        incident_id=incident_id or "unknown",
                        thread_id=thread_id,
                        failure_context=failure_context,
                    ):
                        safety_event["stage"] = (
                            f"safety:{safety_event.get('stage', 'unknown')}"
                        )
                        yield safety_event
                else:
                    if incident_id:
                        record = incident_store.get(incident_id)
                        if record:
                            from app.core.state_machine import state_machine

                            try:
                                record = state_machine.transition(
                                    record,
                                    IncidentState.RESOLVED,
                                    reason="PRP recovery succeeded",
                                    triggered_by="AIOpsService",
                                    trace_id=failure_context.get("trace_id", ""),
                                    thread_id=thread_id,
                                )
                                incident_store.update(record)
                            except ValueError:
                                pass

                final_record = incident_store.get(incident_id) if incident_id else None
                final_state = final_record.state if final_record else IncidentState.FAILED
                yield {
                    "type": "complete",
                    "stage": "complete",
                    "message": f"Incident handling finished with state {final_state.value}",
                    "incident_id": incident_id,
                    "thread_id": thread_id,
                    "final_state": final_state.value,
                    "recovery_attempted": workflow_failed,
                    "recovery_success": recovery_success,
                }

        except Exception as e:
            logger.error(f"[AIOpsService] incident workflow error: {e}", exc_info=True)
            yield {
                "type": "error",
                "stage": "error",
                "message": f"Incident processing failed: {str(e)}",
            }

    async def recover_pending_workflows(self):
        """Explicit Phase 5 recovery entry point; callers decide when to invoke it."""
        return await recover_pending_workflows(incident_store)

    async def process_stsrs(
        self,
        stsrs_data: Dict[str, Any],
        session_id: str = "default",
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """STSRS-specific entrypoint that still uses the incident workflow."""
        if "source" not in stsrs_data:
            stsrs_data["source"] = "stsrs"

        async for event in self.process_incident(
            stsrs_data,
            source=IncidentSource.STSRS,
            session_id=session_id,
        ):
            yield event

    async def process_metrics(
        self,
        metrics_payload: Dict[str, Any],
        session_id: str = "default",
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Metric-driven entrypoint that normalizes raw metrics into incidents."""
        logger.info(
            f"[AIOpsService] process_metrics: session={session_id}, "
            f"train={metrics_payload.get('train_id', 'N/A')}, "
            f"signal={metrics_payload.get('signal_id', 'N/A')}"
        )

        raw_event = {
            "metrics_snapshot": metrics_payload,
            "train_id": metrics_payload.get(
                "train_id", metrics_payload.get("labels", {}).get("train", "")
            ),
            "signal_id": metrics_payload.get(
                "signal_id", metrics_payload.get("labels", {}).get("signal", "")
            ),
            "description": (
                "Metric event: "
                f"train={metrics_payload.get('train_id', 'N/A')}, "
                f"signal={metrics_payload.get('signal_id', 'N/A')}"
            ),
        }

        if "source_files" in metrics_payload:
            raw_event["source_files"] = metrics_payload["source_files"]
        if "record_id" in metrics_payload:
            raw_event["record_id"] = metrics_payload["record_id"]

        async for event in self.process_incident(
            raw_event,
            source=IncidentSource.STSRS,
            session_id=session_id,
        ):
            yield event

    async def _execute_recovery(
        self,
        failure_context: Dict[str, Any],
        session_id: str = "default",
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Run the internal Plan-Execute-Replan recovery strategy."""
        logger.info(f"[Recovery] PRP engine start: session={session_id}")

        try:
            fc = FailureContext(**failure_context)
            planner_input = fc.to_planner_input()

            initial_state: PlanExecuteState = {
                "input": planner_input,
                "plan": [],
                "past_steps": [],
                "response": "",
                "is_recovery": True,
                "recovery_attempt": 0,
                "failure_context": failure_context,
                "recovery_result": "",
            }

            config_dict = {"configurable": {"thread_id": session_id}}

            async for event in self.recovery_graph.astream(
                input=initial_state,
                config=config_dict,
                stream_mode="updates",
            ):
                for node_name, node_output in event.items():
                    if node_name == NODE_PLANNER:
                        formatted = self._format_planner_event(node_output)
                        formatted["stage"] = "recovery_plan"
                        yield formatted
                    elif node_name == NODE_EXECUTOR:
                        formatted = self._format_executor_event(node_output)
                        formatted["stage"] = "recovery_execute"
                        yield formatted
                    elif node_name == NODE_REPLANNER:
                        formatted = self._format_replanner_event(node_output)
                        formatted["stage"] = "recovery_replan"
                        yield formatted

            final_state = self.recovery_graph.get_state(config_dict)
            recovery_result = "recovery_failed"
            final_response = ""

            if final_state and final_state.values:
                final_response = final_state.values.get("response", "")
                recovery_result = final_state.values.get(
                    "recovery_result", "recovery_failed"
                )

            if not recovery_result and final_response:
                recovery_result = "recovery_success"

            logger.info(f"[Recovery] PRP engine finished: result={recovery_result}")
            yield {
                "type": "complete",
                "stage": "recovery_complete",
                "message": f"Recovery engine finished: {recovery_result}",
                "response": final_response,
                "recovery_result": recovery_result,
            }

        except Exception as e:
            logger.error(f"[Recovery] PRP engine error: {e}", exc_info=True)
            yield {
                "type": "error",
                "stage": "recovery_error",
                "message": f"Recovery failed: {str(e)}",
                "recovery_result": "recovery_failed",
            }

    async def _execute_safety_control(
        self,
        incident_id: str,
        thread_id: str,
        failure_context: Dict[str, Any],
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Rollback and escalate when both workflow and recovery fail."""
        logger.warning(
            f"[SafetyControl] starting safety control: incident_id={incident_id}"
        )

        yield {
            "type": "safety_rollback",
            "stage": "safety_rollback",
            "message": "Safety control started rollback actions",
            "incident_id": incident_id,
            "thread_id": thread_id,
            "data": {"recovery_state": RecoveryState.SAFETY_ROLLBACK.value},
        }

        rollback_results = []
        try:
            from app.agents.action_orchestrator import ActionOrchestrator
            from app.models.incident import (
                ActionInstruction,
                AttackType,
                Incident,
                IncidentMetadata,
                Severity,
            )

            orchestrator = ActionOrchestrator()
            rollback_actions = [
                ActionInstruction(
                    action="rollback_block_suspicious_source", description="解除 IP 封禁"
                ),
                ActionInstruction(
                    action="rollback_switch_backup_link", description="恢复原始链路"
                ),
                ActionInstruction(action="verify_network_health", description="验证网络健康"),
            ]
            temp_incident = Incident(
                incident_id=incident_id,
                attack_type=AttackType.UNKNOWN,
                severity=Severity.P1,
                metadata=IncidentMetadata(),
                description=f"Safety Control Rollback for {incident_id}",
            )

            for index, instruction in enumerate(rollback_actions, start=1):
                action_name = instruction.action.value
                step = instruction.description or action_name
                result = await orchestrator._execute_single_action(
                    action_name, temp_incident, thread_id, step, instruction.arguments
                )
                rollback_results.append(result)
                yield {
                    "type": "safety_rollback_action",
                    "stage": "safety_rollback",
                    "message": (
                        f"Rollback {index}/{len(rollback_actions)}: {action_name} -> "
                        f"{'success' if result.success else 'failed'}"
                    ),
                    "incident_id": incident_id,
                    "thread_id": thread_id,
                    "data": {
                        "action_name": action_name,
                        "success": result.success,
                        "message": result.message,
                    },
                }

        except Exception as e:
            logger.error(f"[SafetyControl] rollback error: {e}", exc_info=True)
            yield {
                "type": "safety_rollback_error",
                "stage": "safety_rollback",
                "message": f"Rollback failed: {str(e)}",
                "incident_id": incident_id,
            }

        yield {
            "type": "safety_escalation",
            "stage": "safety_escalation",
            "message": "Safety control escalated to manual handling",
            "incident_id": incident_id,
            "thread_id": thread_id,
            "data": {"recovery_state": RecoveryState.SAFETY_ESCALATION.value},
        }

        try:
            record = incident_store.get(incident_id)
            if record:
                from app.core.state_machine import state_machine

                record = state_machine.transition(
                    record,
                    IncidentState.ESCALATED,
                    reason="Workflow and recovery both failed; escalate to manual handling",
                    triggered_by="AIOpsService.SafetyControl",
                    trace_id=failure_context.get("trace_id", ""),
                    thread_id=thread_id,
                )
                incident_store.update(record)
        except Exception as e:
            logger.error(f"[SafetyControl] state update error: {e}")

        audit_store.record(
            trace_id=failure_context.get("trace_id", ""),
            incident_id=incident_id,
            thread_id=thread_id,
            event_type=SSEEventType.INCIDENT_ESCALATED,
            actor="AIOpsService.SafetyControl",
            action="safety_escalation",
            detail={
                "failure_context": failure_context,
                "rollback_attempted": True,
                "rollback_results": [
                    {
                        "action_name": result.action_name,
                        "success": result.success,
                        "message": result.message,
                    }
                    for result in rollback_results
                ]
                if rollback_results
                else [],
            },
            message="Safety control escalated the incident after rollback attempts",
        )

        yield {
            "type": "safety_escalation_complete",
            "stage": "safety_escalation",
            "message": "Safety control finished rollback and escalation",
            "incident_id": incident_id,
            "thread_id": thread_id,
            "data": {
                "recovery_state": RecoveryState.SAFETY_ESCALATION.value,
                "rollback_count": len(rollback_results),
            },
        }

    def _build_recovery_graph(self) -> CompiledStateGraph:
        """Build the internal PRP graph used by recovery."""
        logger.info("Building internal PRP recovery graph...")

        workflow = StateGraph(PlanExecuteState)
        workflow.add_node(NODE_PLANNER, planner)
        workflow.add_node(NODE_EXECUTOR, executor)
        workflow.add_node(NODE_REPLANNER, replanner)
        workflow.set_entry_point(NODE_PLANNER)
        workflow.add_edge(NODE_PLANNER, NODE_EXECUTOR)
        workflow.add_edge(NODE_EXECUTOR, NODE_REPLANNER)

        def should_continue(state: PlanExecuteState) -> str:
            is_recovery = state.get("is_recovery", False)
            recovery_attempt = state.get("recovery_attempt", 0)

            if state.get("response"):
                recovery_result = state.get("recovery_result", "")
                if is_recovery and recovery_result == "recovery_failed":
                    return END
                return END

            if state.get("plan", []):
                return NODE_EXECUTOR

            if is_recovery and recovery_attempt >= MAX_RECOVERY_ATTEMPTS:
                return END

            return END

        workflow.add_conditional_edges(
            NODE_REPLANNER,
            should_continue,
            {NODE_EXECUTOR: NODE_EXECUTOR, END: END},
        )

        return workflow.compile(checkpointer=self.recovery_checkpointer)

    async def subscribe_sse(
        self,
        thread_id: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Stream audit events for the given thread."""
        async for event in audit_store.subscribe_generator(thread_id):
            yield event

    def _format_planner_event(self, state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not state:
            return {"type": "status", "stage": "planner", "message": "Planner running"}

        plan = state.get("plan", [])
        return {
            "type": "plan",
            "stage": "plan_created",
            "message": f"Plan created with {len(plan)} steps",
            "plan": plan,
        }

    def _format_executor_event(self, state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not state:
            return {"type": "status", "stage": "executor", "message": "Executor running"}

        plan = state.get("plan", [])
        past_steps = state.get("past_steps", [])
        if past_steps:
            last_step, _ = past_steps[-1]
            return {
                "type": "step_complete",
                "stage": "step_executed",
                "message": (
                    f"Step finished ({len(past_steps)}/{len(past_steps) + len(plan)})"
                ),
                "current_step": last_step,
                "remaining_steps": len(plan),
            }

        return {
            "type": "status",
            "stage": "executor",
            "message": "Executor starting current step",
        }

    def _format_replanner_event(
        self, state: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        if not state:
            return {
                "type": "status",
                "stage": "replanner",
                "message": "Replanner running",
            }

        response = state.get("response", "")
        plan = state.get("plan", [])
        recovery_result = state.get("recovery_result", "")

        if recovery_result:
            return {
                "type": "report",
                "stage": "final_report",
                "message": f"Recovery result: {recovery_result}",
                "report": response,
                "recovery_result": recovery_result,
                "remaining_steps": len(plan),
            }

        if response:
            return {
                "type": "report",
                "stage": "final_report",
                "message": "Final report generated",
                "report": response,
            }

        return {
            "type": "status",
            "stage": "replanner",
            "message": (
                "Replanner finished evaluation; "
                + ("continue execution" if plan else "prepare final response")
            ),
            "remaining_steps": len(plan),
        }


aiops_service = AIOpsService()
