"""
IncidentRouter — 事件路由器（监督学习 + LLM 协作版本）

Metric-driven AIOps + 监督学习集成:
- AttackDetector (监督学习): 回答 "What happened?" → attack_prediction
- TriageAgent (LLM + RAG):    回答 "Why? Impact? How to fix?" → TriageResult

链路:
Incident → Normalize → Dedup → Severity
  → AttackDetector.predict() → TriageAgent → RunbookAgent
  → ActionOrchestrator → Verifier → Replanner
"""

from typing import AsyncGenerator, Dict, Any, Optional, List
from loguru import logger

from app.models.incident import (
    Incident,
    IncidentSource,
    AttackType,
    IncidentState,
    SSEEventType,
    ActionStatus,
)
from app.models.metrics import RailMetricRecord
from app.events import EventNormalizer, Deduplicator, SeverityEngine
from app.agents.triage_agent import TriageAgent
from app.agents.runbook_agent import RunbookAgent
from app.agents.action_orchestrator import ActionOrchestrator
from app.agents.verifier import Verifier
from app.agents.replanner import Replanner, ReplanAction
from app.core.state_machine import state_machine
from app.core.incident_store import incident_store
from app.core.audit_store import audit_store


class IncidentRouter:
    """
    事件路由器（监督学习 + LLM 协作版本）。

    AttackDetector 提供初步攻击分类 (What happened?)，
    TriageAgent 负责解释 + 诊断 (Why? Impact? How to fix?)。
    """

    def __init__(self, attack_detector=None):
        self.normalizer = EventNormalizer()
        self.deduplicator = Deduplicator(window_seconds=10.0, threshold=3)
        self.severity_engine = SeverityEngine()
        self.triage_agent = TriageAgent()
        self.runbook_agent = RunbookAgent()
        self.action_orchestrator = ActionOrchestrator()
        self.verifier = Verifier()
        self.replanner = Replanner()

        # 监督学习攻击检测器（默认使用 Mock）
        if attack_detector is None:
            from app.ml.attack_detector import create_attack_detector
            self.attack_detector = create_attack_detector()
            logger.info(
                "[IncidentRouter] using configured AttackDetector: "
                f"{type(self.attack_detector).__name__} "
                f"({self.attack_detector.model_version})"
            )
        else:
            self.attack_detector = attack_detector
            logger.info(f"[IncidentRouter] 使用自定义 AttackDetector: {type(attack_detector).__name__}")

    # ================================================================
    # 路由决策
    # ================================================================

    def should_use_new_link(self, raw_event: Dict[str, Any], source: IncidentSource) -> bool:
        """
        判断是否使用新链路（Metric-driven 版本）。

        Metric-driven: 所有 STSRS / Prometheus / MCP 来源都走新链路。
        MANUAL 来源根据是否提供指标数据判断。
        """
        if source in (IncidentSource.STSRS, IncidentSource.PROMETHEUS, IncidentSource.MCP):
            return True
        if source == IncidentSource.MANUAL:
            # 有指标数据或告警名 → 走新链路
            return bool(
                raw_event.get("metrics")
                or raw_event.get("metrics_snapshot")
                or raw_event.get("alertname")
            )
        return False

    @staticmethod
    def _should_process_single_metric(
        raw_event: Dict[str, Any],
        source: IncidentSource,
    ) -> bool:
        """Realtime metric submissions should produce one pipeline run per request."""
        if source not in (IncidentSource.STSRS, IncidentSource.PROMETHEUS):
            return False
        return isinstance(raw_event.get("metrics"), dict) or isinstance(
            raw_event.get("metrics_snapshot"),
            dict,
        )

    # ================================================================
    # 主路由入口
    # ================================================================

    async def route(
        self,
        raw_event: Dict[str, Any],
        source: IncidentSource,
        thread_id: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        事件路由主入口（Metric-driven 版本）。

        Step 1-3: 归一化 → 去重 → 分级（基于指标影响）
        Step 4+:  统一进入 TriageAgent 诊断 → RunbookAgent → Action → Verify → Replan
        attack_type 由 TriageAgent 基于 metrics_snapshot + KB 诊断产出
        """
        # ---- 归一化 ----
        incident = self.normalizer.normalize(raw_event, source)
        yield self._sse(incident, SSEEventType.INCIDENT_CREATED, "事件已创建", thread_id)

        # ---- 去重（含 incident_deduplicated SSE） ----
        if self._should_process_single_metric(raw_event, source):
            logger.info(
                "[IncidentRouter] real-time metrics input bypasses buffered dedup: "
                f"incident_id={incident.incident_id}"
            )
        else:
            deduped = self.deduplicator.process(incident)
            if deduped is None:
                yield {
                    "type": "status", "stage": "dedup",
                    "message": f"事件 {incident.incident_id} 暂存在去重窗口",
                    "incident_id": incident.incident_id,
                    "trace_id": incident.trace_id,
                    "thread_id": thread_id,
                }
                return
            incident = deduped

        # 如果去重合并了多条，发送 incident_deduplicated
        if incident.duplicate_count > 1:
            yield self._sse(incident, SSEEventType.INCIDENT_DEDUPLICATED,
                            f"去重合并 {incident.duplicate_count} 条事件",
                            thread_id,
                            data={
                                "duplicate_count": incident.duplicate_count,
                                "first_seen": incident.first_seen.isoformat() if incident.first_seen else None,
                                "last_seen": incident.last_seen.isoformat() if incident.last_seen else None,
                                "merged_incident_id": incident.incident_id,
                            })

        for buf_incident in self.deduplicator.drain_output():
            yield self._sse(buf_incident, SSEEventType.INCIDENT_CREATED,
                            "过期去重事件输出", thread_id)

        # ---- 分级 ----
        severity = self.severity_engine.evaluate(incident)
        yield self._sse(incident, SSEEventType.STATE_CHANGED,
                        f"严重级别: {severity.value}", thread_id)

        # ---- 监督学习攻击检测（新） ----
        # AttackDetector: 回答 "What happened?"
        if incident.metrics_snapshot:
            try:
                record_like = self._build_metric_record(incident)
                prediction = self.attack_detector.predict(record_like)
                incident.attack_prediction = prediction.model_dump()
                pred_type = incident.attack_prediction.get("attack_type", "UNKNOWN")
                pred_conf = incident.attack_prediction.get("confidence", 0.0)
                logger.info(
                    f"[IncidentRouter] AttackDetector predict: "
                    f"type={pred_type}, confidence={pred_conf:.0%}, "
                    f"model={incident.attack_prediction.get('model_version', '?')}"
                )
                yield self._sse(incident, SSEEventType.INCIDENT_TRIAGED,
                                f"AttackDetector: {pred_type} (conf={pred_conf:.0%})",
                                thread_id,
                                data={"attack_prediction": incident.attack_prediction})
            except Exception as e:
                logger.warning(f"[IncidentRouter] AttackDetector 调用失败: {e}")
                # 继续流程，TriageAgent 可独立判断

        # ---- 创建存储记录 + NEW 状态 ----
        record = incident_store.create(incident, thread_id)
        record = state_machine.transition(
            record, IncidentState.NEW,
            reason="事件创建", triggered_by="IncidentRouter",
            trace_id=incident.trace_id, thread_id=thread_id,
        )
        incident_store.update(record)

        # ---- 统一进入 TriageAgent（解释 + 诊断） ----
        # TriageAgent: 回答 "Why? Impact? How to fix?"
        async for event in self._common_pipeline(incident, thread_id, record):
            yield event

    # ================================================================
    # 通用 Pipeline（所有事件统一走此流程 — Metric-driven 简化版）
    # ================================================================

    async def _common_pipeline(
        self, incident: Incident, thread_id: str, record: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Metric-driven 统一处理流程:
        NEW → TRIAGED → PLANNED → EXECUTING → VERIFIED → RESOLVED

        TriageAgent 是核心诊断节点:
        - 输入: Incident (含 metrics_snapshot)
        - 输出: TriageResult (含 attack_type, root_cause, severity, confidence, evidence)
        - attack_type 在此阶段被确认/诊断，而非在此阶段之前已知
        含补偿/重试/升级/审批 SSE 事件
        """

        # === Step A: NEW → TRIAGED ===
        record = state_machine.transition(
            record, IncidentState.TRIAGED,
            reason="开始分诊", triggered_by="IncidentRouter",
            trace_id=incident.trace_id, thread_id=thread_id,
        )
        incident_store.update(record)
        yield self._sse(incident, SSEEventType.STATE_CHANGED,
                        f"NEW → TRIAGED", thread_id)

        triage_result = await self.triage_agent.triage(incident)
        record.triage_result = triage_result.model_dump()
        incident_store.update(record)

        # Metric-driven: TriageAgent 产出 attack_type 诊断结论
        # 如果 TriageAgent 诊断出了 attack_type，更新 Incident
        diagnosed_attack_type = triage_result.attack_type
        if diagnosed_attack_type:
            try:
                incident.attack_type = AttackType(diagnosed_attack_type)
            except ValueError:
                # 无法匹配到已知 AttackType 枚举，保持 UNKNOWN
                logger.info(
                    f"[IncidentRouter] TriageAgent 诊断 attack_type='{diagnosed_attack_type}' "
                    f"不在已知枚举中，保持 UNKNOWN"
                )

        audit_store.record(
            trace_id=incident.trace_id, incident_id=incident.incident_id,
            thread_id=thread_id, event_type=SSEEventType.INCIDENT_TRIAGED,
            actor="TriageAgent", action="triaged",
            state_from=IncidentState.NEW, state_to=IncidentState.TRIAGED,
            detail=triage_result.model_dump(),
            message=f"诊断: {triage_result.root_cause}, attack_type={diagnosed_attack_type or 'UNKNOWN'}",
        )
        yield self._sse(incident, SSEEventType.INCIDENT_TRIAGED,
                        f"诊断: {triage_result.root_cause}, "
                        f"attack_type={diagnosed_attack_type or 'UNKNOWN'}, "
                        f"conf={triage_result.confidence:.0%}",
                        thread_id, data={"triage": triage_result.model_dump()})

        # === Step B: TRIAGED → PLANNED ===
        record = state_machine.transition(
            record, IncidentState.PLANNED,
            reason="生成处置计划", triggered_by="IncidentRouter",
            trace_id=incident.trace_id, thread_id=thread_id,
        )
        incident_store.update(record)
        yield self._sse(incident, SSEEventType.STATE_CHANGED,
                        f"TRIAGED → PLANNED", thread_id)

        plan = await self.runbook_agent.generate_plan(incident, triage_result)
        record.plan = plan.steps
        incident_store.update(record)

        audit_store.record(
            trace_id=incident.trace_id, incident_id=incident.incident_id,
            thread_id=thread_id, event_type=SSEEventType.PLAN_GENERATED,
            actor="RunbookAgent", action="plan_generated",
            state_from=IncidentState.TRIAGED, state_to=IncidentState.PLANNED,
            detail=plan.model_dump(),
            message=f"计划: {len(plan.steps)} 步骤, KB={plan.source_kb}",
        )
        yield self._sse(incident, SSEEventType.PLAN_GENERATED,
                        f"计划: {len(plan.steps)} 步骤, KB={plan.source_kb}",
                        thread_id, data={"plan": plan.model_dump()})

        # === 审批事件（如需要） ===
        if plan.requires_approval:
            for app_action in plan.approval_actions:
                yield self._sse(incident, SSEEventType.APPROVAL_REQUIRED,
                                f"需要审批: {app_action.value}",
                                thread_id,
                                data={"action": app_action.value,
                                      "timeout_minutes": 10,
                                      "reason": f"高风险操作: {app_action.value}"})

        # === Step C: PLANNED → EXECUTING ===
        record = state_machine.transition(
            record, IncidentState.EXECUTING,
            reason="开始执行", triggered_by="IncidentRouter",
            trace_id=incident.trace_id, thread_id=thread_id,
        )
        incident_store.update(record)
        yield self._sse(incident, SSEEventType.STATE_CHANGED,
                        f"PLANNED → EXECUTING", thread_id)

        results = await self.action_orchestrator.execute_plan(incident, plan, thread_id)
        record.execution_results = [r.model_dump() for r in results]
        incident_store.update(record)

        for i, r in enumerate(results):
            yield self._sse(incident, SSEEventType.ACTION_EXECUTED,
                            f"动作 {i+1}/{len(results)}: {r.action_name} → {'✓' if r.success else '✗'}",
                            thread_id, data={"action_result": r.model_dump()})

        # === Step D: 验证 ===
        verification = await self.verifier.verify(incident, plan, results, thread_id)
        record.verification_result = verification.model_dump()
        incident_store.update(record)
        yield self._sse(incident, SSEEventType.VERIFICATION_FINISHED,
                        f"验证: {verification.action_status.value}", thread_id,
                        data={"verification": verification.model_dump()})

        # === Step E: Replanner 循环（含补偿/重试/升级） ===
        retry_cycle = 0
        max_cycles = 3

        while retry_cycle < max_cycles:
            decision = await self.replanner.decide(
                incident.incident_id, thread_id, incident.trace_id, verification
            )

            if decision == ReplanAction.RESOLVE:
                record = state_machine.transition(
                    record, IncidentState.VERIFIED,
                    reason="验证通过", triggered_by="Replanner",
                    trace_id=incident.trace_id, thread_id=thread_id,
                )
                incident_store.update(record)
                yield self._sse(incident, SSEEventType.STATE_CHANGED,
                                f"EXECUTING → VERIFIED", thread_id)

                record = state_machine.transition(
                    record, IncidentState.RESOLVED,
                    reason="所有动作验证通过", triggered_by="Replanner",
                    trace_id=incident.trace_id, thread_id=thread_id,
                )
                incident_store.update(record)
                yield self._sse(incident, SSEEventType.INCIDENT_RESOLVED,
                                "事件已解决", thread_id)
                yield self._sse(incident, SSEEventType.STATE_CHANGED,
                                f"VERIFIED → RESOLVED", thread_id)
                break

            elif decision == ReplanAction.RETRY:
                yield self._sse(incident, SSEEventType.RETRY_SCHEDULED,
                                f"重试第 {retry_cycle+1} 轮", thread_id)
                results = await self.action_orchestrator.execute_plan(incident, plan, thread_id)
                verification = await self.verifier.verify(
                    incident, plan, results, thread_id, retry_cycle=retry_cycle + 1,
                )
                retry_cycle += 1
                continue

            elif decision == ReplanAction.COMPENSATE:
                # ---- 补偿开始 SSE ----
                yield self._sse(incident, SSEEventType.COMPENSATION_STARTED,
                                "开始补偿流程", thread_id,
                                data={"failed_actions": verification.compensation_actions})
                record = state_machine.transition(
                    record, IncidentState.COMPENSATING,
                    reason="触发补偿", triggered_by="Replanner",
                    trace_id=incident.trace_id, thread_id=thread_id,
                )
                incident_store.update(record)
                yield self._sse(incident, SSEEventType.STATE_CHANGED,
                                f"EXECUTING → COMPENSATING", thread_id)

                try:
                    comp_results = await self.action_orchestrator.execute_compensation(
                        incident, plan, results, thread_id,
                    )

                    # ---- 补偿动作 SSE ----
                    for cr in comp_results:
                        yield self._sse(incident, SSEEventType.COMPENSATION_ACTION_EXECUTED,
                                        f"补偿动作: {cr.action_name} → {'✓' if cr.success else '✗'}",
                                        thread_id, data={"compensation_result": cr.model_dump()})

                    comp_verification = await self.verifier.verify_compensation(
                        incident, comp_results, thread_id,
                    )
                except Exception as comp_err:
                    logger.error(f"[IncidentRouter] 补偿执行异常: {comp_err}", exc_info=True)
                    record = state_machine.transition(
                        record, IncidentState.FAILED,
                        reason=f"补偿执行异常: {str(comp_err)}",
                        triggered_by="Replanner",
                        trace_id=incident.trace_id, thread_id=thread_id,
                    )
                    incident_store.update(record)
                    yield self._sse(incident, SSEEventType.INCIDENT_FAILED,
                                    f"补偿执行异常: {str(comp_err)}", thread_id)
                    break

                if comp_verification.action_status == ActionStatus.SUCCESS:
                    # ---- 补偿完成 SSE ----
                    yield self._sse(incident, SSEEventType.COMPENSATION_COMPLETED,
                                    "补偿成功", thread_id)
                    record = state_machine.transition(
                        record, IncidentState.VERIFIED,
                        reason="补偿成功", triggered_by="Replanner",
                        trace_id=incident.trace_id, thread_id=thread_id,
                    )
                    record = state_machine.transition(
                        record, IncidentState.RESOLVED,
                        reason="补偿后解决", triggered_by="Replanner",
                        trace_id=incident.trace_id, thread_id=thread_id,
                    )
                    incident_store.update(record)
                    yield self._sse(incident, SSEEventType.INCIDENT_RESOLVED,
                                    "补偿成功，已解决", thread_id)
                    yield self._sse(incident, SSEEventType.STATE_CHANGED,
                                    f"COMPENSATING → VERIFIED → RESOLVED", thread_id)
                else:
                    yield self._sse(incident, SSEEventType.COMPENSATION_COMPLETED,
                                    "补偿失败", thread_id)
                    record = state_machine.transition(
                        record, IncidentState.FAILED,
                        reason="补偿失败", triggered_by="Replanner",
                        trace_id=incident.trace_id, thread_id=thread_id,
                    )
                    incident_store.update(record)
                    yield self._sse(incident, SSEEventType.INCIDENT_FAILED,
                                    "补偿失败", thread_id)
                break

            elif decision == ReplanAction.ESCALATE:
                record = state_machine.transition(
                    record, IncidentState.ESCALATED,
                    reason="升级至人工处理", triggered_by="Replanner",
                    trace_id=incident.trace_id, thread_id=thread_id,
                )
                incident_store.update(record)
                yield self._sse(incident, SSEEventType.INCIDENT_ESCALATED,
                                "已升级人工处理", thread_id)
                yield self._sse(incident, SSEEventType.STATE_CHANGED,
                                f"→ ESCALATED", thread_id)
                break

            elif decision == ReplanAction.FAIL:
                record = state_machine.transition(
                    record, IncidentState.FAILED,
                    reason="处理失败", triggered_by="Replanner",
                    trace_id=incident.trace_id, thread_id=thread_id,
                )
                incident_store.update(record)
                yield self._sse(incident, SSEEventType.INCIDENT_FAILED,
                                "事件处理失败", thread_id)
                yield self._sse(incident, SSEEventType.STATE_CHANGED,
                                f"→ FAILED", thread_id)
                break

        # === 最终输出 ===
        final_record = incident_store.get(incident.incident_id)
        final_state = final_record.state if final_record else IncidentState.FAILED

        # AIOPS-FUSION: 生成 FailureContext（如果需要恢复）
        failure_context = None
        if final_state in (IncidentState.FAILED, IncidentState.ESCALATED):
            from app.models.incident import FailureContext
            failure_context = FailureContext(
                incident_id=incident.incident_id,
                thread_id=thread_id,
                trace_id=incident.trace_id,
                failure_reason=(
                    verification.reason if verification else
                    f"Workflow 进入终态: {final_state.value}"
                ),
                failure_category=self._categorize_failure(verification, results),
                executed_actions=[r.model_dump() for r in results],
                triage_result=triage_result.model_dump() if triage_result else None,
                plan_steps=plan.steps if plan else [],
                current_state=final_state.value,
                retry_cycles=retry_cycle,
                history=[
                    f"状态迁移: {t.from_state.value} → {t.to_state.value}"
                    for t in (final_record.state_history[-10:] if final_record else [])
                ],
            ).model_dump()

        yield {
            "type": "complete",
            "event_type": SSEEventType.COMPLETE.value if hasattr(SSEEventType, 'COMPLETE') else "complete",
            "stage": "complete",
            "message": f"事件处理完成，最终状态: {final_state.value}",
            "incident_id": incident.incident_id,
            "trace_id": incident.trace_id,
            "thread_id": thread_id,
            "final_state": final_state.value,
            "workflow_failed": final_state in (IncidentState.FAILED, IncidentState.ESCALATED),
            "failure_context": failure_context,
            "triage": triage_result.model_dump(),
            "plan": plan.model_dump(),
            "execution_results": [r.model_dump() for r in results],
            "verification": verification.model_dump(),
            "event_sequence": audit_store._global_sequence,
        }

    # ================================================================
    # 监督学习检测辅助方法
    # ================================================================

    @staticmethod
    def _build_metric_record(incident: Incident) -> RailMetricRecord:
        """
        从 Incident 的 metrics_snapshot 重建 RailMetricRecord。

        AttackDetector.predict() 需要 RailMetricRecord 输入，
        此方法在 Event Driven Pipeline 中桥接 Incident → RailMetricRecord。

        Returns:
            从 metrics_snapshot 重建的 RailMetricRecord（可能字段不完整）
        """
        from datetime import datetime as dt
        from app.models.metrics import RailMetrics

        snapshot = incident.metrics_snapshot or {}

        # 提取 metrics
        metrics_data = {}
        raw_metrics = snapshot.get("metrics", {})
        if isinstance(raw_metrics, dict):
            for field_name in RailMetrics.model_fields:
                if field_name in raw_metrics and raw_metrics[field_name] is not None:
                    metrics_data[field_name] = raw_metrics[field_name]

        metrics = RailMetrics(**metrics_data) if metrics_data else RailMetrics()

        # 提取 source_metrics
        source_metrics = snapshot.get("source_metrics", {})

        # 提取基础字段
        train_id = snapshot.get("train_id") or (incident.metadata.train_id or "")
        signal_id = snapshot.get("signal_id") or (incident.metadata.signal_id or "")

        # 解析时间戳
        ts_raw = snapshot.get("timestamp", "")
        try:
            timestamp = dt.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            timestamp = incident.timestamp or dt.utcnow()

        return RailMetricRecord(
            record_id=snapshot.get("record_id", ""),
            timestamp=timestamp,
            train_id=str(train_id),
            signal_id=str(signal_id),
            metrics=metrics,
            source_metrics=source_metrics if isinstance(source_metrics, dict) else {},
            source_files=snapshot.get("source_files", []) if isinstance(snapshot.get("source_files"), list) else [],
        )

    # ================================================================
    # SSE 辅助
    # ================================================================

    def _sse(
        self, incident: Incident, event_type: SSEEventType,
        message: str, thread_id: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """构建统一的 SSE 事件字典（含全链路 Trace 字段）"""
        return {
            "type": event_type.value,
            "trace_id": incident.trace_id,
            "incident_id": incident.incident_id,
            "thread_id": thread_id,
            "message": message,
            "data": data or {},
        }

    @staticmethod
    def _categorize_failure(
        verification: Optional[Any],
        results: List[Any],
    ) -> str:
        """
        AIOPS-FUSION: 将 Workflow 失败归类。

        失败条件:
        - Action 执行失败 → action_failed
        - 所有 Action 超时 → timeout
        - 重试耗尽 → retry_exhausted
        - 补偿失败 → compensation_failed
        - Runbook 匹配失败 → runbook_match_failed
        - MCP Tool 失败 → mcp_tool_failed
        - 未知 → unknown
        """
        if not results:
            return "runbook_match_failed"

        all_failed = all(not r.get("success", False) if isinstance(r, dict) else not r.success for r in results)
        has_timeout = any(
            (isinstance(r, dict) and r.get("error_type") == "timeout") or
            (hasattr(r, 'error_type') and r.error_type == "timeout")
            for r in results
        )
        has_escalated = any(
            (isinstance(r, dict) and r.get("error_type") in ("escalated", "circuit_open")) or
            (hasattr(r, 'error_type') and r.error_type in ("escalated", "circuit_open"))
            for r in results
        )

        if has_escalated:
            return "retry_exhausted"
        if has_timeout:
            return "timeout"
        if all_failed:
            return "action_failed"
        if verification and hasattr(verification, 'compensation_needed') and verification.compensation_needed:
            return "compensation_failed"

        return "unknown"
