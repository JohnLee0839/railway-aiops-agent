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


def _plan():
    return RunbookPlan(
        actions=[ActionInstruction(action="verify_network_health")],
    )


def _result(action="verify_network_health", **kwargs):
    return MockActionResult(action_name=action, success=False, **kwargs)


@pytest.mark.asyncio
async def test_empty_results_are_failed():
    result = await Verifier().verify(Incident(), _plan(), [], "thread")
    assert result.action_status is ActionStatus.FAILED


@pytest.mark.asyncio
async def test_all_success_is_success():
    results = [
        MockActionResult(
            action_name="verify_network_health",
            success=True,
            outcome=ExecutionOutcome.SUCCESS.value,
        )
    ]
    result = await Verifier().verify(Incident(), _plan(), results, "thread")
    assert result.action_status is ActionStatus.SUCCESS


@pytest.mark.asyncio
async def test_explicit_retryable_failure_retries_until_plan_limit():
    result = await Verifier().verify(
        Incident(),
        _plan(),
        [_result(retryable=True, retry_exhausted=False, outcome="failed")],
        "thread",
        retry_cycle=0,
    )
    assert result.action_status is ActionStatus.RETRY


@pytest.mark.asyncio
async def test_retry_exhausted_failure_does_not_retry():
    result = await Verifier().verify(
        Incident(),
        _plan(),
        [_result(retryable=True, retry_exhausted=True, outcome="failed")],
        "thread",
        retry_cycle=0,
    )
    assert result.action_status is ActionStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unknown", "response_lost"])
async def test_uncertain_outcomes_escalate(outcome):
    results = [
        MockActionResult(
            action_name="switch_backup_link",
            success=False,
            outcome=outcome,
            side_effect_possible=True,
        )
    ]
    result = await Verifier().verify(Incident(), _plan(), results, "thread")
    assert result.action_status is ActionStatus.ESCALATE


@pytest.mark.asyncio
async def test_success_plus_response_lost_escalates():
    results = [
        MockActionResult(action_name="notify_dispatcher", success=True),
        MockActionResult(
            action_name="switch_backup_link",
            success=False,
            outcome="response_lost",
            side_effect_possible=True,
        ),
        MockActionResult(action_name="verify_network_health", success=True),
    ]
    result = await Verifier().verify(Incident(), _plan(), results, "thread")
    assert result.action_status is ActionStatus.ESCALATE


@pytest.mark.asyncio
async def test_not_sent_retries_only_with_explicit_authorization():
    retry = await Verifier().verify(
        Incident(),
        _plan(),
        [_result(outcome="not_sent", retryable=True, retry_exhausted=False)],
        "thread",
    )
    failed = await Verifier().verify(
        Incident(),
        _plan(),
        [_result(outcome="not_sent", retryable=False, retry_exhausted=True)],
        "thread",
    )
    assert retry.action_status is ActionStatus.RETRY
    assert failed.action_status is ActionStatus.FAILED


@pytest.mark.asyncio
async def test_permission_denied_is_failed_without_retry_flag():
    result = await Verifier().verify(
        Incident(),
        _plan(),
        [_result(error_type="permission_denied", outcome="failed")],
        "thread",
    )
    assert result.action_status is ActionStatus.FAILED


@pytest.mark.asyncio
async def test_compensation_requires_confirmed_side_effect():
    result = await Verifier().verify(
        Incident(),
        _plan(),
        [
            _result(
                action="switch_backup_link",
                outcome="failed",
                side_effect_possible=True,
                side_effect_confirmed=True,
                retry_exhausted=True,
            )
        ],
        "thread",
        retry_cycle=2,
    )
    assert result.action_status is ActionStatus.COMPENSATE
    assert result.compensation_actions == ["switch_backup_link"]
