"""Durable action reconciliation, PostgreSQL claims, and workflow resume."""

from __future__ import annotations

import asyncio
import uuid
from typing import Protocol, Optional

from app.agents.action_orchestrator import ActionOrchestrator
from app.core.audit_store import audit_store
from app.core.incident_store import IncidentStore
from app.models.incident import (
    ActionStatus,
    ExecutionOutcome,
    ExternalStateObservation,
    ExternalStateStatus,
    MockActionResult,
    ReconciliationDecision,
    ReconciliationResult,
    RecoveryCandidate,
    RecoveryResult,
    SSEEventType,
    WorkflowCursorStatus,
)
from app.agents.verifier import Verifier
from app.tools.mock_actions import get_action_metadata


class ExternalStateProbe(Protocol):
    """Read-only adapter for checking the external state of a durable action."""

    async def probe(self, candidate: RecoveryCandidate) -> ExternalStateObservation:
        """Return facts about external state without executing or mutating anything."""


class MockExternalStateProbe:
    """Read-only Phase 4 adapter over the existing Mock action registry probes."""

    async def probe(self, candidate: RecoveryCandidate) -> ExternalStateObservation:
        metadata = get_action_metadata(candidate.action_name)
        if metadata is None or metadata.state_verifier is None:
            return ExternalStateObservation(
                status=ExternalStateStatus.UNAVAILABLE,
                source="mock_action_registry",
                target=candidate.target,
                reason="No read-only state verifier is registered for this action",
            )

        target = candidate.target or _target_from_arguments(candidate.request_metadata) or "default"
        expected = metadata.expected_state(candidate.request_metadata) if metadata.expected_state else {}
        try:
            observed = await asyncio.to_thread(metadata.state_verifier, target)
        except Exception as exc:
            return ExternalStateObservation(
                status=ExternalStateStatus.UNAVAILABLE,
                source=metadata.state_verifier.__name__,
                target=target,
                reason=f"Probe failed: {type(exc).__name__}: {exc}",
            )

        applied = bool(expected) and all(observed.get(key) == value for key, value in expected.items())
        return ExternalStateObservation(
            status=ExternalStateStatus.APPLIED if applied else ExternalStateStatus.NOT_APPLIED,
            source=metadata.state_verifier.__name__,
            target=target,
            evidence={"expected": expected, "observed": observed},
        )


class Reconciler:
    """Turns a read-only external observation into a recovery decision."""

    def __init__(self, probe: Optional[ExternalStateProbe] = None) -> None:
        self.probe = probe or MockExternalStateProbe()

    async def reconcile(self, candidate: RecoveryCandidate) -> ReconciliationResult:
        try:
            observation = await self.probe.probe(candidate)
        except Exception as exc:
            observation = ExternalStateObservation(
                status=ExternalStateStatus.UNAVAILABLE,
                source=type(self.probe).__name__,
                target=candidate.target,
                reason=f"Probe failed: {type(exc).__name__}: {exc}",
            )

        decisions = {
            ExternalStateStatus.APPLIED: ReconciliationDecision.APPLIED,
            ExternalStateStatus.NOT_APPLIED: ReconciliationDecision.NOT_APPLIED,
            ExternalStateStatus.UNKNOWN: ReconciliationDecision.UNCERTAIN,
            ExternalStateStatus.UNAVAILABLE: ReconciliationDecision.NOT_RECONCILABLE,
        }
        return ReconciliationResult(decision=decisions[observation.status], observation=observation)


