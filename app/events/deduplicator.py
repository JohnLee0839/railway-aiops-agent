"""
Deduplicator — 滑动时间窗口去重

- 窗口大小: 10 秒
- 窗口内相同 source_ip 或相同事件特征超过 3 条则合并为 1 条
- 合并后保留 duplicate_count / first_seen / last_seen
"""

import time
import hashlib
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict
from loguru import logger

from app.models.incident import Incident


@dataclass
class DedupWindowEntry:
    """去重窗口条目"""
    incident: Incident
    first_seen: float  # monotonic timestamp
    last_seen: float   # monotonic timestamp
    count: int = 1


class Deduplicator:
    """
    滑动时间窗口去重器（Metric-driven 版本）。

    窗口大小 = 10 秒，窗口内：
    - 相同 train_id + signal_id + 异常指标组合 超过 3 条 → 合并
    - 相同 source_ip 超过 3 条 → 合并
    - 不再依赖 attack_type（已由 EventNormalizer 在 dedup_key 中体现）
    """

    def __init__(self, window_seconds: float = 10.0, threshold: int = 3):
        """
        Args:
            window_seconds: 滑动窗口大小（秒）
            threshold: 触发合并的最小重复次数
        """
        self.window_seconds = window_seconds
        self.threshold = threshold
        # key → DedupWindowEntry
        self._window: Dict[str, DedupWindowEntry] = {}
        # 合并后的输出缓冲
        self._output_buffer: List[Incident] = []

    def process(self, incident: Incident) -> Optional[Incident]:
        """
        处理一个事件，返回 None 表示被合并暂存，
        返回 Incident 表示应被下游处理。

        Args:
            incident: 待处理事件

        Returns:
            去重后的 Incident（可能合并了多条），或 None（暂存在窗口中）
        """
        now = time.monotonic()

        # 1. 清理过期窗口条目
        self._clean_expired(now)

        # 2. 生成去重 key
        keys = self._make_keys(incident)

        # 3. 检查是否命中已有条目
        matched_key: Optional[str] = None
        for k in keys:
            if k in self._window:
                matched_key = k
                break

        if matched_key:
            # 命中：更新窗口条目
            entry = self._window[matched_key]
            entry.count += 1
            entry.last_seen = now
            entry.incident = incident  # 保留最新的事件

            # 达到阈值则输出合并结果
            if entry.count >= self.threshold:
                merged = self._merge(entry)
                del self._window[matched_key]
                logger.info(
                    f"[Deduplicator] 合并 {entry.count} 条事件 → "
                    f"incident_id={merged.incident_id}, "
                    f"key={matched_key}"
                )
                return merged
            return None

        else:
            # 未命中：所有 key 都作为新条目
            main_key = keys[0] if keys else self._fallback_key(incident)
            self._window[main_key] = DedupWindowEntry(
                incident=incident,
                first_seen=now,
                last_seen=now,
                count=1,
            )
            return None

    def flush(self) -> List[Incident]:
        """强制输出窗口中的所有待定事件"""
        now = time.monotonic()
        self._clean_expired(now)
        results = []
        for entry in self._window.values():
            results.append(entry.incident)
            logger.info(
                f"[Deduplicator] flush: incident_id={entry.incident.incident_id}, "
                f"count={entry.count}"
            )
        self._window.clear()
        return results

    def _clean_expired(self, now: float) -> None:
        """清理过期的窗口条目（超过窗口时间但未达阈值的，直接作为单条输出）"""
        expired_keys = []
        for k, entry in self._window.items():
            if now - entry.first_seen > self.window_seconds:
                expired_keys.append(k)

        for k in expired_keys:
            entry = self._window.pop(k)
            # 未达阈值的过期条目，作为独立事件输出
            incident = entry.incident
            incident.duplicate_count = entry.count
            incident.first_seen = datetime.utcfromtimestamp(entry.first_seen)
            incident.last_seen = datetime.utcfromtimestamp(entry.last_seen)
            self._output_buffer.append(incident)
            logger.info(
                f"[Deduplicator] 过期事件直接输出: incident_id={incident.incident_id}, "
                f"count={entry.count}（未达阈值 {self.threshold}）"
            )

    def drain_output(self) -> List[Incident]:
        """导出缓冲的事件列表并清空"""
        result = self._output_buffer[:]
        self._output_buffer.clear()
        return result

    def _make_keys(self, incident: Incident) -> List[str]:
        """
        生成去重的匹配 key 列表（按优先级排序 — Metric-driven 版本）。

        核心键: dedup_key（由 EventNormalizer 生成）
        回退键: event_signature → train_id+signal_id → source_ip → metrics hash
        不再使用 attack_type 作为去重依据。
        """
        keys = []

        # Key 0: dedup_key（核心合并键 — EventNormalizer 已统一生成）
        if incident.dedup_key:
            keys.append(incident.dedup_key)

        # Key 1: event_signature
        if incident.event_signature:
            keys.append(incident.event_signature)

        # Key 2: source_ip
        if incident.source_ip:
            keys.append(f"ip:{incident.source_ip}")

        # Key 3: metadata source_ip (回退)
        if incident.metadata and incident.metadata.source_ip:
            keys.append(f"ip:{incident.metadata.source_ip}")

        # Key 4: train_id + signal_id + 时间窗口（Metric-driven 核心去重依据）
        meta = incident.metadata
        if meta and meta.train_id and meta.signal_id:
            # 按时间窗口（分钟级别）分组
            if incident.timestamp:
                time_bucket = incident.timestamp.strftime("%Y%m%d%H%M")
            else:
                time_bucket = "notime"
            keys.append(f"train:{meta.train_id}:sig:{meta.signal_id}:t:{time_bucket}")

        # Key 5: train_id + signal_id（跨时间窗口回退）
        if meta and meta.train_id and meta.signal_id:
            keys.append(f"train:{meta.train_id}:sig:{meta.signal_id}")

        # Key 6: metrics_snapshot hash（基于指标内容的回退）
        if incident.metrics_snapshot:
            metrics = incident.metrics_snapshot.get("metrics", {})
            if metrics:
                metrics_str = str(sorted(metrics.items()))
                h = hashlib.md5(metrics_str.encode()).hexdigest()[:8]
                keys.append(f"metrics:{h}")

        # Key 7: train_id only（如有）
        if meta and meta.train_id:
            keys.append(f"train:{meta.train_id}")

        return keys

    @staticmethod
    def _fallback_key(incident: Incident) -> str:
        """当无法生成任何 key 时的回退"""
        raw = str(incident.raw_payload or incident.description or "")
        h = hashlib.md5(raw.encode()).hexdigest()[:8]
        return f"hash:{h}"

    def _merge(self, entry: DedupWindowEntry) -> Incident:
        """合并窗口条目为单个 Incident"""
        incident = entry.incident
        incident.duplicate_count = entry.count
        incident.first_seen = datetime.utcfromtimestamp(entry.first_seen)
        incident.last_seen = datetime.utcfromtimestamp(entry.last_seen)
        return incident

    @property
    def pending_count(self) -> int:
        """待定事件数量"""
        return len(self._window)

    @property
    def buffer_count(self) -> int:
        """输出缓冲数量"""
        return len(self._output_buffer)
