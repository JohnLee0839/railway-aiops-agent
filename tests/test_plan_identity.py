"""PostgreSQL integration tests for Phase 2 plan, step, and action identity."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.agents.action_orchestrator import ActionOrchestrator
from app.core.incident_store import IncidentStore
from app.models.incident import ActionInstruction, Incident, MockActionResult, RunbookPlan
from app.repositories.incident_repository import (
    RunbookPlanRevisionRow,
    RunbookPlanRow,
    RunbookPlanStepRow,
)


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL identity integration tests")
    if not url.startswith("postgresql+"):
        pytest.fail("TEST_DATABASE_URL must use a PostgreSQL SQLAlchemy dialect")
    return url


def _incident() -> Incident:
    return Incident(incident_id=f"INC-IDENTITY-{uuid.uuid4().hex[:12]}")


def _plan(plan_id: str | None = None, revision: int = 1) -> RunbookPlan:
    return RunbookPlan(
        plan_id=plan_id or f"plan-{uuid.uuid4().hex[:12]}",
        plan_revision=revision,
        actions=[
            ActionInstruction(action="switch_backup_link", description="切换备用链路"),
            ActionInstruction(action="restart_gateway", description="重启网关"),
            ActionInstruction(action="notify_dispatcher", description="通知调度员"),
        ],
        rollback_actions=[
            ActionInstruction(
                action="rollback_switch_backup_link",
                description="恢复主链路",
            )
        ],
    )


def _persist_plan(store: IncidentStore, incident: Incident, plan: RunbookPlan):
    record = store.create(incident, f"thread-{incident.incident_id}")
    record.plan = plan.steps
    record.runbook_plan = plan
    return store.update(record)


def _identities(plan: RunbookPlan) -> list[tuple[str, str, str]]:
    return [
        (instruction.step_id, instruction.action_id, instruction.idempotency_key or "")
        for instruction in [*plan.actions, *plan.rollback_actions]
    ]


def test_plan_revision_and_action_identities_survive_reload(database_url: str):
    incident = _incident()
    store = IncidentStore(database_url)
    try:
        record = _persist_plan(store, incident, _plan())
        original = record.runbook_plan
        assert original is not None
        assert original.plan_revision == 1
        original_identities = _identities(original)

        store.repository.engine.dispose()
        reloaded = IncidentStore(database_url).get(incident.incident_id)
        assert reloaded is not None and reloaded.runbook_plan is not None
        assert reloaded.runbook_plan.plan_revision == 1
        assert _identities(reloaded.runbook_plan) == original_identities
        assert len({step_id for step_id, _, _ in original_identities}) == len(original_identities)
        assert len({action_id for _, action_id, _ in original_identities}) == len(original_identities)
        assert len({key for _, _, key in original_identities}) == len(original_identities)
    finally:
        store.delete(incident.incident_id)
        store.repository.engine.dispose()


def test_reordering_actions_does_not_change_identity(database_url: str):
    incident = _incident()
    store = IncidentStore(database_url)
    try:
        record = _persist_plan(store, incident, _plan())
        assert record.runbook_plan is not None
        original_by_action_id = {
            instruction.action_id: (instruction.step_id, instruction.idempotency_key)
            for instruction in record.runbook_plan.actions
        }

        record.runbook_plan.actions.reverse()
        record.plan = record.runbook_plan.steps
        store.update(record)
        reloaded = store.get(incident.incident_id)
        assert reloaded is not None and reloaded.runbook_plan is not None
        reloaded_by_action_id = {
            instruction.action_id: (instruction.step_id, instruction.idempotency_key)
            for instruction in reloaded.runbook_plan.actions
        }
        assert reloaded_by_action_id == original_by_action_id
    finally:
        store.delete(incident.incident_id)
        store.repository.engine.dispose()


def test_different_revisions_have_different_execution_identities(database_url: str):
    incident = _incident()
    store = IncidentStore(database_url)
    try:
        plan_id = f"plan-{uuid.uuid4().hex[:12]}"
        record = _persist_plan(store, incident, _plan(plan_id, revision=1))
        assert record.runbook_plan is not None
        revision_one_identities = set(_identities(record.runbook_plan))

        revision_two = _plan(plan_id, revision=2)
        record.plan = revision_two.steps
        record.runbook_plan = revision_two
        store.update(record)

        reloaded_one = store.repository.get_plan(incident.incident_id, plan_revision=1)
        reloaded_two = store.repository.get_plan(incident.incident_id, plan_revision=2)
        assert reloaded_one is not None and reloaded_two is not None
        assert reloaded_two.plan_revision == 2
        assert revision_one_identities.isdisjoint(set(_identities(reloaded_two)))
    finally:
        store.delete(incident.incident_id)
        store.repository.engine.dispose()


def test_phase_one_plan_is_backfilled_once_with_stable_identity(database_url: str):
    incident = _incident()
    store = IncidentStore(database_url)
    try:
        record = store.create(incident, "thread-phase-one-backfill")
        legacy_plan = _plan()
        with store.repository.session_factory.begin() as session:
            session.add(
                RunbookPlanRow(
                    plan_id=legacy_plan.plan_id,
                    incident_id=record.incident_id,
                    plan_revision=1,
                    payload=legacy_plan.model_dump(mode="json"),
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
            )

        first_reload = IncidentStore(database_url).repository.get_plan(incident.incident_id)
        assert first_reload is not None
        first_identities = _identities(first_reload)
        assert all(key for _, _, key in first_identities)

        second_reload = IncidentStore(database_url).repository.get_plan(incident.incident_id)
        assert second_reload is not None
        assert _identities(second_reload) == first_identities
    finally:
        store.delete(incident.incident_id)
        store.repository.engine.dispose()


def test_identity_survives_process_restart(database_url: str):
    incident_id = f"INC-IDENTITY-PROCESS-{uuid.uuid4().hex[:12]}"
    plan_id = f"plan-{uuid.uuid4().hex[:12]}"
    source = "\n".join(
        [
            "import json",
            "from app.core.incident_store import IncidentStore",
            "from app.models.incident import ActionInstruction, Incident, RunbookPlan",
            f"store = IncidentStore({database_url!r})",
            f"incident = Incident(incident_id={incident_id!r})",
            (
                "plan = RunbookPlan(plan_id="
                f"{plan_id!r}, actions=[ActionInstruction(action='notify_dispatcher')])"
            ),
            "record = store.create(incident, 'thread-process-identity')",
            "record.plan = plan.steps",
            "record.runbook_plan = plan",
            "store.update(record)",
            "print(json.dumps(plan.model_dump(mode='json')))",
        ]
    )
    environment = {**os.environ, "DATABASE_URL": database_url}
    result = subprocess.run(
        [sys.executable, "-c", source],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    original = RunbookPlan(**json.loads(result.stdout.strip().splitlines()[-1]))

    store = IncidentStore(database_url)
    try:
        reloaded = store.repository.get_plan(incident_id)
        assert reloaded is not None
        assert reloaded.plan_revision == original.plan_revision
        assert _identities(reloaded) == _identities(original)
    finally:
        store.delete(incident_id)
        store.repository.engine.dispose()


def test_database_rejects_identity_collisions(database_url: str):
    incident = _incident()
    store = IncidentStore(database_url)
    related_incidents = [incident.incident_id]
    try:
        record = _persist_plan(store, incident, _plan())
        assert record.runbook_plan is not None
        first = record.runbook_plan.actions[0]
        repository = store.repository

        with pytest.raises(IntegrityError):
            with repository.session_factory.begin() as session:
                session.add(
                    RunbookPlanRevisionRow(
                        plan_id=record.runbook_plan.plan_id,
                        plan_revision=record.runbook_plan.plan_revision,
                        incident_id=incident.incident_id,
                        payload={},
                        created_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                    )
                )

        with pytest.raises(IntegrityError):
            with repository.session_factory.begin() as session:
                session.add(
                    RunbookPlanStepRow(
                        plan_id=record.runbook_plan.plan_id,
                        plan_revision=record.runbook_plan.plan_revision,
                        step_id=first.step_id,
                        action_id=f"action-{uuid.uuid4().hex}",
                        idempotency_key=f"idem-{uuid.uuid4().hex}",
                        action_name=first.action.value,
                        is_rollback=False,
                        position=99,
                    )
                )

        duplicate_action_incident = _incident()
        related_incidents.append(duplicate_action_incident.incident_id)
        duplicate_action = _plan()
        duplicate_action.actions[0].action_id = first.action_id
        duplicate_action_record = store.create(duplicate_action_incident, "thread-duplicate-action")
        duplicate_action_record.runbook_plan = duplicate_action
        with pytest.raises(IntegrityError):
            store.update(duplicate_action_record)

        duplicate_key_incident = _incident()
        related_incidents.append(duplicate_key_incident.incident_id)
        duplicate_key = _plan()
        duplicate_key.actions[0].idempotency_key = first.idempotency_key
        duplicate_key_record = store.create(duplicate_key_incident, "thread-duplicate-key")
        duplicate_key_record.runbook_plan = duplicate_key
        with pytest.raises(IntegrityError):
            store.update(duplicate_key_record)
    finally:
        for incident_id in related_incidents:
            store.delete(incident_id)
        store.repository.engine.dispose()


@pytest.mark.asyncio
async def test_orchestrator_keeps_execution_behavior_and_binds_identity(monkeypatch):
    async def execute_single_action(action_name, *_args, **_kwargs):
        return MockActionResult(action_name=action_name, success=True, message="executed")

    orchestrator = ActionOrchestrator()
    monkeypatch.setattr(orchestrator, "_execute_single_action", execute_single_action)
    incident = _incident()
    plan = RunbookPlan(actions=[ActionInstruction(action="notify_dispatcher")])

    results = await orchestrator.execute_plan(incident, plan, "thread-orchestrator-identity")

    assert [result.success for result in results] == [True]
    assert plan.actions[0].idempotency_key is not None