class RecoveryWorker:
    """Reconcile durable non-terminal actions before any permitted replay.

    It is intentionally not a startup hook. PostgreSQL leases coordinate recovery
    ownership across worker processes, but do not make external side effects
    exactly-once.
    """

    def __init__(
        self,
        store: IncidentStore,
        reconciler: Optional[Reconciler] = None,
        action_orchestrator: Optional[ActionOrchestrator] = None,
        owner_id: Optional[str] = None,
        lease_seconds: int = 60,
    ) -> None:
        self.store = store
        self.reconciler = reconciler or Reconciler()
        self.action_orchestrator = action_orchestrator or ActionOrchestrator(journal_store=store)
        self.owner_id = owner_id or f"recovery-worker-{uuid.uuid4().hex}"
        self.lease_seconds = lease_seconds

    async def recover_all(self) -> list[RecoveryResult]:
        recovered = [await self.recover(candidate) for candidate in self.store.list_recovery_candidates()]
        return [result for result in recovered if result is not None]

    async def recover(self, candidate: RecoveryCandidate) -> Optional[RecoveryResult]:
        """Recover exactly one persisted action using its existing plan identity."""
        lease = self.store.acquire_recovery_lease(
            candidate.action_id,
            self.owner_id,
            self.lease_seconds,
        )
        if lease is None:
            # The action is terminal or another durable worker owns it. In both
            # cases this worker must not probe or execute it.
            return None
        try:
            reconciliation = await self.reconciler.reconcile(candidate)
            record = self.store.get(candidate.incident_id)
            if record is None:
                raise KeyError(f"Incident not found: {candidate.incident_id}")

            if reconciliation.decision is ReconciliationDecision.APPLIED:
                result = MockActionResult(
                    action_name=candidate.action_name,
                    target=reconciliation.observation.target or candidate.target,
                    success=True,
                    message="External state probe confirmed the action is already applied",
                    outcome=ExecutionOutcome.SUCCESS.value,
                    retryable=False,
                    side_effect_possible=True,
                    side_effect_confirmed=True,
                )
                return self._persist(candidate, reconciliation, result, lease, action_executed=False)

            if reconciliation.decision is ReconciliationDecision.NOT_APPLIED:
                plan = self.store.repository.get_plan(candidate.incident_id, candidate.plan_revision)
                instruction = _instruction_for_action(plan, candidate.action_id) if plan else None
                metadata = get_action_metadata(candidate.action_name)
                if instruction is not None and metadata is not None and metadata.idempotent and not candidate.retry_exhausted:
                    # The prior journal remains STARTED while this external call runs.
                    # resume_action appends attempts under the same action identity.
                    result = await self.action_orchestrator.resume_action(
                        record.incident,
                        instruction,
                        record.thread_id,
                        candidate.latest_attempt_no or 0,
                    )
                    return self._persist(candidate, reconciliation, result, lease, action_executed=True)
                result = _unknown_result(
                    candidate,
                    "Probe confirmed target state is not applied, but recovery retry is not permitted",
                    "recovery_retry_forbidden",
                    side_effect_possible=False,
                )
                return self._persist(candidate, reconciliation, result, lease, action_executed=False)

            error_type = (
                "recovery_probe_unavailable"
                if reconciliation.decision is ReconciliationDecision.NOT_RECONCILABLE
                else "recovery_uncertain"
            )
            result = _unknown_result(
                candidate,
                reconciliation.observation.reason or "External state cannot be determined",
                error_type,
                side_effect_possible=True,
            )
            return self._persist(candidate, reconciliation, result, lease, action_executed=False)
        except Exception:
            # Completion deletes an owned lease atomically. Any pre-completion
            # failure releases only this worker's lease so a later recovery can
            # reconcile the remaining STARTED action.
            self.store.release_recovery_lease(candidate.action_id, self.owner_id, lease.lease_token)
            raise

    def _persist(
        self,
        candidate: RecoveryCandidate,
        reconciliation: ReconciliationResult,
        result: MockActionResult,
        lease,
        *,
        action_executed: bool,
    ) -> RecoveryResult:
        metadata = {
            "recovery": {
                "decision": reconciliation.decision.value,
                "observed_at": reconciliation.observation.observed_at.isoformat(),
                "source": reconciliation.observation.source,
                "target": reconciliation.observation.target,
                "evidence": reconciliation.observation.evidence,
                "reason": reconciliation.observation.reason,
                "action_executed": action_executed,
            }
        }
        journal = self.store.complete_recovered_action(
            candidate.action_id,
            result,
            metadata,
            owner_id=self.owner_id,
            lease_token=lease.lease_token,
        )
        record = self.store.get(candidate.incident_id)
        if record is not None:
            audit_store.record(
                trace_id=record.incident.trace_id,
                incident_id=candidate.incident_id,
                thread_id=record.thread_id,
                event_type=SSEEventType.STATE_CHANGED,
                actor="RecoveryWorker",
                action="reconcile",
                detail=metadata,
                message=f"Recovery {reconciliation.decision.value}: {candidate.action_name}",
            )
        return RecoveryResult(
            action_id=candidate.action_id,
            decision=reconciliation.decision,
            observation=reconciliation.observation,
            action_executed=action_executed,
            journal=journal,
        )


