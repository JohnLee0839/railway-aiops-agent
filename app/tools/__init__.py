"""Tool exports for Agent usage.

Knowledge retrieval is loaded lazily because importing it initializes the Milvus
vector store. Mock actions stay eager so the action orchestrator can import them
without requiring Milvus in lightweight tests.
"""

from app.tools.mock_actions import (
    ALL_MOCK_ACTIONS,
    DEFAULT_FAILURE_RATE,
    HIGH_RISK_ACTIONS,
    ROLLBACK_MAP,
    block_suspicious_source,
    generate_ticket,
    get_action,
    get_rollback_action,
    has_rollback,
    notify_dispatcher,
    requires_approval,
    restart_gateway,
    rollback_block_suspicious_source,
    rollback_switch_backup_link,
    set_failure_rate,
    switch_backup_link,
    verify_network_health,
)


def _load_retrieve_knowledge():
    from app.tools.knowledge_tool import retrieve_knowledge

    return retrieve_knowledge


def _load_get_current_time():
    from app.tools.time_tool import get_current_time

    return get_current_time


def _load_query_prometheus_alerts():
    from app.tools.query_metrics_alerts import query_prometheus_alerts

    return query_prometheus_alerts


def _default_local_agent_tools():
    return (
        _load_retrieve_knowledge(),
        _load_get_current_time(),
        _load_query_prometheus_alerts(),
    )


def __getattr__(name: str):
    if name == "retrieve_knowledge":
        return _load_retrieve_knowledge()
    if name == "get_current_time":
        return _load_get_current_time()
    if name == "query_prometheus_alerts":
        return _load_query_prometheus_alerts()
    if name == "DEFAULT_LOCAL_AGENT_TOOLS":
        return _default_local_agent_tools()
    raise AttributeError(f"module 'app.tools' has no attribute {name!r}")


__all__ = [
    "DEFAULT_LOCAL_AGENT_TOOLS",
    "retrieve_knowledge",
    "get_current_time",
    "query_prometheus_alerts",
    "switch_backup_link",
    "restart_gateway",
    "block_suspicious_source",
    "notify_dispatcher",
    "generate_ticket",
    "verify_network_health",
    "rollback_switch_backup_link",
    "rollback_block_suspicious_source",
    "get_action",
    "get_rollback_action",
    "has_rollback",
    "requires_approval",
    "set_failure_rate",
    "ALL_MOCK_ACTIONS",
    "ROLLBACK_MAP",
    "HIGH_RISK_ACTIONS",
    "DEFAULT_FAILURE_RATE",
]
