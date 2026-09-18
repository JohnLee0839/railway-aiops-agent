import pytest
from pydantic import ValidationError

from app.agents.action_orchestrator import ActionOrchestrator
from app.models.incident import (
    ActionInstruction,
    Incident,
    MockActionResult,
    RunbookPlan,
)
from app.tools.mock_actions import RegisteredAction, get_action_metadata


def test_action_instruction_rejects_unregistered_action_name():
    with pytest.raises(ValidationError):
        ActionInstruction(action="switch the backup link")


def test_runbook_plan_requires_structured_actions():
    with pytest.raises(ValidationError):
        RunbookPlan()


@pytest.mark.asyncio
async def test_orchestrator_uses_structured_action_and_arguments(monkeypatch):
    captured = {}

    def switch_tool(source: str, target: str):
        captured["source"] = source
        captured["target"] = target
        return MockActionResult(
            action_name="switch_backup_link",
            success=True,
            message="switched",
        )

    switch_metadata = get_action_metadata("switch_backup_link")
    assert switch_metadata is not None
    monkeypatch.setattr(
        "app.agents.action_orchestrator.get_registered_action",
        lambda action_name: (
            RegisteredAction(handler=switch_tool, metadata=switch_metadata)
            if action_name == "switch_backup_link"
            else None
        ),
    )
    plan = RunbookPlan(
        actions=[
            ActionInstruction(
                action="switch_backup_link",
                arguments={"source": "primary-A", "target": "backup-B"},
                description="将受影响的线路切换到备用路径",
            )
        ]
    )

    results = await ActionOrchestrator().execute_plan(Incident(), plan, "test-thread")

    assert captured == {"source": "primary-A", "target": "backup-B"}
    assert results[0].success is True
    assert plan.steps == ["将受影响的线路切换到备用路径"]
