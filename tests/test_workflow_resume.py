"""PostgreSQL integration tests for durable cursor and workflow resume."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest

from app.agents.action_orchestrator import ActionOrchestrator
from app.core.incident_store import IncidentStore
from app.core.recovery import Reconciler, RecoveryWorker, WorkflowResumer, recover_pending_workflows
from app.models.incident import (
    ActionInstruction,
    ExecutionOutcome,
    ExecutionResult,
    ExternalStateObservation,
    ExternalStateStatus,
    Incident,
    MockActionResult,
    RunbookPlan,
    WorkflowCursorStatus,
)
from app.tools.mock_actions import RegisteredAction, get_action_metadata


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for workflow resume integration tests")
    return url


class StaticProbe:
    def __init__(self, status: ExternalStateStatus) -> None:
        self.status = status

    async def probe(self, candidate):
        return ExternalStateObservation(
            status=self.status,
            source="workflow_resume_test_probe",
            target=candidate.target,
        )


def _prepared(database_url: str, count: int = 3):
    store = IncidentStore(database_url)
    incident = Incident(incident_id=f"INC-WORKFLOW-{uuid.uuid4().hex[:12]}")
    record = store.create(incident, f"thread-{incident.incident_id}")
    plan = RunbookPlan(
        actions=[ActionInstruction(action="notify_dispatcher", description=f"step-{index}") for index in range(count)]
    )
    plan.bind_execution_identities(incident.incident_id)
    record.plan = plan.steps
    record.runbook_plan = plan
    store.update(record)
    return store, incident, plan


def _start(store, incident, plan, instruction):
    return store.start_action_journal(
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


def _candidate(store, action_id):
    return next(item for item in store.list_recovery_candidates() if item.action_id == action_id)


def _success_handler(monkeypatch, calls):
    metadata = get_action_metadata("notify_dispatcher")
    assert metadata is not None

    def handler(**_kwargs):
        calls.append("executed")
        return MockActionResult(action_name="notify_dispatcher", success=True, message="ok")

    monkeypatch.setattr(
        "app.agents.action_orchestrator.get_registered_action",
        lambda _name: RegisteredAction(handler=handler, metadata=metadata),
    )


def test_cursor_is_created_at_first_persisted_step(database_url):
    store, incident, plan = _prepared(database_url)
    try:
        cursor = store.get_workflow_cursor(incident.incident_id)
        assert cursor is not None
        assert cursor.plan_id == plan.plan_id
        assert cursor.plan_revision == plan.plan_revision
        assert cursor.current_step_id == plan.actions[0].step_id
        assert cursor.current_action_id == plan.actions[0].action_id
        assert cursor.cursor_status is WorkflowCursorStatus.NOT_STARTED
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_success_advances_cursor_only_after_terminal_write(database_url, monkeypatch):
    store, incident, plan = _prepared(database_url, 2)
    calls = []
    _success_handler(monkeypatch, calls)
    try:
        result = await ActionOrchestrator(journal_store=store).execute_instruction(
            incident, plan, plan.actions[0], "thread"
        )
        journal = store.get_action_journal(plan.actions[0].action_id)
        cursor = store.get_workflow_cursor(incident.incident_id)
        assert result.success is True
        assert journal is not None and journal.outcome is ExecutionOutcome.SUCCESS
        assert cursor is not None and cursor.current_action_id == plan.actions[1].action_id
        assert cursor.cursor_status is WorkflowCursorStatus.NOT_STARTED
        assert len(calls) == 1
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_recovered_applied_advances_to_next_existing_step(database_url):
    store, incident, plan = _prepared(database_url, 3)
    first, second, third = plan.actions
    try:
        _start(store, incident, plan, first)
        store.finish_action_journal(
            first.action_id,
            MockActionResult(action_name=first.action.value, success=True, outcome="success"),
        )
        _start(store, incident, plan, second)
        store.start_action_attempt(second.action_id, 1, {})
        store.finish_action_attempt(
            second.action_id,
            1,
            ExecutionResult(
                success=False,
                outcome=ExecutionOutcome.RESPONSE_LOST,
                retryable=False,
                retry_exhausted=True,
                side_effect_possible=True,
            ),
        )
        recovered = await RecoveryWorker(store, Reconciler(StaticProbe(ExternalStateStatus.APPLIED))).recover(
            _candidate(store, second.action_id)
        )
        cursor = store.get_workflow_cursor(incident.incident_id)
        assert recovered is not None and recovered.journal.outcome is ExecutionOutcome.SUCCESS
        assert cursor is not None and cursor.current_action_id == third.action_id
        assert cursor.cursor_status is WorkflowCursorStatus.NOT_STARTED
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_not_applied_safe_retry_preserves_identity_and_appends_attempt(database_url, monkeypatch):
    store, incident, plan = _prepared(database_url, 2)
    first, second = plan.actions
    calls = []
    _success_handler(monkeypatch, calls)
    try:
        _start(store, incident, plan, first)
        store.start_action_attempt(first.action_id, 1, {})
        recovered = await RecoveryWorker(store, Reconciler(StaticProbe(ExternalStateStatus.NOT_APPLIED))).recover(
            _candidate(store, first.action_id)
        )
        attempts = store.list_action_attempts(first.action_id)
        cursor = store.get_workflow_cursor(incident.incident_id)
        assert recovered is not None and recovered.action_executed is True
        assert recovered.journal.action_id == first.action_id
        assert recovered.journal.plan_revision == plan.plan_revision
        assert recovered.journal.step_id == first.step_id
        assert recovered.journal.idempotency_key == first.idempotency_key
        assert [attempt.attempt_no for attempt in attempts] == [1, 2]
        assert len({attempt.attempt_id for attempt in attempts}) == 2
        assert cursor is not None and cursor.current_action_id == second.action_id
        assert len(calls) == 1
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_unknown_blocks_cursor_and_does_not_execute_next_step(database_url, monkeypatch):
    store, incident, plan = _prepared(database_url, 2)
    calls = []
    _success_handler(monkeypatch, calls)
    try:
        _start(store, incident, plan, plan.actions[0])
        recovered = await RecoveryWorker(store, Reconciler(StaticProbe(ExternalStateStatus.UNKNOWN))).recover(
            _candidate(store, plan.actions[0].action_id)
        )
        resumed = await WorkflowResumer(store).resume_workflow(incident.incident_id)
        cursor = store.get_workflow_cursor(incident.incident_id)
        assert recovered is not None and recovered.journal.outcome is ExecutionOutcome.UNKNOWN
        assert resumed == []
        assert cursor is not None and cursor.current_action_id == plan.actions[0].action_id
        assert cursor.cursor_status is WorkflowCursorStatus.BLOCKED
        assert store.get_action_journal(plan.actions[1].action_id) is None
        assert calls == []
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_full_resume_reuses_plan_and_executes_only_remaining_steps(database_url, monkeypatch):
    store, incident, plan = _prepared(database_url, 3)
    calls = []
    _success_handler(monkeypatch, calls)
    first, second, third = plan.actions
    original = (plan.plan_id, plan.plan_revision, second.action_id, second.idempotency_key)
    try:
        _start(store, incident, plan, first)
        store.finish_action_journal(
            first.action_id,
            MockActionResult(action_name=first.action.value, success=True, outcome="success"),
        )
        _start(store, incident, plan, second)
        recovered = await RecoveryWorker(store, Reconciler(StaticProbe(ExternalStateStatus.APPLIED))).recover(
            _candidate(store, second.action_id)
        )
        results = await WorkflowResumer(store).resume_workflow(incident.incident_id)
        cursor = store.get_workflow_cursor(incident.incident_id)
        reloaded = store.repository.get_plan(incident.incident_id, plan.plan_revision)
        assert recovered is not None and recovered.action_executed is False
        assert [item.action_name for item in results] == ["notify_dispatcher"]
        assert len(calls) == 1
        assert cursor is not None and cursor.cursor_status is WorkflowCursorStatus.COMPLETED
        assert reloaded is not None
        assert (reloaded.plan_id, reloaded.plan_revision, reloaded.actions[1].action_id, reloaded.actions[1].idempotency_key) == original
        assert store.get_action_journal(third.action_id) is not None
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_service_entry_recovers_then_resumes_without_triage_or_runbook(database_url, monkeypatch):
    store, incident, plan = _prepared(database_url, 2)
    calls = []
    _success_handler(monkeypatch, calls)
    first, second = plan.actions

    async def should_not_triage(*_args, **_kwargs):
        raise AssertionError("Triage must not run during workflow recovery")

    async def should_not_plan(*_args, **_kwargs):
        raise AssertionError("RunbookAgent must not run during workflow recovery")

    monkeypatch.setattr("app.agents.triage_agent.TriageAgent.triage", should_not_triage)
    monkeypatch.setattr("app.agents.runbook_agent.RunbookAgent.generate_plan", should_not_plan)
    try:
        _start(store, incident, plan, first)
        recovered = await recover_pending_workflows(
            store,
            recovery_worker=RecoveryWorker(
                store,
                Reconciler(StaticProbe(ExternalStateStatus.APPLIED)),
            ),
        )
        cursor = store.get_workflow_cursor(incident.incident_id)
        assert len(recovered) == 1
        assert recovered[0].journal.action_id == first.action_id
        assert store.get_action_journal(second.action_id) is not None
        assert cursor is not None and cursor.cursor_status is WorkflowCursorStatus.COMPLETED
        assert calls == ["executed"]
    finally:
        store.delete(incident.incident_id)


def test_cursor_survives_process_reload(database_url):
    store, incident, plan = _prepared(database_url, 3)
    try:
        _start(store, incident, plan, plan.actions[0])
        store.finish_action_journal(
            plan.actions[0].action_id,
            MockActionResult(action_name="notify_dispatcher", success=True, outcome="success"),
        )
        source = "\n".join(
            [
                "import json",
                "from app.core.incident_store import IncidentStore",
                f"store = IncidentStore({database_url!r})",
                f"cursor = store.get_workflow_cursor({incident.incident_id!r})",
                "print(json.dumps(cursor.model_dump(mode='json')))",
            ]
        )
        completed = subprocess.run(
            [sys.executable, "-c", source],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "DATABASE_URL": database_url},
        )
        assert completed.returncode == 0, completed.stderr
        assert plan.actions[1].action_id in completed.stdout
    finally:
        store.delete(incident.incident_id)
