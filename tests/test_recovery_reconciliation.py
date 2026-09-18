"""PostgreSQL integration tests for Phase 4 recovery reconciliation."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest

from app.core.incident_store import IncidentStore
from app.core.recovery import Reconciler, RecoveryWorker
from app.models.incident import (
    ActionInstruction,
    ActionJournalStatus,
    ExecutionOutcome,
    ExecutionResult,
    ExternalStateObservation,
    ExternalStateStatus,
    Incident,
    MockActionResult,
    ReconciliationDecision,
    RunbookPlan,
)
from app.tools.mock_actions import RegisteredAction, get_action_metadata


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for recovery integration tests")
    return url


class StaticProbe:
    def __init__(self, status: ExternalStateStatus, *, raises: bool = False) -> None:
        self.status = status
        self.raises = raises
        self.calls = 0

    async def probe(self, candidate):
        self.calls += 1
        if self.raises:
            raise RuntimeError("probe unavailable")
        return ExternalStateObservation(
            status=self.status,
            source="test_probe",
            target=candidate.target,
            evidence={"action_id": candidate.action_id},
        )


def _prepared(database_url: str, action: str = "notify_dispatcher"):
    store = IncidentStore(database_url)
    incident = Incident(incident_id=f"INC-RECOVERY-{uuid.uuid4().hex[:12]}")
    record = store.create(incident, f"thread-{incident.incident_id}")
    plan = RunbookPlan(actions=[ActionInstruction(action=action)])
    plan.bind_execution_identities(incident.incident_id)
    record.plan = plan.steps
    record.runbook_plan = plan
    store.update(record)
    instruction = plan.actions[0]
    store.start_action_journal(
        incident_id=incident.incident_id,
        plan_id=plan.plan_id,
        plan_revision=plan.plan_revision,
        step_id=instruction.step_id,
        action_id=instruction.action_id,
        idempotency_key=instruction.idempotency_key,
        action_name=instruction.action.value,
        target=None,
        request_metadata=instruction.arguments,
    )
    candidate = next(
        item
        for item in store.list_recovery_candidates()
        if item.action_id == instruction.action_id
    )
    return store, incident, plan, instruction, candidate


def _worker(store, probe, orchestrator=None):
    return RecoveryWorker(store, Reconciler(probe), orchestrator)


@pytest.mark.asyncio
async def test_applied_marks_success_without_replay(database_url, monkeypatch):
    store, incident, _plan, instruction, candidate = _prepared(database_url)
    calls = 0

    def handler(**_kwargs):
        nonlocal calls
        calls += 1
        return MockActionResult(action_name="notify_dispatcher", success=True)

    metadata = get_action_metadata("notify_dispatcher")
    assert metadata is not None
    monkeypatch.setattr(
        "app.agents.action_orchestrator.get_registered_action",
        lambda _name: RegisteredAction(handler=handler, metadata=metadata),
    )
    try:
        recovered = await _worker(store, StaticProbe(ExternalStateStatus.APPLIED)).recover(candidate)
        assert recovered.decision is ReconciliationDecision.APPLIED
        assert recovered.action_executed is False
        assert calls == 0
        assert recovered.journal.journal_status is ActionJournalStatus.TERMINAL
        assert recovered.journal.outcome is ExecutionOutcome.SUCCESS
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_not_applied_retries_only_when_registry_policy_allows(database_url, monkeypatch):
    store, incident, _plan, instruction, candidate = _prepared(database_url)
    calls = 0

    def handler(**_kwargs):
        nonlocal calls
        calls += 1
        return MockActionResult(action_name="notify_dispatcher", success=True)

    metadata = get_action_metadata("notify_dispatcher")
    assert metadata is not None and metadata.idempotent is True
    monkeypatch.setattr(
        "app.agents.action_orchestrator.get_registered_action",
        lambda _name: RegisteredAction(handler=handler, metadata=metadata),
    )
    try:
        recovered = await _worker(store, StaticProbe(ExternalStateStatus.NOT_APPLIED)).recover(candidate)
        attempts = store.list_action_attempts(instruction.action_id)
        assert recovered.action_executed is True
        assert calls == 1
        assert recovered.journal.outcome is ExecutionOutcome.SUCCESS
        assert [attempt.attempt_no for attempt in attempts] == [1]
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_not_applied_non_idempotent_action_is_not_replayed(database_url, monkeypatch):
    store, incident, _plan, _instruction, candidate = _prepared(database_url, "generate_ticket")
    calls = 0

    def handler(**_kwargs):
        nonlocal calls
        calls += 1
        return MockActionResult(action_name="generate_ticket", success=True)

    metadata = get_action_metadata("generate_ticket")
    assert metadata is not None and metadata.idempotent is False
    monkeypatch.setattr(
        "app.agents.action_orchestrator.get_registered_action",
        lambda _name: RegisteredAction(handler=handler, metadata=metadata),
    )
    try:
        recovered = await _worker(store, StaticProbe(ExternalStateStatus.NOT_APPLIED)).recover(candidate)
        assert recovered.decision is ReconciliationDecision.NOT_APPLIED
        assert recovered.action_executed is False
        assert calls == 0
        assert recovered.journal.outcome is ExecutionOutcome.UNKNOWN
        assert recovered.journal.error_type == "recovery_retry_forbidden"
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_uncertain_probe_marks_unknown_without_replay(database_url, monkeypatch):
    store, incident, _plan, _instruction, candidate = _prepared(database_url)
    calls = 0

    def handler(**_kwargs):
        nonlocal calls
        calls += 1
        return MockActionResult(action_name="notify_dispatcher", success=True)

    metadata = get_action_metadata("notify_dispatcher")
    assert metadata is not None
    monkeypatch.setattr(
        "app.agents.action_orchestrator.get_registered_action",
        lambda _name: RegisteredAction(handler=handler, metadata=metadata),
    )
    try:
        recovered = await _worker(store, StaticProbe(ExternalStateStatus.UNKNOWN)).recover(candidate)
        assert recovered.decision is ReconciliationDecision.UNCERTAIN
        assert recovered.action_executed is False
        assert calls == 0
        assert recovered.journal.outcome is ExecutionOutcome.UNKNOWN
        assert recovered.journal.side_effect_possible is True
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_probe_failure_is_not_interpreted_as_not_applied(database_url):
    store, incident, _plan, _instruction, candidate = _prepared(database_url)
    try:
        recovered = await _worker(
            store, StaticProbe(ExternalStateStatus.UNAVAILABLE, raises=True)
        ).recover(candidate)
        assert recovered.decision is ReconciliationDecision.NOT_RECONCILABLE
        assert recovered.action_executed is False
        assert recovered.journal.outcome is ExecutionOutcome.UNKNOWN
        assert recovered.journal.error_type == "recovery_probe_unavailable"
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_response_lost_is_reconciled_before_safe_retry(database_url, monkeypatch):
    store, incident, _plan, instruction, candidate = _prepared(database_url)
    store.start_action_attempt(instruction.action_id, 1, {})
    store.finish_action_attempt(
        instruction.action_id,
        1,
        ExecutionResult(
            success=False,
            outcome=ExecutionOutcome.RESPONSE_LOST,
            retryable=False,
            retry_exhausted=True,
            side_effect_possible=True,
            error_type="timeout",
        ),
    )
    candidate = next(
        item
        for item in store.list_recovery_candidates()
        if item.action_id == instruction.action_id
    )
    probe = StaticProbe(ExternalStateStatus.APPLIED)
    calls = 0

    def handler(**_kwargs):
        nonlocal calls
        calls += 1
        return MockActionResult(action_name="notify_dispatcher", success=True)

    metadata = get_action_metadata("notify_dispatcher")
    assert metadata is not None
    monkeypatch.setattr(
        "app.agents.action_orchestrator.get_registered_action",
        lambda _name: RegisteredAction(handler=handler, metadata=metadata),
    )
    try:
        recovered = await _worker(store, probe).recover(candidate)
        assert probe.calls == 1
        assert recovered.decision is ReconciliationDecision.APPLIED
        assert calls == 0
        assert recovered.journal.outcome is ExecutionOutcome.SUCCESS
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_crash_before_external_call_is_reconciled_not_assumed_not_sent(database_url, monkeypatch):
    store, incident, _plan, instruction, candidate = _prepared(database_url)
    store.start_action_attempt(instruction.action_id, 1, {})
    candidate = next(
        item
        for item in store.list_recovery_candidates()
        if item.action_id == instruction.action_id
    )
    calls = 0

    def handler(**_kwargs):
        nonlocal calls
        calls += 1
        return MockActionResult(action_name="notify_dispatcher", success=True)

    metadata = get_action_metadata("notify_dispatcher")
    assert metadata is not None
    monkeypatch.setattr(
        "app.agents.action_orchestrator.get_registered_action",
        lambda _name: RegisteredAction(handler=handler, metadata=metadata),
    )
    try:
        recovered = await _worker(store, StaticProbe(ExternalStateStatus.UNKNOWN)).recover(candidate)
        assert recovered.decision is ReconciliationDecision.UNCERTAIN
        assert calls == 0
        assert recovered.journal.outcome is ExecutionOutcome.UNKNOWN
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_crash_after_external_side_effect_applied_is_never_replayed(database_url, monkeypatch):
    store, incident, _plan, instruction, candidate = _prepared(database_url)
    store.start_action_attempt(instruction.action_id, 1, {})
    candidate = next(
        item
        for item in store.list_recovery_candidates()
        if item.action_id == instruction.action_id
    )
    calls = 0

    def handler(**_kwargs):
        nonlocal calls
        calls += 1
        return MockActionResult(action_name="notify_dispatcher", success=True)

    metadata = get_action_metadata("notify_dispatcher")
    assert metadata is not None
    monkeypatch.setattr(
        "app.agents.action_orchestrator.get_registered_action",
        lambda _name: RegisteredAction(handler=handler, metadata=metadata),
    )
    try:
        recovered = await _worker(store, StaticProbe(ExternalStateStatus.APPLIED)).recover(candidate)
        assert recovered.journal.outcome is ExecutionOutcome.SUCCESS
        assert calls == 0
        assert store.list_action_attempts(instruction.action_id)[0].status is ActionJournalStatus.STARTED
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_recovery_reuses_existing_plan_without_triage_or_regeneration(database_url):
    store, incident, plan, instruction, candidate = _prepared(database_url)
    original_identity = (plan.plan_id, plan.plan_revision, instruction.step_id, instruction.action_id)
    try:
        await _worker(store, StaticProbe(ExternalStateStatus.APPLIED)).recover(candidate)
        reloaded = store.repository.get_plan(incident.incident_id, plan.plan_revision)
        assert reloaded is not None
        reloaded_instruction = reloaded.actions[0]
        assert (
            reloaded.plan_id,
            reloaded.plan_revision,
            reloaded_instruction.step_id,
            reloaded_instruction.action_id,
        ) == original_identity
    finally:
        store.delete(incident.incident_id)


def test_recovery_candidate_is_visible_to_a_new_process(database_url):
    store, incident, _plan, instruction, _candidate = _prepared(database_url, "switch_backup_link")
    source = "\n".join(
        [
            "import json",
            "from app.core.incident_store import IncidentStore",
            "from app.core.recovery import RecoveryWorker",
            f"store = IncidentStore({database_url!r})",
            "result = __import__('asyncio').run(RecoveryWorker(store).recover_all())",
            "print(json.dumps([item.model_dump(mode='json') for item in result]))",
        ]
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", source],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "DATABASE_URL": database_url},
        )
        assert completed.returncode == 0, completed.stderr
        assert instruction.action_id in completed.stdout
        assert '"journal_status": "TERMINAL"' in completed.stdout
    finally:
        store.delete(incident.incident_id)
