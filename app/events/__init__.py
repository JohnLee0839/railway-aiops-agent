"""
事件驱动层模块（Metric-driven AIOps）
EventNormalizer → Deduplicator → SeverityEngine → TimeoutManager

Metric-driven 重构:
- EventNormalizer: attack_type=UNKNOWN, severity=P4（不在此阶段判断）
- Deduplicator: 基于 TrainID+SignalID+异常指标组合去重
- SeverityEngine: 基于指标影响程度（安全/通信/性能）评估
"""

from app.events.event_normalizer import EventNormalizer
from app.events.deduplicator import Deduplicator
from app.events.severity_engine import SeverityEngine
from app.events.timeout_manager import TimeoutManager

__all__ = [
    "EventNormalizer",
    "Deduplicator",
    "SeverityEngine",
    "TimeoutManager",
]
