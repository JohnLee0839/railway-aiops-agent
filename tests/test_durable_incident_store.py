"""PostgreSQL integration tests for Phase 1 durable Incident persistence."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest

from app.core.incident_store import IncidentStore
from app.core.state_machine import StateMachine
from app.models.incident import (
    ActionInstruction,
    ApprovalAction,
    Incident,
    IncidentMetadata,
    IncidentSource,
    IncidentState,
    RunbookPlan,
    Severity,
    TriageResult,
)


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL durability integration tests")
    if not url.startswith("postgresql+"):
        pytest.fail("TEST_DATABASE_URL must use a PostgreSQL SQLAlchemy dialect")
    return url


def _incident() -> Incident:
    suffix = uuid.uuid4().hex[:10]
    return Incident(
        incident_id=f"INC-DURABLE-{suffix}",
        source=IncidentSource.STSRS,
        severity=Severity.P2,
        metadata=IncidentMetadata(train_id="T-001", signal_id="S-001", source_ip="10.0.0.8"),
        raw_payload={"alarm": "packet_loss"},
        metrics_snapshot={"packet_loss": 0.75, "latency": 300},
        attack_prediction={"attack_type": "DoS", "confidence": 0.98, "model_version": "test-v1"},
        description="durable-store-test",
    )


def _cleanup(store: IncidentStore, incident_id: str) -> None:
    store.delete(incident_id)
    store.repository.engine.dispose()


def test_incident_create_and_reload_across_store_instances(database_url: str):
    incident = _incident()
    store_a = IncidentStore(database_url)
    try:
        created = store_a.create(incident, "thread-durable-create")
        store_a.repository.engine.dispose()

        reloaded = IncidentStore(database_url).get(created.incident_id)
        assert reloaded is not None
        assert reloaded.incident.model_dump(mode="json") == incident.model_dump(mode="json")
        assert reloaded.thread_id == "thread-durable-create"
        assert reloaded.state is IncidentState.NEW
    finally:
        _cleanup(IncidentStore(database_url), incident.incident_id)


def test_state_and_transition_history_reload(database_url: str):
    incident = _incident()
    store = IncidentStore(database_url)
    try:
        record = store.create(incident, "thread-durable-state")
        machine = StateMachine()
        machine.transition(record, IncidentState.NEW, reason="created", triggered_by="test")
        machine.transition(record, IncidentState.TRIAGED, reason="triaged", triggered_by="test")
        machine.transition(record, IncidentState.PLANNED, reason="planned", triggered_by="test")
        store.update(record)

        reloaded = IncidentStore(database_url).get(incident.incident_id)
        assert reloaded is not None
        assert reloaded.state is IncidentState.PLANNED
        assert [(item.from_state, item.to_state) for item in reloaded.state_history] == [
            (IncidentState.NEW, IncidentState.NEW),
            (IncidentState.NEW, IncidentState.TRIAGED),
            (IncidentState.TRIAGED, IncidentState.PLANNED),
        ]
    finally:
        _cleanup(store, incident.incident_id)


def test_triage_result_reload(database_url: str):
    incident = _incident()
    store = IncidentStore(database_url)
    try:
        record = store.create(incident, "thread-durable-triage")
        triage = TriageResult(
            root_cause="packet loss",
            attack_type="DoS",
            severity=Severity.P2,
            impact_scope=["T-001"],
            confidence=0.91,
            evidence=["packet_loss=0.75"],
        )
        record.triage_result = triage.model_dump(mode="json")
        store.update(record)

        reloaded = IncidentStore(database_url).get(incident.incident_id)
        assert reloaded is not None
        assert reloaded.triage_result == triage.model_dump(mode="json")
    finally:
        _cleanup(store, incident.incident_id)


def test_full_runbook_plan_reload(database_url: str):
    incident = _incident()
    store = IncidentStore(database_url)
    try:
        record = store.create(incident, "thread-durable-plan")
        plan = RunbookPlan(
            actions=[
                ActionInstruction(
                    action="switch_backup_link",
                    arguments={"target": "device-a"},
                    description="切换备用链路",
                ),
                ActionInstruction(
                    action="restart_gateway",
                    arguments={"gateway_id": "gw-a"},
                    description="重启网关",
                ),
            ],
            rollback_actions=[
                ActionInstruction(
                    action="rollback_switch_backup_link",
                    arguments={"target": "device-a"},
                    description="恢复主链路",
                )
            ],
            source_kb="CaseKB",
            case_id="CASE-DURABLE",
            confidence=0.93,
            reasoning="matched durable test case",
            affected_assets=["device-a", "gw-a"],
            requires_approval=True,
            approval_actions=[ApprovalAction.RESTART_GATEWAY],
        )
        record.plan = plan.steps
        record.runbook_plan = plan
        store.update(record)

        reloaded = IncidentStore(database_url).get(incident.incident_id)
        assert reloaded is not None
        assert reloaded.runbook_plan is not None
        assert reloaded.runbook_plan.model_dump(mode="json") == plan.model_dump(mode="json")
        assert reloaded.plan == plan.steps
        assert IncidentStore(database_url).repository.get_plan(incident.incident_id) == plan
    finally:
        _cleanup(store, incident.incident_id)


def test_process_level_durability(database_url: str):
    incident_id = f"INC-DURABLE-PROCESS-{uuid.uuid4().hex[:10]}"
    source = "\n".join(
        [
            "from app.core.incident_store import IncidentStore",
            "from app.models.incident import Incident, IncidentSource",
            f"store = IncidentStore({database_url!r})",
            (
                "store.create(Incident(incident_id="
                f"{incident_id!r}, source=IncidentSource.MANUAL, description='written-by-process-a'), "
                "'thread-process-a')"
            ),
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

    store_b = IncidentStore(database_url)
    try:
        reloaded = store_b.get(incident_id)
        assert reloaded is not None
        assert reloaded.incident.description == "written-by-process-a"
        assert reloaded.thread_id == "thread-process-a"
    finally:
        _cleanup(store_b, incident_id)
