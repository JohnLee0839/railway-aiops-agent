import time
from dataclasses import replace

import pytest

from app.agents.verifier import Verifier
from app.models.incident import (
    ActionInstruction,
    ActionStatus,
    ExecutionOutcome,
    Incident,
    MockActionResult,
    RunbookPlan,
)
from app.tools import mock_actions
from app.tools.mock_actions import (
    get_action_metadata,
    query_device_link_state,
    reset_mock_operational_state,
    switch_backup_link,
)


@pytest.fixture(autouse=True)
def clean_mock_state():
    reset_mock_operational_state()
    yield
    reset_mock_operational_state()


def _plan(action="switch_backup_link", **arguments):
    return RunbookPlan(
        actions=[ActionInstruction(action=action, arguments=arguments)],
    )


def _successful_switch(target="device-a"):
    config = mock_actions.ACTION_CONFIGS["switch_backup_link"]
    old_rate = config.failure_rate
    config.failure_rate = 0
    try:
        return switch_backup_link(target=target)
    finally:
        config.failure_rate = old_rate


@pytest.mark.asyncio
async def test_successful_command_requires_and_passes_state_readback():
    result = _successful_switch()

    verification = await Verifier().verify(
        Incident(), _plan(target="device-a"), [result], "thread"
    )

    assert verification.action_status is ActionStatus.SUCCESS
    assert "expected_link=backup" in verification.evidence
    assert "observed_link=backup" in verification.evidence
    assert "verifier=query_device_link_state" in verification.evidence
    assert result.side_effect_confirmed is True


@pytest.mark.asyncio
async def test_successful_command_with_wrong_observed_state_is_failed():
    result = MockActionResult(
        action_name="switch_backup_link",
        target="device-a",
        success=True,
        outcome=ExecutionOutcome.SUCCESS.value,
    )

    verification = await Verifier().verify(
        Incident(), _plan(target="device-a"), [result], "thread"
    )

    assert verification.action_status is ActionStatus.FAILED
    assert "expected_link=backup" in verification.evidence
    assert "observed_link=primary" in verification.evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [ExecutionOutcome.RESPONSE_LOST, ExecutionOutcome.UNKNOWN])
async def test_uncertain_execution_is_reconciled_when_probe_confirms_target(outcome):
    applied = _successful_switch()
    result = applied.model_copy(
        update={
            "success": False,
            "outcome": outcome.value,
            "side_effect_possible": True,
            "side_effect_confirmed": False,
        }
    )

    verification = await Verifier().verify(
        Incident(), _plan(target="device-a"), [result], "thread"
    )

    assert verification.action_status is ActionStatus.SUCCESS
    assert result.side_effect_confirmed is True
    assert "observed_link=backup" in verification.evidence


@pytest.mark.asyncio
async def test_response_lost_with_unchanged_state_is_failed_without_replay():
    result = MockActionResult(
        action_name="switch_backup_link",
        target="device-a",
        success=False,
        outcome=ExecutionOutcome.RESPONSE_LOST.value,
        side_effect_possible=True,
    )

    verification = await Verifier().verify(
        Incident(), _plan(target="device-a"), [result], "thread"
    )

    assert verification.action_status is ActionStatus.FAILED
    assert verification.retry_recommended is False
    assert "observed_link=primary" in verification.evidence


@pytest.mark.asyncio
async def test_unknown_execution_escalates_when_probe_times_out(monkeypatch):
    def slow_read_only_probe(target):
        time.sleep(0.05)
        return {"link": "backup"}

    metadata = get_action_metadata("switch_backup_link")
    assert metadata is not None
    monkeypatch.setattr(
        "app.agents.verifier.get_action_metadata",
        lambda _: replace(
            metadata,
            state_verifier=slow_read_only_probe,
            verifier_timeout_seconds=0.001,
        ),
    )
    result = MockActionResult(
        action_name="switch_backup_link",
        target="device-a",
        success=False,
        outcome=ExecutionOutcome.UNKNOWN.value,
        side_effect_possible=True,
    )

    verification = await Verifier().verify(
        Incident(), _plan(target="device-a"), [result], "thread"
    )

    assert verification.action_status is ActionStatus.ESCALATE
    assert "probe_error=timeout" in verification.evidence


def test_link_state_probe_is_read_only(monkeypatch):
    def forbidden_side_effect(*args, **kwargs):
        raise AssertionError("state probe must not execute an action handler")

    monkeypatch.setattr(mock_actions, "switch_backup_link", forbidden_side_effect)

    assert query_device_link_state("device-a") == {"link": "primary"}
    assert query_device_link_state("device-a") == {"link": "primary"}
