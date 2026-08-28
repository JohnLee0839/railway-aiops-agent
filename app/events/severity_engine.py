"""
SeverityEngine — 基于指标影响程度的严重级别评估（Metric-driven 重写）

评估依据（按优先级）:
1. SignalStatus + OverlapStatus → 影响列车运行安全 → P1
2. Speed 异常 + SignalStatus 异常 → 影响列车运行安全 → P1
3. PacketLoss > 0.5 或 Latency > 200ms → 影响信号通信 → P2
4. RenewalInterval 异常 → 信号通信中断 → P2
5. PacketLoss > 0.1 或 Latency > 100ms → 性能下降 → P3
6. Burstiness 异常 → 性能下降 → P3
7. 轻微异常 / 无指标 → P4

动态升级规则:
- 涉及列车运行 + 任何通信异常 → 升级到 P1
- 高频重复事件（>10 次）→ 升级一级
- 多条指标同时异常 → 升级一级

不再依赖: AttackType → Severity 映射表
"""

from typing import Optional, Dict, Any, List
from loguru import logger

from app.models.incident import Incident, Severity


class SeverityEngine:
    """
    严重级别引擎（Metric-driven 版本）。

    基于实际监测指标的影响程度评估严重级别，不再依赖攻击类型分类。

    评估维度:
    - 安全维度: SignalStatus, OverlapStatus, Speed（列车运行安全）
    - 通信维度: PacketLoss, Latency, RenewalInterval（信号通信质量）
    - 性能维度: Burstiness, OverlapCount（系统性能）
    """

    # ================================================================
    # 阈值配置
    # ================================================================

    # P1 阈值 — 影响列车运行安全
    SIGNAL_STATUS_DANGER = frozenset({"RED", "DANGER", "OFFLINE", "FAILURE", "red", "danger", "offline", "failure"})
    OVERLAP_STATUS_ABNORMAL = frozenset({"ABNORMAL", "CONFLICT", "ERROR", "abnormal", "conflict", "error"})
    SPEED_SAFETY_THRESHOLD = 350.0  # km/h，超过此值为异常（高铁上限）

    # P2 阈值 — 影响信号通信
    PACKET_LOSS_CRITICAL = 0.5   # 50% 丢包
    LATENCY_CRITICAL = 200.0     # 200ms 延迟
    RENEWAL_INTERVAL_CRITICAL_MAX = 5000.0  # 续期间隔超过 5s

    # P3 阈值 — 性能下降
    PACKET_LOSS_MODERATE = 0.1   # 10% 丢包
    LATENCY_MODERATE = 100.0     # 100ms 延迟
    BURSTINESS_MODERATE = 0.5    # 突发度 > 0.5

    def evaluate(self, incident: Incident) -> Severity:
        """
        基于指标影响程度评估严重级别。

        评估优先级:
        1. 安全影响检查 → P1
        2. 通信影响检查 → P2
        3. 性能影响检查 → P3
        4. 默认 → P4

        Args:
            incident: 待评估事件

        Returns:
            Severity 枚举值
        """
        metrics = incident.metrics_snapshot
        meta = incident.metadata

        # ---- Step 1: 提取指标值 ----
        metrics_dict = self._flatten_metrics(metrics, meta, incident)

        # ---- Step 2: 安全影响评估 (P1) ----
        safety_risk = self._assess_safety_impact(metrics_dict, meta)
        if safety_risk:
            severity = Severity.P1
            logger.info(
                f"[SeverityEngine] P1 - 安全影响: {safety_risk}, "
                f"incident={incident.incident_id}"
            )
            incident.severity = severity
            return severity

        # ---- Step 3: 通信影响评估 (P2) ----
        comm_risk = self._assess_communication_impact(metrics_dict)
        if comm_risk:
            severity = Severity.P2
            logger.info(
                f"[SeverityEngine] P2 - 通信影响: {comm_risk}, "
                f"incident={incident.incident_id}"
            )
            incident.severity = severity
            # 如果有 train_id，涉及列车的通信中断升级到 P1
            if meta and meta.train_id:
                severity = Severity.P1
                logger.info(
                    f"[SeverityEngine] 涉及列车通信 → 升级到 P1: "
                    f"train_id={meta.train_id}"
                )
            incident.severity = severity
            return severity

        # ---- Step 4: 性能影响评估 (P3) ----
        perf_risk = self._assess_performance_impact(metrics_dict)
        if perf_risk:
            severity = Severity.P3
            logger.info(
                f"[SeverityEngine] P3 - 性能影响: {perf_risk}, "
                f"incident={incident.incident_id}"
            )
            # 多条指标同时异常 → 升级到 P2
            abnormal_count = sum(
                1 for k, v in metrics_dict.items()
                if self._is_metric_abnormal(k, v)
            )
            if abnormal_count >= 3:
                severity = Severity.P2
                logger.info(
                    f"[SeverityEngine] 多条指标异常 ({abnormal_count}) → 升级到 P2"
                )
            incident.severity = severity
            return severity

        # ---- Step 5: 默认 P4 ----
        severity = Severity.P4

        # ---- 动态升级规则 ----

        # 高频重复事件（>10 次）→ 升级一级
        if incident.duplicate_count > 10:
            severity = self._upgrade(severity)
            logger.info(
                f"[SeverityEngine] 高频重复 ({incident.duplicate_count}次) "
                f"触发升级: → {severity.value}"
            )

        # 涉及列车的事件（P3/P4 → 升级）
        if meta and meta.train_id and severity in (Severity.P3, Severity.P4):
            severity = self._upgrade(severity)
            logger.info(
                f"[SeverityEngine] 列车相关事件升级: "
                f"train_id={meta.train_id}, → {severity.value}"
            )

        incident.severity = severity
        logger.info(
            f"[SeverityEngine] 最终级别: {severity.value} "
            f"for incident_id={incident.incident_id}"
        )
        return severity

    # ================================================================
    # 影响评估子方法
    # ================================================================

    def _assess_safety_impact(
        self,
        metrics: Dict[str, Any],
        meta: Any,
    ) -> Optional[str]:
        """
        评估安全影响（→ P1）。

        Returns:
            安全风险描述，无风险则返回 None
        """
        risks = []

        # 信号状态异常
        signal_status = str(metrics.get("signal_status", "")).strip()
        if signal_status.upper() in self.SIGNAL_STATUS_DANGER:
            risks.append(f"SignalStatus={signal_status}")

        # 联锁重叠状态异常
        overlap_status = str(metrics.get("overlap_status", "")).strip()
        if overlap_status.upper() in self.OVERLAP_STATUS_ABNORMAL:
            risks.append(f"OverlapStatus={overlap_status}")

        # 速度异常 + 信号状态异常
        speed = self._get_numeric(metrics, "speed")
        if speed is not None and speed > self.SPEED_SAFETY_THRESHOLD:
            if signal_status.upper() in self.SIGNAL_STATUS_DANGER:
                risks.append(f"Speed={speed}km/h (异常) + SignalStatus异常")

        # 速度异常 + 信号异常 + 列车存在
        if meta and meta.train_id:
            if speed is not None and (speed < 0 or speed > self.SPEED_SAFETY_THRESHOLD):
                if signal_status:
                    risks.append(f"Train={meta.train_id} 速度异常+信号异常")

        if risks:
            return "; ".join(risks)
        return None

    def _assess_communication_impact(self, metrics: Dict[str, Any]) -> Optional[str]:
        """
        评估通信影响（→ P2）。

        检查点:
        1. PacketLoss > 50%
        2. Latency > 200ms
        3. RenewalInterval > 5000ms（公共指标）
        4. RenewalInterval 来源冲突（source_metrics 中存在差异）

        Returns:
            通信风险描述，无风险则返回 None
        """
        risks = []

        packet_loss = self._get_numeric(metrics, "packet_loss")
        if packet_loss is not None and packet_loss > self.PACKET_LOSS_CRITICAL:
            risks.append(f"PacketLoss={packet_loss:.0%} (>{self.PACKET_LOSS_CRITICAL:.0%})")

        latency = self._get_numeric(metrics, "latency")
        if latency is not None and latency > self.LATENCY_CRITICAL:
            risks.append(f"Latency={latency:.0f}ms (>{self.LATENCY_CRITICAL:.0f}ms)")

        renewal = self._get_numeric(metrics, "renewal_interval")
        if renewal is not None and renewal > self.RENEWAL_INTERVAL_CRITICAL_MAX:
            risks.append(f"RenewalInterval={renewal:.0f}ms (>{self.RENEWAL_INTERVAL_CRITICAL_MAX:.0f}ms)")

        # ---- 来源冲突检测（Multi-Source 异常证据） ----
        source_conflicts = self._detect_source_conflicts(metrics)
        if source_conflicts:
            for conflict in source_conflicts:
                risks.append(conflict)

        if risks:
            return "; ".join(risks)
        return None

    def _assess_performance_impact(self, metrics: Dict[str, Any]) -> Optional[str]:
        """
        评估性能影响（→ P3）。

        Returns:
            性能风险描述，无风险则返回 None
        """
        risks = []

        packet_loss = self._get_numeric(metrics, "packet_loss")
        if packet_loss is not None and packet_loss > self.PACKET_LOSS_MODERATE:
            risks.append(f"PacketLoss={packet_loss:.0%} (>{self.PACKET_LOSS_MODERATE:.0%})")

        latency = self._get_numeric(metrics, "latency")
        if latency is not None and latency > self.LATENCY_MODERATE:
            risks.append(f"Latency={latency:.0f}ms (>{self.LATENCY_MODERATE:.0f}ms)")

        burstiness = self._get_numeric(metrics, "burstiness")
        if burstiness is not None and burstiness > self.BURSTINESS_MODERATE:
            risks.append(f"Burstiness={burstiness:.2f} (>{self.BURSTINESS_MODERATE})")

        if risks:
            return "; ".join(risks)
        return None

    # ================================================================
    # 指标提取辅助
    # ================================================================

    @staticmethod
    def _flatten_metrics(
        metrics_snapshot: Optional[Dict[str, Any]],
        meta: Any,
        incident: Incident,
    ) -> Dict[str, Any]:
        """
        从多种来源提取扁平化的指标字典。

        支持格式:
        - metrics_snapshot["metrics"]: PrometheusMetricSnapshot 格式
        - metrics_snapshot 直接包含指标: RailMetricRecord 格式
        - metadata.extra 中的原始字段
        """
        result: Dict[str, Any] = {}

        if metrics_snapshot:
            # PrometheusMetricSnapshot 格式: {"labels": {...}, "metrics": {...}}
            inner = metrics_snapshot.get("metrics", {})
            if isinstance(inner, dict) and inner:
                result.update(inner)
            else:
                # RailMetricRecord 格式: metrics 直接在顶层
                for key in (
                    "speed", "distance", "packet_loss", "latency",
                    "renewal_interval", "burstiness", "overlap_count",
                    "signal_status", "overlap_status",
                ):
                    if key in metrics_snapshot and metrics_snapshot[key] is not None:
                        result[key] = metrics_snapshot[key]

            # 包含 source_metrics（Multi-Source 冲突数据）
            source_metrics = metrics_snapshot.get("source_metrics", {})
            if source_metrics and isinstance(source_metrics, dict) and source_metrics:
                result["source_metrics"] = source_metrics
                result["has_source_conflicts"] = metrics_snapshot.get("has_source_conflicts", bool(source_metrics))

        # 从 metadata.extra 提取
        if meta and meta.extra:
            for key in (
                "speed", "distance", "packet_loss", "latency",
                "renewal_interval", "burstiness",
            ):
                if key in meta.extra and meta.extra[key] is not None:
                    if key not in result:
                        result[key] = meta.extra[key]

        # 从 raw_payload 提取（最后手段）
        payload = incident.raw_payload
        if payload and isinstance(payload, dict):
            for key in (
                "speed", "packet_loss", "latency", "burstiness",
                "signal_status", "overlap_status",
            ):
                if key in payload and payload[key] is not None and key not in result:
                    result[key] = payload[key]

        return result

    @staticmethod
    def _detect_source_conflicts(metrics: Dict[str, Any]) -> List[str]:
        """
        检测 source_metrics 中的来源间冲突。

        当同一指标在不同来源存在差异值时，生成冲突证据。
        例如:
        - renewal_interval: control_center=0, train=170 → "冲突"
        - 如果 signal_status 在不同来源也不同 → 安全级别冲突

        Returns:
            冲突描述列表，无冲突则返回空列表
        """
        conflicts = []
        source_metrics = metrics.get("source_metrics", {})
        if not source_metrics or not isinstance(source_metrics, dict):
            return conflicts

        for metric_name in ("renewal_interval", "packet_loss", "latency",
                            "burstiness", "signal_status", "overlap_status"):
            # 收集各来源的该指标值
            source_values: Dict[str, Any] = {}
            for src_name, src_data in source_metrics.items():
                if isinstance(src_data, dict) and metric_name in src_data:
                    source_values[src_name] = src_data[metric_name]

            if len(source_values) >= 2:
                unique = set(source_values.values())
                if len(unique) >= 2:
                    # 存在冲突
                    detail = ", ".join(f"{s}={v}" for s, v in sorted(source_values.items()))
                    conflicts.append(
                        f"来源冲突:{metric_name}[{detail}]"
                    )
                    logger.info(
                        f"[SeverityEngine] 检测到来源冲突: metric={metric_name}, "
                        f"details={source_values}"
                    )

        return conflicts

    @staticmethod
    def _get_numeric(metrics: Dict[str, Any], key: str) -> Optional[float]:
        """从指标字典中安全提取数值"""
        value = metrics.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    def _is_metric_abnormal(self, metric_name: str, value: Any) -> bool:
        """判断单个指标是否异常"""
        num = None
        try:
            num = float(value) if value is not None else None
        except (ValueError, TypeError):
            return False

        if num is None:
            return False

        thresholds = {
            "packet_loss": self.PACKET_LOSS_MODERATE,
            "latency": self.LATENCY_MODERATE,
            "burstiness": self.BURSTINESS_MODERATE,
            "renewal_interval": self.RENEWAL_INTERVAL_CRITICAL_MAX,
            "speed": self.SPEED_SAFETY_THRESHOLD,
        }

        threshold = thresholds.get(metric_name)
        if threshold is not None:
            return num > threshold
        return False

    # ================================================================
    # 级别操作
    # ================================================================

    @staticmethod
    def _upgrade(severity: Severity) -> Severity:
        """升级一级"""
        upgrades = {
            Severity.P4: Severity.P3,
            Severity.P3: Severity.P2,
            Severity.P2: Severity.P1,
            Severity.P1: Severity.P1,   # P1 无法再升级
            Severity.UNKNOWN: Severity.P4,
        }
        return upgrades.get(severity, severity)

    @staticmethod
    def compare(a: Severity, b: Severity) -> int:
        """
        比较两个严重级别。

        Returns:
            -1 如果 a < b, 0 如果 a == b, 1 如果 a > b
        """
        order = {
            Severity.P1: 5,
            Severity.P2: 4,
            Severity.P3: 3,
            Severity.P4: 2,
            Severity.UNKNOWN: 1,
        }
        oa, ob = order.get(a, 0), order.get(b, 0)
        if oa > ob:
            return 1
        elif oa < ob:
            return -1
        return 0

    @staticmethod
    def is_critical(severity: Severity) -> bool:
        """是否为紧急级别 (P1/P2)"""
        return severity in (Severity.P1, Severity.P2)
