"""
EventNormalizer — 统一异构事件源为 Incident 对象

Metric-driven AIOps 重构:
- 删除 STSRS_ATTACK_MAP（不再把 attack_code 映射为 AttackType）
- 删除 PROMETHEUS_ALERT_MAP 中攻击类映射
- 所有来源的 attack_type 默认为 UNKNOWN（由 TriageAgent 诊断决定）
- severity 默认为 P4（由 SeverityEngine 初步评估 + TriageAgent 确认）
- 新增 _normalize_metric() 处理 RailMetricRecord / PrometheusMetricSnapshot
- event_signature 和 dedup_key 不再依赖 attack_type
"""

import uuid
import hashlib
from typing import Dict, Any, Optional
from datetime import datetime
from loguru import logger

from app.models.incident import (
    Incident,
    IncidentSource,
    AttackType,
    Severity,
    IncidentMetadata,
)


class EventNormalizer:
    """
    将异构事件源统一为 Incident 对象。

    支持来源：
    - prometheus: Prometheus AlertManager webhook / PrometheusMetricSnapshot
    - mcp: MCP 工具返回的告警数据
    - stsrs: 列车信号安全系统原始监测指标（不含攻击标签）
    - metric: RailMetricRecord / PrometheusMetricSnapshot（新增）
    - manual: 手工输入 / API 请求

    Metric-driven 设计原则:
    - 不在此阶段判断攻击类型（attack_type = UNKNOWN）
    - 不在此阶段判断严重级别（severity = P4，由 SeverityEngine 覆盖）
    - 只负责格式标准化、ID 生成、基础上下文字段填充
    """

    # ================================================================
    # Prometheus 告警名 → AttackType 映射（仅保留基础设施类告警）
    # 攻击类映射已删除 — 由 TriageAgent 负责诊断
    # ================================================================
    PROMETHEUS_ALERT_MAP: Dict[str, AttackType] = {
        "HighCPUUsage": AttackType.CPU_HIGH,
        "HighMemoryUsage": AttackType.MEMORY_HIGH,
        "HighDiskUsage": AttackType.DISK_HIGH,
        "ServiceDown": AttackType.SERVICE_UNAVAILABLE,
        "HighLatency": AttackType.SLOW_RESPONSE,
        "NetworkPartition": AttackType.NETWORK_PARTITION,
        # 以下攻击类映射已删除（Metric-driven: 不再从告警名推断攻击类型）:
        # "PacketFlood" → 删除（原映射为 DOS）
        # "SignalJamming" → 删除（原映射为 JAMMING）
        # "UnauthorizedLogin" → 删除（原映射为 UNAUTHORIZED_ACCESS）
    }

    # ================================================================
    # 公共方法
    # ================================================================

    def normalize(
        self,
        raw: Dict[str, Any],
        source: IncidentSource,
    ) -> Incident:
        """
        将原始事件归一化为 Incident。

        Metric-driven: attack_type=UNKNOWN, severity=P4（后续阶段覆盖）

        Args:
            raw: 原始事件数据
            source: 事件来源枚举

        Returns:
            Incident 对象
        """
        handler = {
            IncidentSource.PROMETHEUS: self._normalize_prometheus,
            IncidentSource.MCP: self._normalize_mcp,
            IncidentSource.STSRS: self._normalize_stsrs,
            IncidentSource.MANUAL: self._normalize_manual,
        }

        normalizer = handler.get(source, self._normalize_generic)
        incident = normalizer(raw)
        incident.source = source
        incident.timestamp = datetime.utcnow()
        incident.trace_id = f"trace-{uuid.uuid4().hex[:12]}"

        # ---- Metric-driven: 统一生成去重字段（不依赖 attack_type） ----
        incident.source_ip = self._extract_source_ip(incident)
        incident.event_signature = self._build_event_signature(incident)
        incident.dedup_key = self._build_dedup_key(incident)

        # ---- 提取 metrics_snapshot（如原始数据中存在） ----
        extracted_metrics = self._extract_metrics_snapshot(raw, incident)
        if extracted_metrics is not None:
            incident.metrics_snapshot = extracted_metrics

        logger.info(
            f"[EventNormalizer] 归一化完成: id={incident.incident_id}, "
            f"source={source.value}, attack_type={incident.attack_type.value}, "
            f"severity={incident.severity.value}, "
            f"has_metrics={incident.metrics_snapshot is not None}"
        )
        return incident

    # ================================================================
    # Metric 来源归一化（新增 — 核心变化）
    # ================================================================

    def normalize_metric(
        self,
        metrics_snapshot: Dict[str, Any],
        source: IncidentSource = IncidentSource.STSRS,
        attack_prediction: Optional[Dict[str, Any]] = None,
    ) -> Incident:
        """
        将 RailMetricRecord / PrometheusMetricSnapshot 归一化为 Incident。

        这是 Metric-driven AIOps 的核心入口：
        - 不假设攻击类型
        - attack_prediction 来自监督学习模型（AttackDetector）
        - 只提取指标快照和基础上下文

        Args:
            metrics_snapshot: 序列化的 RailMetricRecord 或 PrometheusMetricSnapshot
            source: 来源（通常是 STSRS 或 PROMETHEUS）
            attack_prediction: 监督学习模型预测结果（AttackPrediction.model_dump()）

        Returns:
            Incident（attack_type=UNKNOWN, severity=P4, attack_prediction=prediction）
        """
        train_id = metrics_snapshot.get("train_id", "")
        signal_id = metrics_snapshot.get("signal_id", "")
        labels = metrics_snapshot.get("labels", {})

        # 从 labels 或顶层字段提取 train_id / signal_id
        if not train_id and labels:
            train_id = labels.get("train", labels.get("train_id", ""))
        if not signal_id and labels:
            signal_id = labels.get("signal", labels.get("signal_id", ""))

        # 提取纯指标数据
        metrics = metrics_snapshot.get("metrics", {})
        if not metrics:
            # 可能是 RailMetricRecord 格式: metrics 嵌套在 metrics 对象中
            inner = metrics_snapshot.get("metrics", {})
            if isinstance(inner, dict):
                # 可能是 Pydantic model dump
                metrics = {
                    k: v for k, v in inner.items()
                    if v is not None and k not in ("location",)
                }

        # 提取 source_metrics（多来源指标差异，如存在）
        source_metrics = metrics_snapshot.get("source_metrics", {})
        source_files = metrics_snapshot.get("source_files", [])

        metadata = IncidentMetadata(
            train_id=train_id or metrics_snapshot.get("train_id"),
            signal_id=signal_id or metrics_snapshot.get("signal_id"),
            extra={
                "source_record_id": metrics_snapshot.get("record_id", ""),
                "snapshot_id": metrics_snapshot.get("snapshot_id", ""),
                "metric_count": len(metrics) if metrics else 0,
                "has_source_conflicts": bool(source_metrics),
                "source_count": len(source_metrics) if source_metrics else 0,
            },
        )

        # 构建增强的 metrics_snapshot（含 source_metrics 及冲突检测）
        enhanced_snapshot = {
            **metrics_snapshot,
            "source_metrics": source_metrics,
            "source_files": source_files,
            "has_source_conflicts": bool(source_metrics),
        }

        incident = Incident(
            incident_id=f"INC-MET-{uuid.uuid4().hex[:8].upper()}",
            source=source,
            attack_type=AttackType.UNKNOWN,   # ← 不在此阶段判断
            severity=Severity.P4,             # ← 由 SeverityEngine 覆盖
            metadata=metadata,
            metrics_snapshot=enhanced_snapshot,  # ← 保留完整指标快照（含 source_metrics）
            attack_prediction=attack_prediction,  # ← 监督学习模型预测结果
            description=f"Metric event: train={train_id}, signal={signal_id}",
            raw_payload=metrics_snapshot,
        )
        return incident

    # ================================================================
    # 各来源归一化器
    # ================================================================

    def _normalize_prometheus(self, raw: Dict[str, Any]) -> Incident:
        """
        Prometheus 来源 → Incident。

        支持两种格式:
        1. AlertManager webhook: {"alerts": [{"labels": {...}, "annotations": {...}}]}
        2. PrometheusMetricSnapshot: {"labels": {...}, "metrics": {...}}

        Metric-driven: 攻击类告警名不再映射为 AttackType（保留为 UNKNOWN）
        """
        # 检测是否为 PrometheusMetricSnapshot 格式
        if "metrics" in raw and "labels" in raw:
            return self.normalize_metric(raw, IncidentSource.PROMETHEUS)

        alert = raw.get("alerts", [{}])[0] if raw.get("alerts") else raw
        annotations = alert.get("annotations", {})
        labels = alert.get("labels", {})

        alert_name = labels.get("alertname", alert.get("alertname", "Unknown"))
        attack_type = self.PROMETHEUS_ALERT_MAP.get(alert_name, AttackType.UNKNOWN)

        source_ip = labels.get("instance", "").split(":")[0] if ":" in labels.get("instance", "") else labels.get("instance")
        target_ip = labels.get("target", annotations.get("target"))

        # 提取 Prometheus 指标值（如有）
        metrics_snapshot = None
        if "metrics" in raw:
            metrics_snapshot = {
                "timestamp": raw.get("timestamp", ""),
                "labels": labels,
                "metrics": raw["metrics"],
                "source_metrics": raw.get("source_metrics", {}),
                "source_files": raw.get("source_files", []),
            }

        metadata = IncidentMetadata(
            source_ip=source_ip,
            target_ip=target_ip,
            raw_alert=raw,
            extra={
                "alert_status": raw.get("status", "unknown"),
                "fingerprint": alert.get("fingerprint", ""),
                "starts_at": alert.get("startsAt", ""),
            },
        )

        incident = Incident(
            incident_id=f"INC-PROM-{uuid.uuid4().hex[:8].upper()}",
            attack_type=attack_type,
            severity=self._prometheus_severity(labels.get("severity", "warning")),
            metadata=metadata,
            source_ip=source_ip,
            metrics_snapshot=metrics_snapshot,
            description=annotations.get("summary", alert_name),
            raw_payload=raw,
        )
        return incident

    def _normalize_mcp(self, raw: Dict[str, Any]) -> Incident:
        """
        MCP 工具返回数据 → Incident。

        Metric-driven: attack_type 默认为 UNKNOWN。
        """
        if isinstance(raw, list):
            raw = raw[0] if raw else {"message": "empty mcp response"}

        alert_name = raw.get("alertname", raw.get("name", "MCP Alert"))
        # 仅保留基础设施告警映射，攻击类统一为 UNKNOWN
        attack_type = AttackType.UNKNOWN

        metadata = IncidentMetadata(
            source_ip=raw.get("instance", raw.get("host")),
            raw_alert=raw,
            extra={"mcp_server": raw.get("_mcp_server", "unknown")},
        )

        incident = Incident(
            incident_id=f"INC-MCP-{uuid.uuid4().hex[:8].upper()}",
            attack_type=attack_type,
            severity=self._prometheus_severity(raw.get("severity", "info")),
            metadata=metadata,
            source_ip=raw.get("instance", raw.get("host")),
            description=raw.get("description", raw.get("summary", alert_name)),
            raw_payload=raw,
        )
        return incident

    def _normalize_stsrs(self, raw: Dict[str, Any]) -> Incident:
        """
        STSRS 原始监测数据 → Incident。

        Metric-driven 重构:
        - 不再根据 attack_code 映射 AttackType
        - 检查是否为 RailMetricRecord 格式
        - attack_type 统一为 UNKNOWN
        """
        # 检测是否为 RailMetricRecord / PrometheusMetricSnapshot 格式
        if isinstance(raw.get("metrics_snapshot"), dict):
            return self.normalize_metric(raw["metrics_snapshot"], IncidentSource.STSRS)

        if "metrics" in raw and ("train_id" in raw or "labels" in raw):
            return self.normalize_metric(raw, IncidentSource.STSRS)

        # 旧格式兼容: 即使有 attack_code 也不映射，只提取基础信息
        metadata = IncidentMetadata(
            train_id=raw.get("train_id"),
            signal_id=raw.get("signal_id"),
            control_center=raw.get("control_center"),
            extra={
                "region": raw.get("region", ""),
                "line": raw.get("line", ""),
                "position_km": raw.get("position_km"),
            },
        )

        # 尝试提取指标数据
        metrics_snapshot = self._extract_metrics_snapshot(raw, None)

        # 描述中注明原始数据含有的 attack_code（如有），但不作为攻击类型判断
        description = raw.get("description", "")
        attack_code = raw.get("attack_code", raw.get("code", ""))
        if attack_code and not description:
            description = f"STSRS observation (code: {attack_code})"

        incident = Incident(
            incident_id=f"INC-STSRS-{uuid.uuid4().hex[:8].upper()}",
            attack_type=AttackType.UNKNOWN,     # ← Metric-driven: 不在此判断
            severity=Severity.P4,               # ← 由 SeverityEngine 覆盖
            metadata=metadata,
            metrics_snapshot=metrics_snapshot,
            description=description,
            raw_payload=raw,
        )
        return incident

    def _normalize_manual(self, raw: Dict[str, Any]) -> Incident:
        """
        手工输入 → Incident。

        Metric-driven: 用户可以手动指定 attack_type（测试/兼容用途），
        但默认仍为 UNKNOWN。
        """
        attack_type_str = raw.get("attack_type", "Unknown")
        try:
            attack_type = AttackType(attack_type_str)
        except ValueError:
            attack_type = AttackType.UNKNOWN

        source_ip = raw.get("source_ip")
        target_ip = raw.get("target_ip")
        train_id = raw.get("train_id")
        signal_id = raw.get("signal_id")
        control_center = raw.get("control_center")
        extra = raw.get("extra", {})

        # 提取指标快照（如有）
        metrics_snapshot = self._extract_metrics_snapshot(raw, None)

        metadata = IncidentMetadata(
            source_ip=source_ip,
            target_ip=target_ip,
            train_id=train_id,
            signal_id=signal_id,
            control_center=control_center,
            extra=extra,
        )

        incident = Incident(
            incident_id=raw.get("incident_id", f"INC-MAN-{uuid.uuid4().hex[:8].upper()}"),
            attack_type=attack_type,
            severity=Severity.P4,  # 手工输入默认 P4，后续 SeverityEngine 重新评估
            metadata=metadata,
            source_ip=source_ip,
            metrics_snapshot=metrics_snapshot,
            description=raw.get("description", "Manual incident report"),
            raw_payload=raw,
        )
        return incident

    def _normalize_generic(self, raw: Dict[str, Any]) -> Incident:
        """通用回退归一化"""
        return Incident(
            incident_id=f"INC-GEN-{uuid.uuid4().hex[:8].upper()}",
            source=IncidentSource.MANUAL,
            attack_type=AttackType.UNKNOWN,
            severity=Severity.P4,
            description=str(raw)[:500],
            raw_payload=raw if isinstance(raw, dict) else {"raw": str(raw)},
        )

    # ================================================================
    # 辅助方法
    # ================================================================

    @staticmethod
    def _extract_source_ip(incident: Incident) -> Optional[str]:
        """从 Incident 中提取 source_ip"""
        if incident.source_ip:
            return incident.source_ip
        if incident.metadata and incident.metadata.source_ip:
            return incident.metadata.source_ip
        if incident.raw_payload and isinstance(incident.raw_payload, dict):
            return incident.raw_payload.get("source_ip")
        return None

    @staticmethod
    def _extract_metrics_snapshot(
        raw: Dict[str, Any],
        incident: Optional[Incident],
    ) -> Optional[Dict[str, Any]]:
        """从原始数据中提取指标快照"""
        # 如果 raw 本身就包含 metrics 字段（RailMetricRecord/PrometheusMetricSnapshot 格式）
        if "metrics" in raw and isinstance(raw["metrics"], dict):
            return {
                "timestamp": str(raw.get("timestamp", "")),
                "train_id": raw.get("train_id", raw.get("labels", {}).get("train", "")),
                "signal_id": raw.get("signal_id", raw.get("labels", {}).get("signal", "")),
                "labels": raw.get("labels", {}),
                "metrics": raw["metrics"],
            }

        # 从 raw_payload 中提取指标字段
        metric_fields = {
            "speed", "distance", "location",
            "signal_status", "overlap_status", "overlap_count",
            "packet_loss", "latency", "renewal_interval", "burstiness",
        }
        found_metrics = {}
        for key in metric_fields:
            # 尝试多种大小写形式
            for variant in (key, key.upper(), key.title(), key.replace("_", "")):
                if variant in raw and raw[variant] is not None:
                    try:
                        found_metrics[key] = float(raw[variant]) if key not in (
                            "location", "signal_status", "overlap_status",
                        ) else str(raw[variant])
                    except (ValueError, TypeError):
                        found_metrics[key] = str(raw[variant])
                    break

        if found_metrics:
            train_id = ""
            signal_id = ""
            if incident and incident.metadata:
                train_id = incident.metadata.train_id or ""
                signal_id = incident.metadata.signal_id or ""

            return {
                "timestamp": str(raw.get("timestamp", "")),
                "train_id": raw.get("train_id", train_id),
                "signal_id": raw.get("signal_id", signal_id),
                "labels": {},
                "metrics": found_metrics,
                "source_metrics": raw.get("source_metrics", {}),
                "source_files": raw.get("source_files", []),
            }

        return None

    @staticmethod
    def _build_event_signature(incident: Incident) -> str:
        """
        构建 event_signature（Metric-driven 版本）。

        不再依赖 attack_type，改为基于:
        - train_id + signal_id（关键资产标识）
        - metrics hash（如果存在 metrics_snapshot）

        示例: "Train-1H66:Signal-YT546:abc12345"
        """
        parts = []

        if incident.metadata.train_id:
            parts.append(incident.metadata.train_id)
        if incident.metadata.signal_id:
            parts.append(incident.metadata.signal_id)
        if incident.metadata.control_center:
            parts.append(incident.metadata.control_center)
        if incident.metadata.source_ip:
            parts.append(incident.metadata.source_ip)

        # 基于 metrics_snapshot 的 hash（如果有）
        if incident.metrics_snapshot:
            metrics_str = str(sorted(incident.metrics_snapshot.get("metrics", {}).items()))
            h = hashlib.md5(metrics_str.encode()).hexdigest()[:8]
            parts.append(h)

        return ":".join(parts) if parts else "unknown"

    @staticmethod
    def _build_dedup_key(incident: Incident) -> str:
        """
        构建 dedup_key（核心去重键 — Metric-driven 版本）。

        优先级（不再依赖 attack_type）:
        1. train_id + signal_id + metrics hash（最精确 — 同一设备异常指标）
        2. train_id + signal_id（基于资产标识）
        3. source_ip（IP 级别）
        4. hash(metrics_snapshot)（回退）
        """
        key_parts = []

        # 优先级 1: train_id + signal_id + metrics hash
        if incident.metadata.train_id and incident.metadata.signal_id:
            base = f"train:{incident.metadata.train_id}:sig:{incident.metadata.signal_id}"
            if incident.metrics_snapshot:
                metrics = incident.metrics_snapshot.get("metrics", {})
                # 找出异常指标名
                abnormal = sorted(k for k, v in metrics.items() if v is not None)
                if abnormal:
                    base += f":abnormal:{','.join(abnormal)}"
            key_parts.append(base)

        # 优先级 2: source_ip
        src = incident.source_ip
        if src:
            key_parts.append(f"ip:{src}")

        # 优先级 3: event_signature
        sig = incident.event_signature
        if sig:
            key_parts.append(f"sig:{sig}")

        # 优先级 4: metrics hash（最模糊回退）
        if incident.metrics_snapshot:
            metrics = incident.metrics_snapshot.get("metrics", {})
            metrics_str = str(sorted(metrics.items()))
            h = hashlib.md5(metrics_str.encode()).hexdigest()[:8]
            key_parts.append(f"metrics:{h}")

        # 优先级 5: 纯 hash 回退
        if not key_parts:
            raw = str(incident.raw_payload or incident.description or "")
            h = hashlib.md5(raw.encode()).hexdigest()[:8]
            key_parts.append(f"hash:{h}")

        return ":".join(key_parts)

    @staticmethod
    def _prometheus_severity(severity_str: str) -> Severity:
        mapping = {
            "critical": Severity.P1,
            "warning": Severity.P3,
            "info": Severity.P4,
        }
        return mapping.get(severity_str.lower(), Severity.P4)
