import asyncio

import pytest

from app.agents.action_orchestrator import ActionOrchestrator
from app.events.timeout_manager import ExecutionResult, TimeoutManager
from app.models.incident import ExecutionOutcome
from app.models.incident import ActionInstruction, Incident, MockActionResult, RetryPolicy, RunbookPlan
from app.tools.mock_actions import ActionRisk, get_action_metadata


class HttpError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


def test_registry_assigns_independent_execution_and_risk_policies():
    restart = get_action_metadata("restart_gateway")
    notify = get_action_metadata("notify_dispatcher")
    rollback = get_action_metadata("rollback_block_suspicious_source")

    assert restart is not None
    assert notify is not None
    assert rollback is not None
    assert restart.timeout_seconds == 30.0
    assert restart.retry_policy.max_retries == 1
    assert restart.risk == ActionRisk.HIGH
    assert restart.requires_approval is True
    assert notify.timeout_seconds == 5.0
    assert notify.retry_policy.max_retries == 3
    assert rollback.timeout_seconds == 6.0
    assert rollback.retry_policy.max_retries == 2


@pytest.mark.asyncio
async def test_non_retryable_http_400_stops_after_first_attempt():
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        raise HttpError(400)

    result = await TimeoutManager().execute_with_timeout(
        operation,
        "http-400",
        retry_policy=RetryPolicy(max_retries=3, base_delay_seconds=0),
    )

    assert attempts == 1
    assert result.success is False
    assert result.escalated is True
    assert result.retry_count == 0
    assert result.error_type == "http_400"
    assert result.error_message == "HTTP 400"
    assert isinstance(result.error, str)
    assert result.retryable is False
    assert result.retry_exhausted is True
    assert result.outcome is ExecutionOutcome.FAILED


@pytest.mark.asyncio
async def test_retryable_http_500_retries_and_recovers():
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HttpError(500)
        return "recovered"

    result = await TimeoutManager().execute_with_timeout(
        operation,
        "http-500",
        retry_policy=RetryPolicy(max_retries=3, base_delay_seconds=0),
    )

    assert attempts == 2
    assert result.success is True
    assert result.result == "recovered"
    assert result.retry_count == 1
    assert result.retry_exhausted is False
    assert result.outcome is ExecutionOutcome.SUCCESS


@pytest.mark.asyncio
async def test_unknown_exception_is_structured_without_retry():
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("delivery status unavailable")

    result = await TimeoutManager().execute_with_timeout(
        operation,
        "unknown-delivery",
        retry_policy=RetryPolicy(max_retries=3, base_delay_seconds=0),
    )

    assert attempts == 1
    assert result.success is False
    assert result.outcome is ExecutionOutcome.UNKNOWN
    assert result.error_type == "unknown"
    assert result.error_message == "delivery status unavailable"
    assert result.side_effect_possible is True
    assert result.retryable is False


@pytest.mark.asyncio
async def test_non_idempotent_timeout_reports_unknown_outcome():
    async def operation():
        await asyncio.sleep(0.05)

    result = await TimeoutManager().execute_with_timeout(
        operation,
        "side-effect-timeout",
        timeout_seconds=0.001,
        retry_policy=RetryPolicy(max_retries=2, base_delay_seconds=0),
        uncertain_on_timeout=True,
    )

    assert result.success is False
    assert result.final_action == "unknown"
    assert result.outcome is ExecutionOutcome.UNKNOWN


@pytest.mark.asyncio
async def test_retry_exhausted_timeout_reports_response_lost():
    async def operation():
        await asyncio.sleep(0.05)

    result = await TimeoutManager().execute_with_timeout(
        operation,
        "idempotent-timeout",
        timeout_seconds=0.001,
        retry_policy=RetryPolicy(max_retries=1, base_delay_seconds=0),
    )

    assert result.success is False
    assert result.escalated is True
    assert result.outcome is ExecutionOutcome.RESPONSE_LOST
    assert result.error_type == "timeout"
    assert result.error_message is not None
    assert isinstance(result.error, str)
    assert result.retry_count == 1
    assert result.retry_exhausted is True
    assert result.side_effect_possible is True


@pytest.mark.asyncio
async def test_success_result_metadata_is_preserved():
    expected = MockActionResult(
        action_name="switch_backup_link",
        target="section-a",
        success=True,
        side_effect_possible=True,
        side_effect_confirmed=True,
    )

    async def operation():
        return expected

    result = await TimeoutManager().execute_with_timeout(operation, "switch-backup-link")

    assert result.action_name == "switch_backup_link"
    assert result.target == "section-a"
    assert result.side_effect_possible is True
    assert result.side_effect_confirmed is True


@pytest.mark.asyncio
async def test_explicit_non_retryable_action_result_is_honored():
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        return MockActionResult(
            action_name="restart_gateway",
            success=False,
            message="permission denied",
            error_type="permission_denied",
            retryable=False,
        )

    result = await TimeoutManager().execute_with_timeout(
        operation,
        "restart-gateway",
        retry_policy=RetryPolicy(max_retries=3, base_delay_seconds=0),
    )

    assert attempts == 1
    assert result.retryable is False
    assert result.retry_exhausted is True
    assert result.outcome is ExecutionOutcome.FAILED
    assert result.error_type == "permission_denied"


@pytest.mark.asyncio
async def test_orchestrator_uses_registered_timeout_and_retry_policy(monkeypatch):
    captured = {}
    orchestrator = ActionOrchestrator()

    async def execute_with_timeout(**kwargs):
        captured.update(kwargs)
        return ExecutionResult(
            success=True,
            result=MockActionResult(
                action_name="verify_network_health",
                success=True,
                message="healthy",
            ),
        )

    monkeypatch.setattr(orchestrator.timeout_manager, "execute_with_timeout", execute_with_timeout)
    plan = RunbookPlan(
        actions=[ActionInstruction(action="verify_network_health", description="检查网络健康")]
    )

    results = await orchestrator.execute_plan(Incident(), plan, "test-thread")

    assert results[0].success is True
    assert captured["timeout_seconds"] == 10.0
    assert captured["retry_policy"].max_retries == 2
