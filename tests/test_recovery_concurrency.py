"""PostgreSQL integration tests for durable recovery claims and lease races."""

from __future__ import annotations

from datetime import datetime, timedelta
import asyncio
import os
import uuid

import pytest

from app.core.incident_store import IncidentStore
from app.core.recovery import Reconciler, RecoveryWorker
from app.models.incident import (
    ActionInstruction,
    ExecutionOutcome,
    ExternalStateObservation,
    ExternalStateStatus,
    Incident,
    MockActionResult,
    RunbookPlan,
)
from app.repositories.incident_repository import RecoveryLeaseRow
from app.tools.mock_actions import RegisteredAction, get_action_metadata


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for recovery concurrency integration tests")
    return url


class StaticProbe:
    def __init__(self, status: ExternalStateStatus, gate: asyncio.Event | None = None) -> None:
        self.status = status
        self.gate = gate
        self.calls = 0

    async def probe(self, candidate):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return ExternalStateObservation(status=self.status, source="lease_test_probe")


def _prepared(database_url: str):
    store = IncidentStore(database_url)
    incident = Incident(incident_id=f"INC-LEASE-{uuid.uuid4().hex[:12]}")
    record = store.create(incident, f"thread-{incident.incident_id}")
    plan = RunbookPlan(actions=[ActionInstruction(action="notify_dispatcher")])
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
    candidate = next(item for item in store.list_recovery_candidates() if item.action_id == instruction.action_id)
    return store, incident, instruction, candidate


@pytest.mark.asyncio
async def test_two_workers_claim_same_candidate_only_once(database_url, monkeypatch):
    store, incident, instruction, candidate = _prepared(database_url)
    gate = asyncio.Event()
    probe_a = StaticProbe(ExternalStateStatus.NOT_APPLIED, gate)
    probe_b = StaticProbe(ExternalStateStatus.NOT_APPLIED)
    worker_a = RecoveryWorker(store, Reconciler(probe_a), owner_id="worker-a")
    worker_b = RecoveryWorker(store, Reconciler(probe_b), owner_id="worker-b")
    executions = 0

    def handler(**_kwargs):
        nonlocal executions
        executions += 1
        return MockActionResult(action_name="notify_dispatcher", success=True)

    metadata = get_action_metadata("notify_dispatcher")
    assert metadata is not None and metadata.idempotent is True
    monkeypatch.setattr(
        "app.agents.action_orchestrator.get_registered_action",
        lambda _name: RegisteredAction(handler=handler, metadata=metadata),
    )
    try:
        pending = asyncio.create_task(worker_a.recover(candidate))
        for _ in range(20):
            if probe_a.calls:
                break
            await asyncio.sleep(0.01)
        second = await worker_b.recover(candidate)
        gate.set()
        first = await pending
        assert first is not None and first.journal.outcome is ExecutionOutcome.SUCCESS
        assert second is None
        assert probe_a.calls == 1
        assert probe_b.calls == 0
        assert executions == 1
    finally:
        store.delete(incident.incident_id)


def test_expired_lease_can_be_reclaimed_but_is_not_success(database_url):
    store, incident, instruction, _candidate = _prepared(database_url)
    try:
        lease_a = store.acquire_recovery_lease(instruction.action_id, "worker-a", 60)
        assert lease_a is not None
        assert store.acquire_recovery_lease(instruction.action_id, "worker-b", 60) is None
        with store.repository.session_factory.begin() as session:
            lease = session.get(RecoveryLeaseRow, instruction.action_id)
            assert lease is not None
            lease.expires_at = datetime.utcnow() - timedelta(seconds=1)
        lease_b = store.acquire_recovery_lease(instruction.action_id, "worker-b", 60)
        journal = store.get_action_journal(instruction.action_id)
        assert lease_b is not None and lease_b.owner_id == "worker-b"
        assert lease_b.lease_token != lease_a.lease_token
        assert journal is not None and journal.outcome is None
    finally:
        store.delete(incident.incident_id)


def test_lease_release_and_completion_require_exact_owner_token(database_url):
    store, incident, instruction, _candidate = _prepared(database_url)
    try:
        lease = store.acquire_recovery_lease(instruction.action_id, "worker-a", 60)
        assert lease is not None
        assert store.release_recovery_lease(instruction.action_id, "worker-b", lease.lease_token) is False
        with pytest.raises(PermissionError):
            store.complete_recovered_action(
                instruction.action_id,
                MockActionResult(action_name="notify_dispatcher", success=True, outcome="success"),
                owner_id="worker-b",
                lease_token=lease.lease_token,
            )
        assert store.release_recovery_lease(instruction.action_id, "worker-a", lease.lease_token) is True
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_terminal_action_race_never_replays_external_action(database_url):
    store, incident, instruction, candidate = _prepared(database_url)
    probe_a = StaticProbe(ExternalStateStatus.APPLIED)
    probe_b = StaticProbe(ExternalStateStatus.APPLIED)
    worker_a = RecoveryWorker(store, Reconciler(probe_a), owner_id="worker-a")
    worker_b = RecoveryWorker(store, Reconciler(probe_b), owner_id="worker-b")
    try:
        first = await worker_a.recover(candidate)
        second = await worker_b.recover(candidate)
        assert first is not None and first.journal.outcome is ExecutionOutcome.SUCCESS
        assert second is None
        assert probe_a.calls == 1
        assert probe_b.calls == 0
    finally:
        store.delete(incident.incident_id)


@pytest.mark.asyncio
async def test_crashed_owner_expiry_reconciles_instead_of_assuming_success(database_url):
    store, incident, instruction, candidate = _prepared(database_url)
    try:
        lease_a = store.acquire_recovery_lease(instruction.action_id, "crashed-worker", 60)
        assert lease_a is not None
        with store.repository.session_factory.begin() as session:
            lease = session.get(RecoveryLeaseRow, instruction.action_id)
            assert lease is not None
            lease.expires_at = datetime.utcnow() - timedelta(seconds=1)
        probe = StaticProbe(ExternalStateStatus.UNKNOWN)
        recovered = await RecoveryWorker(store, Reconciler(probe), owner_id="worker-b").recover(candidate)
        assert recovered is not None and recovered.journal.outcome is ExecutionOutcome.UNKNOWN
        assert probe.calls == 1
    finally:
        store.delete(incident.incident_id)
