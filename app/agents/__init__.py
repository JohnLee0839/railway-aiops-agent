"""
事件驱动 Agent 模块（Metric-driven AIOps）
TriageAgent → RunbookAgent → ActionOrchestrator → Verifier → Replanner

Metric-driven 重构:
- TriageAgent: 真正的诊断 Agent（输入 metrics_snapshot → 输出 attack_type + root_cause + severity）
- 注意: IncidentRouter 已移至 app.core.incident_router

为避免循环导入，不使用 eager import。
请直接从子模块导入: from app.agents.triage_agent import TriageAgent
"""

# Lazy imports to avoid circular dependencies
# Use: from app.agents.triage_agent import TriageAgent
# NOT: from app.agents import TriageAgent (may cause circular import)


def get_triage_agent():
    """Lazy accessor for TriageAgent"""
    from app.agents.triage_agent import TriageAgent
    return TriageAgent


def get_runbook_agent():
    """Lazy accessor for RunbookAgent"""
    from app.agents.runbook_agent import RunbookAgent
    return RunbookAgent


def get_action_orchestrator():
    """Lazy accessor for ActionOrchestrator"""
    from app.agents.action_orchestrator import ActionOrchestrator
    return ActionOrchestrator


def get_verifier():
    """Lazy accessor for Verifier"""
    from app.agents.verifier import Verifier
    return Verifier


def get_replanner():
    """Lazy accessor for Replanner"""
    from app.agents.replanner import Replanner
    return Replanner


__all__ = [
    "get_triage_agent",
    "get_runbook_agent",
    "get_action_orchestrator",
    "get_verifier",
    "get_replanner",
]
