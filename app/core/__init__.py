"""
核心模块。

注意: 为避免循环导入，IncidentRouter 不在此处 eager import。
请直接从子模块导入: from app.core.incident_router import IncidentRouter
"""

from app.core.state_machine import state_machine, StateMachine
from app.core.incident_store import incident_store, IncidentStore
from app.core.audit_store import audit_store, AuditStore
from app.core.llm_factory import LLMFactory
from app.core.milvus_client import milvus_manager, MilvusClientManager

# IncidentRouter 延迟导入 — 避免循环依赖:
#   app.core.incident_router → app.agents.triage_agent → app.tools → app.core.milvus_client → app.core.__init__
# 如果需要 IncidentRouter，请从 app.core.incident_router 直接导入:
#   from app.core.incident_router import IncidentRouter

__all__ = [
    "state_machine",
    "StateMachine",
    "incident_store",
    "IncidentStore",
    "audit_store",
    "AuditStore",
    "LLMFactory",
    "milvus_manager",
    "MilvusClientManager",
]
