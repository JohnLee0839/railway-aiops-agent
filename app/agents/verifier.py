"""
Verifier — 验证 Mock 执行结果

职责:
- 验证 Mock 执行结果
- 判断动作是否生效
- 输出结果类型: SUCCESS / RETRY / COMPENSATE / ESCALATE
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional
from loguru import logger

from app.models.incident import (
    Incident,
    RunbookPlan,
    MockActionResult,
    VerificationResult,
    ActionStatus,
    IncidentState,
    SSEEventType,
    ExecutionOutcome,
    StateVerificationResult,
)
from app.core.audit_store import audit_store
from app.tools.mock_actions import get_action_metadata


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

        if not results:
            return self._make_result(
                incident,
                thread_id,
                action_status=ActionStatus.FAILED,
                next_state=IncidentState.FAILED,
                reason="No execution results available for verification",
            )

        def outcome_of(result: MockActionResult) -> Optional[str]:
            """兼容旧结果的字符串字段和新 ExecutionOutcome 枚举。"""
            outcome = result.outcome
            return outcome.value if isinstance(outcome, ExecutionOutcome) else outcome

        outcomes = [outcome_of(result) for result in results]
        state_checks = await self._verify_business_states(plan, results)
        evidence = [item for check in state_checks for item in check.evidence]

        if any(check.error_type for check in state_checks):
            return self._make_result(
                incident,
                thread_id,
                action_status=ActionStatus.ESCALATE,
                next_state=IncidentState.ESCALATED,
                reason="状态探针未能完成，无法安全确认业务目标",
                evidence=evidence,
            )

        mismatches = [check for check in state_checks if not check.verified]
        if mismatches:
            confirmed_side_effects = self._confirmed_side_effects(results)
            if confirmed_side_effects:
                return self._make_result(
                    incident,
                    thread_id,
                    action_status=ActionStatus.COMPENSATE,
                    next_state=IncidentState.COMPENSATING,
                    reason="业务目标未达成，但存在已确认副作用，触发安全补偿",
                    compensation_needed=True,
                    compensation_actions=[
                        result.action_name for result in confirmed_side_effects
                    ],
                    evidence=evidence,
                )
            # A known retryable command failure may still be retried; the probe
            # confirms that the target is not yet reached but does not consume
            # the explicit retry authorization from TimeoutManager.
            can_retry_failed_actions = any(
                not result.success
                and result.retryable is True
                and not result.retry_exhausted
                for result in results
            )
            if not can_retry_failed_actions:
                return self._make_result(
                    incident,
                    thread_id,
                    action_status=ActionStatus.FAILED,
                    next_state=IncidentState.FAILED,
                    reason="动作执行结局与观测到的业务状态不一致",
                    evidence=evidence,
                )

        verified_indexes = {
            index for index, check in enumerate(state_checks) if check.verifier_name
        }
        success_count = sum(
            1
            for index, (result, outcome) in enumerate(zip(results, outcomes))
            if index in verified_indexes
            or (result.success and outcome in (None, ExecutionOutcome.SUCCESS.value))
        )
        failure_count = len(results) - success_count

        # === 情况 1: 每个动作都已由状态探针或明确执行结果确认 ===
        if success_count == len(results):
            return self._make_result(
                incident, thread_id,
                action_status=ActionStatus.SUCCESS,
                next_state=IncidentState.VERIFIED,
                reason=f"全部 {success_count} 个动作的业务目标已确认",
                evidence=evidence,
            )

        # === 情况 2: 无法确认下游结局，禁止自动重放或回滚 ===
        if any(
            outcome in (ExecutionOutcome.UNKNOWN.value, ExecutionOutcome.RESPONSE_LOST.value)
            or result.error_type == "unknown"
            or (result.error_type == "escalated" and outcome != ExecutionOutcome.FAILED.value)
            for index, (result, outcome) in enumerate(zip(results, outcomes))
            if index not in verified_indexes
        ):
            return self._make_result(
                incident, thread_id,
                action_status=ActionStatus.ESCALATE,
                next_state=IncidentState.ESCALATED,
                reason="存在 UNKNOWN/RESPONSE_LOST，无法确认副作用，需人工对账",
                evidence=evidence,
            )

        # === 情况 3: 请求未发送，只能依据明确的 retryable 字段决定 ===
        not_sent = [
            result for result, outcome in zip(results, outcomes)
            if outcome == ExecutionOutcome.NOT_SENT.value or result.error_type == "circuit_open"
        ]
        if not_sent:
            if (
                retry_cycle < self.max_retry_cycles
                and any(result.retryable is True and not result.retry_exhausted for result in not_sent)
            ):
                return self._make_result(
                    incident, thread_id,
                    action_status=ActionStatus.RETRY,
                    next_state=IncidentState.EXECUTING,
                    reason="动作未发送且明确可重试，建议重新执行",
                    retry_recommended=True,
                    evidence=evidence,
                )
            return self._make_result(
                incident, thread_id,
                action_status=ActionStatus.FAILED,
                next_state=IncidentState.FAILED,
                reason="动作未发送且没有可用的重试授权",
                evidence=evidence,
            )

        # === 情况 4: 仅对明确标记为可重试且尚未耗尽的动作重试 ===
        retryable_failures = [
            result for result in results
            if not result.success and result.retryable is True and not result.retry_exhausted
        ]
        if retryable_failures and retry_cycle < self.max_retry_cycles:
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
                evidence=evidence,
            )

        # === 情况 5: 只有已经确认产生副作用且有回滚定义，才允许补偿 ===
        confirmed_side_effects = self._confirmed_side_effects(results)
        if confirmed_side_effects:
            compensation_actions = [
                result.action_name for result in confirmed_side_effects
            ]

            return self._make_result(
                incident, thread_id,
                action_status=ActionStatus.COMPENSATE,
                next_state=IncidentState.COMPENSATING,
                reason="存在已确认副作用且定义了回滚动作，触发安全补偿",
                compensation_needed=True,
                compensation_actions=compensation_actions,
                evidence=evidence,
            )

        # === 情况 6: 明确失败、不可重试或动作级重试已耗尽 ===
        return self._make_result(
            incident, thread_id,
            action_status=ActionStatus.FAILED,
            next_state=IncidentState.FAILED,
            reason="动作执行失败，且没有明确的计划级重试或安全补偿条件",
            evidence=evidence,
        )

    @staticmethod
    def _confirmed_side_effects(results: List[MockActionResult]) -> List[MockActionResult]:
        return [
            result
            for result in results
            if result.side_effect_confirmed
            and get_action_metadata(result.action_name)
            and get_action_metadata(result.action_name).rollback_action
        ]

    async def _verify_business_states(
        self,
        plan: RunbookPlan,
        results: List[MockActionResult],
    ) -> List[StateVerificationResult]:
        """Run registered read-only probes and compare their observations to expectations."""
        checks: List[StateVerificationResult] = []
        for index, result in enumerate(results):
            metadata = get_action_metadata(result.action_name)
            is_planned_action = any(
                instruction.action.value == result.action_name for instruction in plan.actions
            )
            if not is_planned_action or metadata is None or metadata.state_verifier is None:
                checks.append(StateVerificationResult(verified=True))
                continue

            arguments = self._action_arguments(plan, result)
            target = self._target_for(result, arguments)
            expected = metadata.expected_state(arguments) if metadata.expected_state else {}
            verifier_name = metadata.state_verifier.__name__
            try:
                observed = await asyncio.wait_for(
                    asyncio.to_thread(metadata.state_verifier, target),
                    timeout=metadata.verifier_timeout_seconds,
                )
                if not isinstance(observed, dict):
                    raise TypeError("state verifier must return a dictionary")
                verified = all(observed.get(key) == value for key, value in expected.items())
                checked_at = datetime.utcnow()
                evidence = [
                    f"action={result.action_name}",
                    f"target={target}",
                    f"verifier={verifier_name}",
                    f"checked_at={checked_at.isoformat()}",
                ]
                evidence.extend(f"expected_{key}={value}" for key, value in expected.items())
                evidence.extend(f"observed_{key}={value}" for key, value in observed.items())
                checks.append(
                    StateVerificationResult(
                        verified=verified,
                        target=target,
                        expected_state=expected,
                        observed_state=observed,
                        verifier_name=verifier_name,
                        checked_at=checked_at,
                        evidence=evidence,
                    )
                )
                if verified:
                    # A successful read-back is the authoritative confirmation of this side effect.
                    result.side_effect_confirmed = True
            except asyncio.TimeoutError:
                checked_at = datetime.utcnow()
                checks.append(
                    StateVerificationResult(
                        verified=False,
                        target=target,
                        expected_state=expected,
                        verifier_name=verifier_name,
                        checked_at=checked_at,
                        error_type="timeout",
                        evidence=[
                            f"action={result.action_name}",
                            f"target={target}",
                            *[f"expected_{key}={value}" for key, value in expected.items()],
                            f"verifier={verifier_name}",
                            "probe_error=timeout",
                            f"checked_at={checked_at.isoformat()}",
                        ],
                    )
                )
            except Exception as error:
                checked_at = datetime.utcnow()
                checks.append(
                    StateVerificationResult(
                        verified=False,
                        target=target,
                        expected_state=expected,
                        verifier_name=verifier_name,
                        checked_at=checked_at,
                        error_type=type(error).__name__,
                        evidence=[
                            f"action={result.action_name}",
                            f"target={target}",
                            *[f"expected_{key}={value}" for key, value in expected.items()],
                            f"verifier={verifier_name}",
                            f"probe_error={type(error).__name__}",
                            f"checked_at={checked_at.isoformat()}",
                        ],
                    )
                )
        return checks

    @staticmethod
    def _action_arguments(plan: RunbookPlan, result: MockActionResult) -> Dict[str, Any]:
        arguments = result.metadata.get("action_arguments") if result.metadata else None
        if isinstance(arguments, dict):
            return arguments
        for instruction in plan.actions:
            if instruction.action.value == result.action_name:
                return instruction.arguments
        return {}

    @staticmethod
    def _target_for(result: MockActionResult, arguments: Dict[str, Any]) -> str:
        return (
            result.target
            or arguments.get("target")
            or arguments.get("gateway_id")
            or arguments.get("source_ip")
            or arguments.get("source")
            or "default"
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
        evidence: Optional[List[str]] = None,
    ) -> VerificationResult:
        """构建 VerificationResult 并记录审计"""
        result = VerificationResult(
            incident_id=incident.incident_id,
            action_status=action_status,
            reason=reason,
            evidence=evidence or [],
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
                "evidence": result.evidence,
            },
            message=f"验证完成: {action_status.value} → {next_state.value}",
        )

        return result