class WorkflowResumer:
    """Continue a durable cursor through existing plan instructions only."""

    def __init__(
        self,
        store: IncidentStore,
        action_orchestrator: Optional[ActionOrchestrator] = None,
        verifier: Optional[Verifier] = None,
    ) -> None:
        self.store = store
        self.action_orchestrator = action_orchestrator or ActionOrchestrator(journal_store=store)
        self.verifier = verifier or Verifier()

    async def resume_workflow(self, incident_id: str) -> list[MockActionResult]:
        """Execute only cursor-authorized, not-yet-started durable plan steps."""
        completed: list[MockActionResult] = []
        while True:
            cursor = self.store.get_workflow_cursor(incident_id)
            if cursor is None or cursor.cursor_status is not WorkflowCursorStatus.NOT_STARTED:
                return completed
            record = self.store.get(incident_id)
            if record is None:
                raise KeyError(f"Incident not found: {incident_id}")
            plan = self.store.repository.get_plan(incident_id, cursor.plan_revision)
            if plan is None or plan.plan_id != cursor.plan_id:
                self.store.block_workflow_cursor(incident_id)
                return completed
            instruction = _instruction_for_action(plan, cursor.current_action_id or "")
            if instruction is None:
                self.store.block_workflow_cursor(incident_id)
                return completed
            existing = self.store.get_action_journal(instruction.action_id)
            if existing is not None:
                # A terminal success should already have atomically advanced the
                # cursor. Never repair this inconsistency by replaying or skipping.
                self.store.block_workflow_cursor(incident_id)
                return completed

            result = await self.action_orchestrator.execute_instruction(
                record.incident, plan, instruction, record.thread_id
            )
            completed.append(result)
            verification = await self.verifier.verify(
                record.incident, plan, [result], record.thread_id
            )
            if not result.success or verification.action_status is not ActionStatus.SUCCESS:
                self.store.block_workflow_cursor(incident_id)
                return completed

    async def verify_recovered_action(self, incident_id: str, action_id: str) -> bool:
        """Run the existing Verifier before a recovered success can resume its cursor."""
        record = self.store.get(incident_id)
        journal = self.store.get_action_journal(action_id)
        if record is None or journal is None or journal.outcome is not ExecutionOutcome.SUCCESS:
            return False
        plan = self.store.repository.get_plan(incident_id, journal.plan_revision)
        instruction = _instruction_for_action(plan, action_id) if plan else None
        if plan is None or instruction is None:
            self.store.block_workflow_cursor(incident_id)
            return False
        result = MockActionResult(
            action_name=journal.action_name,
            target=journal.target,
            success=True,
            message="Recovered action terminal success",
            outcome=ExecutionOutcome.SUCCESS.value,
            retryable=False,
            side_effect_possible=journal.side_effect_possible,
            side_effect_confirmed=True,
            metadata={"action_arguments": journal.request_metadata},
        )
        verification = await self.verifier.verify(record.incident, plan, [result], record.thread_id)
        if verification.action_status is ActionStatus.SUCCESS:
            return True
        self.store.block_workflow_cursor(incident_id)
        return False


async def recover_pending_workflows(
    store: IncidentStore,
    *,
    recovery_worker: Optional[RecoveryWorker] = None,
    workflow_resumer: Optional[WorkflowResumer] = None,
) -> list[RecoveryResult]:
    """Explicit service-level Phase 5 entry point; it is not a background scheduler."""
    worker = recovery_worker or RecoveryWorker(store)
    resumer = workflow_resumer or WorkflowResumer(store)
    recovered = await worker.recover_all()
    resume_ids = {
        item.journal.incident_id
        for item in recovered
        if item.journal.outcome is ExecutionOutcome.SUCCESS
        and await resumer.verify_recovered_action(item.journal.incident_id, item.action_id)
    }
    resume_ids.update(cursor.incident_id for cursor in store.list_resumable_workflow_cursors())
    for incident_id in resume_ids:
        await resumer.resume_workflow(incident_id)
    return recovered


def _instruction_for_action(plan, action_id: str):
    if plan is None:
        return None
    return next(
        (
            instruction
            for instruction in [*plan.actions, *plan.rollback_actions]
            if instruction.action_id == action_id
        ),
        None,
    )


def _target_from_arguments(arguments: dict) -> Optional[str]:
    for key in ("target", "gateway_id", "source_ip", "source"):
        if arguments.get(key):
            return str(arguments[key])
    return None


def _unknown_result(
    candidate: RecoveryCandidate,
    message: str,
    error_type: str,
    *,
    side_effect_possible: bool,
) -> MockActionResult:
    return MockActionResult(
        action_name=candidate.action_name,
        target=candidate.target,
        success=False,
        message=message,
        error_type=error_type,
        outcome=ExecutionOutcome.UNKNOWN.value,
        retryable=False,
        retry_exhausted=True,
        side_effect_possible=side_effect_possible,
    )
