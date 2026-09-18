"""PostgreSQL integration tests for the durable action journal."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from dataclasses import replace

import pytest

from app.agents.action_orchestrator import ActionOrchestrator
from app.core.incident_store import IncidentStore
from app.models.incident import (
    ActionInstruction,
    ActionJournalStatus,
    ExecutionOutcome,
    Incident,
    MockActionResult,
    RunbookPlan,
)
from app.tools.mock_actions import RegisteredAction, get_action_metadata


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for action journal integration tests")
    return url


def _prepared(database_url: str):
    store = IncidentStore(database_url)
    incident = Incident(incident_id=f"INC-JOURNAL-{uuid.uuid4().hex[:12]}")
    record = store.create(incident, f"thread-{incident.incident_id}")
    plan = RunbookPlan(actions=[ActionInstruction(action="notify_dispatcher")])
    plan.bind_execution_identities(incident.incident_id)
    record.runbook_plan = plan
    record.plan = plan.steps
    store.update(record)
    return store, incident, plan, plan.actions[0]


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


def test_started_journal_survives_reload(database_url):
    store, incident, plan, instruction = _prepared(database_url)
    try:
        _start(store, incident, plan, instruction)
        store.repository.engine.dispose()
        journal = IncidentStore(database_url).get_action_journal(instruction.action_id)
        assert journal is not None
        assert journal.journal_status is ActionJournalStatus.STARTED
        assert journal.outcome is None
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_failed_action_is_terminal_and_preserves_failure_details(database_url, monkeypatch):
    store, incident, plan, instruction = _prepared(database_url)

    def handler(**_kwargs):
        return MockActionResult(
            action_name="notify_dispatcher",
            success=False,
            message="permission denied",
            error_type="permission_denied",
            retryable=False,
        )

    metadata = get_action_metadata("notify_dispatcher")
    assert metadata is not None
    monkeypatch.setattr(
        "app.agents.action_orchestrator.get_registered_action",
        lambda _name: RegisteredAction(handler=handler, metadata=metadata),
    )
    try:
        results = await ActionOrchestrator(journal_store=store).execute_plan(incident, plan, "thread")
        assert results[0].success is False
        journal = store.get_action_journal(instruction.action_id)
        attempts = store.list_action_attempts(instruction.action_id)
        assert journal is not None and journal.journal_status is ActionJournalStatus.TERMINAL
        assert journal.outcome is ExecutionOutcome.FAILED
        assert journal.error_type == "permission_denied"
        assert journal.retryable is False and journal.retry_exhausted is True
        assert len(attempts) == 1
        assert attempts[0].outcome is ExecutionOutcome.FAILED
        assert attempts[0].error_type == "permission_denied"
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_success_and_retry_attempts_are_durable(database_url, monkeypatch):
    store, incident, plan, instruction = _prepared(database_url)
    calls = 0

    def handler(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return MockActionResult(action_name="notify_dispatcher", success=False, error_type="failure", retryable=True)
        return MockActionResult(action_name="notify_dispatcher", success=True, message="ok")

    metadata = get_action_metadata("notify_dispatcher")
    assert metadata is not None
    monkeypatch.setattr(
        "app.agents.action_orchestrator.get_registered_action",
        lambda _name: RegisteredAction(handler=handler, metadata=metadata),
    )
    try:
        results = await ActionOrchestrator(journal_store=store).execute_plan(incident, plan, "thread")
        assert results[0].success is True
        journal = store.get_action_journal(instruction.action_id)
        attempts = store.list_action_attempts(instruction.action_id)
        assert journal is not None and journal.journal_status is ActionJournalStatus.TERMINAL
        assert journal.outcome is ExecutionOutcome.SUCCESS
        assert [item.attempt_no for item in attempts] == [1, 2]
        assert len({item.attempt_id for item in attempts}) == 2
        assert attempts[0].retryable is True and attempts[1].success is True
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_response_lost_is_not_converted_to_success(database_url, monkeypatch):
    store, incident, plan, instruction = _prepared(database_url)

    def handler(**_kwargs):
        time.sleep(0.05)
        return MockActionResult(action_name="notify_dispatcher", success=True)

    metadata = get_action_metadata("notify_dispatcher")
    assert metadata is not None
    short_policy = metadata.retry_policy.model_copy(update={"max_retries": 0, "base_delay_seconds": 0})
    monkeypatch.setattr(
        "app.agents.action_orchestrator.get_registered_action",
        lambda _name: RegisteredAction(
            handler=handler,
            metadata=replace(
                metadata,
                timeout_seconds=0.001,
                retry_policy=short_policy,
                idempotent=True,
            ),
        ),
    )
    try:
        results = await ActionOrchestrator(journal_store=store).execute_plan(incident, plan, "thread")
        journal = store.get_action_journal(instruction.action_id)
        attempts = store.list_action_attempts(instruction.action_id)
        assert results[0].outcome == ExecutionOutcome.RESPONSE_LOST.value
        assert journal is not None and journal.outcome is ExecutionOutcome.RESPONSE_LOST
        assert journal.side_effect_possible is True
        assert len(attempts) == 1
        assert attempts[0].outcome is ExecutionOutcome.RESPONSE_LOST
        assert attempts[0].side_effect_possible is True
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_unknown_outcome_is_durable_with_possible_side_effect(database_url, monkeypatch):
    store, incident, plan, instruction = _prepared(database_url)

    def handler(**_kwargs):
        time.sleep(0.05)
        return MockActionResult(action_name="notify_dispatcher", success=True)

    metadata = get_action_metadata("notify_dispatcher")
    assert metadata is not None
    short_policy = metadata.retry_policy.model_copy(update={"max_retries": 0, "base_delay_seconds": 0})
    monkeypatch.setattr(
        "app.agents.action_orchestrator.get_registered_action",
        lambda _name: RegisteredAction(
            handler=handler,
            metadata=replace(
                metadata,
                timeout_seconds=0.001,
                retry_policy=short_policy,
                idempotent=False,
            ),
        ),
    )
    try:
        results = await ActionOrchestrator(journal_store=store).execute_plan(incident, plan, "thread")
        journal = store.get_action_journal(instruction.action_id)
        attempts = store.list_action_attempts(instruction.action_id)
        assert results[0].outcome == ExecutionOutcome.UNKNOWN.value
        assert journal is not None and journal.outcome is ExecutionOutcome.UNKNOWN
        assert journal.side_effect_possible is True
        assert len(attempts) == 1
        assert attempts[0].outcome is ExecutionOutcome.UNKNOWN
        assert attempts[0].side_effect_possible is True
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_crash_after_started_before_external_call_leaves_started_journal(database_url, monkeypatch):
    store, incident, plan, instruction = _prepared(database_url)
    called = False

    def handler(**_kwargs):
        nonlocal called
        called = True
        return MockActionResult(action_name="notify_dispatcher", success=True)

    metadata = get_action_metadata("notify_dispatcher")
    assert metadata is not None
    monkeypatch.setattr(
        "app.agents.action_orchestrator.get_registered_action",
        lambda _name: RegisteredAction(handler=handler, metadata=metadata),
    )
    monkeypatch.setattr(
        store,
        "start_action_attempt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    try:
        with pytest.raises(RuntimeError, match="Durable action attempt write failed"):
            await ActionOrchestrator(journal_store=store).execute_plan(incident, plan, "thread")
        assert called is False
        journal = store.get_action_journal(instruction.action_id)
        assert journal is not None and journal.journal_status is ActionJournalStatus.STARTED
        assert journal.outcome is None
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_crash_after_external_call_leaves_started_journal(database_url, monkeypatch):
    store, incident, plan, instruction = _prepared(database_url)
    called = False

    def handler(**_kwargs):
        nonlocal called
        called = True
        return MockActionResult(action_name="notify_dispatcher", success=True)

    metadata = get_action_metadata("notify_dispatcher")
    assert metadata is not None
    monkeypatch.setattr(
        "app.agents.action_orchestrator.get_registered_action",
        lambda _name: RegisteredAction(handler=handler, metadata=metadata),
    )
    monkeypatch.setattr(store, "finish_action_attempt", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash")))
    try:
        with pytest.raises(RuntimeError, match="Durable action attempt write failed"):
            await ActionOrchestrator(journal_store=store).execute_plan(incident, plan, "thread")
        assert called is True
        journal = store.get_action_journal(instruction.action_id)
        attempts = store.list_action_attempts(instruction.action_id)
        assert journal is not None and journal.journal_status is ActionJournalStatus.STARTED
        assert len(attempts) == 1
        assert attempts[0].status is ActionJournalStatus.STARTED
        assert attempts[0].outcome is None
    finally:
        store.delete(incident.incident_id)


def test_action_journal_survives_process_restart(database_url):
    store, incident, plan, instruction = _prepared(database_url)
    try:
        _start(store, incident, plan, instruction)
        source = "\n".join(
            [
                "import json",
                "from app.core.incident_store import IncidentStore",
                f"store = IncidentStore({database_url!r})",
                f"journal = store.get_action_journal({instruction.action_id!r})",
                "print(json.dumps(journal.model_dump(mode='json')))",
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
        assert '"journal_status": "STARTED"' in completed.stdout
        assert f'"action_id": "{instruction.action_id}"' in completed.stdout
    finally:
        store.delete(incident.incident_id)
