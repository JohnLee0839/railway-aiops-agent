"""
STSRS Data Adapter — STSRS 数据集读取、AttackInfo 隐藏、多源观测融合

职责:
1. 读取 STSRS 数据集（支持 CSV/JSON 格式）
2. 删除/隐藏 AttackInfo 字段（模拟真实 AIOps 无法提前知道攻击类型）
3. 按照 Timestamp + TrainID + SignalID 三维进行数据融合
4. 基于来源（control_center / train / signal_device / ...）保留观测差异
5. 输出统一的 RailMetricRecord 列表

设计原则:
- 不关心 STSRS 原始文件中有哪些攻击标签 — 一律删除
- 同一时间、同一列车、同一信号设备的观测合并为一条记录
- 不同来源的同名指标值存在差异时，不做覆盖，保留在 source_metrics 中
  这种差异本身就是异常证据（如 Control Center 认为 RenewalInterval=0，
  但 Train 观测到 RenewalInterval=170，说明可能存在信号篡改/Replay Attack）
- 缺失指标字段为 None，不做填充
"""

from typing import Dict, List, Optional, Any
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from loguru import logger

from app.models.metrics import (
    RailMetricRecord,
    RailMetrics,
    FusionKey,
)


# STSRS 数据集中需要被隐藏/删除的攻击相关字段
_ATTACK_FIELDS_TO_HIDE = frozenset({
    "attack_info",
    "attackinfo",
    "attack_code",
    "attack_type",
    "attack_code_id",
    "attack_category",
    "attack_label",
    "label",
    "anomaly_type",
    "is_attack",
    "is_anomaly",
    "ground_truth",
})


class STSRSAdapter:
    """
    STSRS 数据适配器。

    读取一个或多个 STSRS 数据文件，隐藏攻击标签，按三维键融合。

    使用示例:
        adapter = STSRSAdapter()
        records = adapter.load_and_fuse(["data/file1.csv", "data/file2.csv"])
        for record in records:
            print(record.train_id, record.metrics.packet_loss)
    """

    # ================================================================
    # 字段名映射（支持不同格式的 STSRS 文件）
    # ================================================================
    # 标准字段名 -> 归一化字段名
    FIELD_ALIASES: Dict[str, str] = {
        # 时间戳
        "timestamp": "timestamp", "time": "timestamp", "ts": "timestamp",
        "datetime": "timestamp", "date_time": "timestamp",
        # 列车
        "train_id": "train_id", "trainid": "train_id", "train": "train_id",
        "TrainID": "train_id", "Train_ID": "train_id",
        # 信号设备
        "signal_id": "signal_id", "signalid": "signal_id", "signal": "signal_id",
        "SignalID": "signal_id", "Signal_ID": "signal_id",
        # 指标
        "speed": "speed", "Speed": "speed", "train_speed": "speed",
        "distance": "distance", "Distance": "distance",
        "location": "location", "Location": "location",
        "signal_status": "signal_status", "signalstatus": "signal_status",
        "SignalStatus": "signal_status",
        "overlap_status": "overlap_status", "overlapstatus": "overlap_status",
        "OverlapStatus": "overlap_status",
        "overlap_count": "overlap_count", "overlapcount": "overlap_count",
        "OverlapCount": "overlap_count",
        "packet_loss": "packet_loss", "packetloss": "packet_loss",
        "PacketLoss": "packet_loss",
        "latency": "latency", "Latency": "latency", "delay": "latency",
        "renewal_interval": "renewal_interval", "renewalinterval": "renewal_interval",
        "RenewalInterval": "renewal_interval",
        "burstiness": "burstiness", "Burstiness": "burstiness",
    }

    # 必须存在的关键字段（用于融合键）
    REQUIRED_KEY_FIELDS = ("timestamp", "train_id", "signal_id")

    def __init__(self, strict_mode: bool = False):
        """
        Args:
            strict_mode: True 时，关键字段缺失则丢弃该行；False 则用默认值填充
        """
        self.strict_mode = strict_mode
        self._fusion_map: Dict[FusionKey, List[Dict[str, Any]]] = defaultdict(list)

    # ================================================================
    # 主入口
    # ================================================================

    def load_and_fuse(
        self,
        file_paths: List[str],
    ) -> List[RailMetricRecord]:
        """
        读取多个 STSRS 文件，融合后返回 RailMetricRecord 列表。

        每个文件的来源通过文件名自动识别:
        - "STSRS-Control Center.txt" → source="control_center"
        - "STSRS-Train.txt" → source="train"
        - "STSRS-Signal Device.csv" → source="signal_device"

        Args:
            file_paths: STSRS 数据文件路径列表

        Returns:
            融合后的 RailMetricRecord 列表（按时间排序）
        """
        self._fusion_map.clear()
        all_rows: List[Dict[str, Any]] = []

        for fp in file_paths:
            logger.info(f"[STSRSAdapter] 读取文件: {fp}")
            source_name = self._derive_source(fp)
            rows = self._read_file(fp)
            cleaned = [self._strip_attack_fields(r) for r in rows]
            normalized = [self._normalize_fields(r) for r in cleaned]
            # 标记来源
            for row in normalized:
                row["_source"] = source_name
            valid = self._filter_valid(normalized)
            all_rows.extend(valid)
            logger.info(f"[STSRSAdapter]   -> {len(valid)} 条有效记录 (来源: {source_name})")

        logger.info(f"[STSRSAdapter] 总共 {len(all_rows)} 条记录，开始融合...")

        # 按融合键分组
        for row in all_rows:
            try:
                key = self._make_fusion_key(row)
                self._fusion_map[key].append(row)
            except KeyError as e:
                logger.warning(f"[STSRSAdapter] 跳过无法生成融合键的记录: {e}")

        # 融合每组记录
        records: List[RailMetricRecord] = []
        for key, rows in self._fusion_map.items():
            record = self._fuse_rows(key, rows)
            records.append(record)

        # 按时间排序
        records.sort(key=lambda r: r.timestamp)
        logger.info(f"[STSRSAdapter] 融合完成: {len(records)} 条 RailMetricRecord")

        return records

    def load_single(self, file_path: str) -> List[RailMetricRecord]:
        """读取单个文件（不融合），每条原始行转为一条 RailMetricRecord"""
        source_name = self._derive_source(file_path)
        rows = self._read_file(file_path)
        cleaned = [self._strip_attack_fields(r) for r in rows]
        normalized = [self._normalize_fields(r) for r in cleaned]
        # 标记来源
        for row in normalized:
            row["_source"] = source_name
        valid = self._filter_valid(normalized)

        records: List[RailMetricRecord] = []
        for row in valid:
            metrics = self._build_metrics(row)
            record = RailMetricRecord(
                timestamp=self._parse_timestamp(row["timestamp"]),
                train_id=str(row["train_id"]),
                signal_id=str(row["signal_id"]),
                metrics=metrics,
                source_files=[file_path],
            )
            records.append(record)

        logger.info(f"[STSRSAdapter] 单文件读取: {len(records)} 条 from {file_path} (来源: {source_name})")
        return records

    # ================================================================
    # 文件读取
    # ================================================================

    def _read_file(self, file_path: str) -> List[Dict[str, Any]]:
        """根据扩展名读取 CSV 或 JSON 文件"""
        path = Path(file_path)
        if not path.exists():
            logger.error(f"[STSRSAdapter] 文件不存在: {file_path}")
            return []

        suffix = path.suffix.lower()

        if suffix in (".csv", ".txt"):
            return self._read_csv(file_path)
        elif suffix in (".json", ".jsonl"):
            return self._read_json(file_path)
        else:
            logger.warning(f"[STSRSAdapter] 未知文件类型 {suffix}，尝试 CSV 方式读取")
            return self._read_csv(file_path)

    @staticmethod
    def _read_csv(file_path: str) -> List[Dict[str, Any]]:
        """读取 CSV 文件"""
        import csv
        rows = []
        try:
            with open(file_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(dict(row))
        except UnicodeDecodeError:
            with open(file_path, "r", encoding="gbk") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(dict(row))
        return rows

    @staticmethod
    def _read_json(file_path: str) -> List[Dict[str, Any]]:
        """读取 JSON / JSONL 文件"""
        import json
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read().strip()

        # JSONL: 每行一个 JSON 对象
        if content.startswith("{"):
            rows = []
            for line in content.split("\n"):
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            if rows:
                return rows

        # JSON: 整体解析
        data = json.loads(content)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            # 如果是 {"data": [...]} 格式
            for key in ("data", "records", "rows", "items"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            return [data]
        return []

    # ================================================================
    # 数据清洗
    # ================================================================

    def _strip_attack_fields(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """
        删除/隐藏所有攻击相关字段。

        模拟真实场景：系统只能看到监测指标，不知道是否有攻击、是什么攻击。
        """
        keys_to_remove = []
        for key in row:
            key_lower = key.lower().replace(" ", "_").replace("-", "_")
            if key_lower in _ATTACK_FIELDS_TO_HIDE:
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del row[key]

        return row

    def _normalize_fields(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """将不同的字段名统一为标准名称"""
        normalized: Dict[str, Any] = {}
        for key, value in row.items():
            key_lower = key.lower().replace(" ", "_").replace("-", "_")
            if key_lower in self.FIELD_ALIASES:
                std_key = self.FIELD_ALIASES[key_lower]
                # 如果已存在（从多个别名映射到同一个标准名），不覆盖
                if std_key not in normalized:
                    normalized[std_key] = value
            else:
                normalized[key_lower] = value
        return normalized

    def _filter_valid(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """过滤无效行"""
        valid = []
        for row in rows:
            has_keys = all(row.get(k) for k in self.REQUIRED_KEY_FIELDS)
            if has_keys:
                valid.append(row)
            elif not self.strict_mode:
                # 非严格模式：尝试用默认值补全
                if "timestamp" not in row or not row.get("timestamp"):
                    row["timestamp"] = datetime.utcnow().isoformat()
                if "train_id" not in row or not row.get("train_id"):
                    row["train_id"] = "UNKNOWN"
                if "signal_id" not in row or not row.get("signal_id"):
                    row["signal_id"] = "UNKNOWN"
                valid.append(row)
            else:
                logger.debug(f"[STSRSAdapter] 丢弃无效行（缺少关键字段）: {row}")
        return valid

    # ================================================================
    # 融合逻辑（Multi-Source Observation Fusion）
    # ================================================================

    # RailMetrics 中的所有指标字段名
    _METRIC_FIELD_NAMES = frozenset({
        "speed", "distance", "location",
        "signal_status", "overlap_status", "overlap_count",
        "packet_loss", "latency", "renewal_interval", "burstiness",
    })

    # 非数值型指标字段（保持字符串原值）
    _STRING_METRIC_FIELDS = frozenset({"location", "signal_status", "overlap_status"})

    @staticmethod
    def _derive_source(file_path: str) -> str:
        """
        从文件名推导数据来源标识。

        规则:
        1. 取文件名（不含扩展名）
        2. 去掉 "STSRS-" 或 "stsrs-" 前缀
        3. 转换为小写，空格和连字符替换为下划线

        示例:
        - "STSRS-Control Center.txt" → "control_center"
        - "STSRS-Train.txt" → "train"
        - "STSRS-Signal Device.csv" → "signal_device"
        - "data/STSRS-dispatcher.json" → "dispatcher"
        """
        stem = Path(file_path).stem
        # 去掉 STSRS- 前缀
        if stem.lower().startswith("stsrs-"):
            stem = stem[6:]
        elif stem.lower().startswith("stsrs_"):
            stem = stem[6:]
        # 归一化: 小写 + 空格/连字符 → 下划线
        source = stem.lower().replace(" ", "_").replace("-", "_")
        return source

    def _make_fusion_key(self, row: Dict[str, Any]) -> FusionKey:
        """从行数据生成融合键"""
        return FusionKey(
            timestamp=self._parse_timestamp(row["timestamp"]),
            train_id=str(row["train_id"]),
            signal_id=str(row["signal_id"]),
        )

    @staticmethod
    def _parse_timestamp(ts: Any) -> datetime:
        """解析时间戳（支持多种格式）"""
        if isinstance(ts, datetime):
            return ts
        if isinstance(ts, (int, float)):
            # Unix 时间戳
            if ts > 1e12:
                return datetime.utcfromtimestamp(ts / 1000.0)  # 毫秒
            return datetime.utcfromtimestamp(ts)  # 秒

        ts_str = str(ts).strip()
        # 尝试多种格式
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y/%m/%d %H:%M:%S",
            "%d/%m/%Y %H:%M:%S",
            "%d-%b-%Y %H:%M:%S",     # 14-Aug-2025 09:08:15
            "%d-%b-%Y %H:%M:%S.%f",  # 14-Aug-2025 09:08:15.123
        ]
        for fmt in formats:
            try:
                return datetime.strptime(ts_str, fmt)
            except ValueError:
                continue
        # 最后尝试 ISO 格式
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return datetime.utcnow()

    def _fuse_rows(
        self,
        key: FusionKey,
        rows: List[Dict[str, Any]],
    ) -> RailMetricRecord:
        """
        融合同一 FusionKey 下多来源的观测数据（Multi-Source Observation Fusion）。

        策略:
        1. 按来源收集所有指标值
        2. 对于每个指标:
           a. 如果所有来源的非空值一致 → 放入 metrics（公共指标）
           b. 如果不同来源的值存在差异 → 放入 source_metrics（来源特定指标）
              （这种差异本身就是异常证据）
        3. 收集所有来源文件名

        Args:
            key: 融合键 (timestamp, train_id, signal_id)
            rows: 同键下的所有行数据（可能来自不同来源文件）

        Returns:
            融合后的 RailMetricRecord
        """
        # ---- Step 1: 按来源收集所有指标值 ----
        # 结构: {metric_name: {source_name: value, ...}, ...}
        metric_by_source: Dict[str, Dict[str, Any]] = defaultdict(dict)
        source_files: set[str] = set()

        for row in rows:
            source = row.get("_source", "unknown")
            source_files.add(source)

            for metric_name in self._METRIC_FIELD_NAMES:
                if metric_name in row and row[metric_name] is not None:
                    parsed = self._parse_metric_value(metric_name, row[metric_name])
                    if parsed is not None:
                        # 同名指标同一来源: 后出现的值覆盖先出现的
                        metric_by_source[metric_name][source] = parsed

        # ---- Step 2: 分离公共指标与来源特定指标 ----
        common_metrics: Dict[str, Any] = {}
        source_metrics: Dict[str, Dict[str, Any]] = defaultdict(dict)

        for metric_name in self._METRIC_FIELD_NAMES:
            source_values = metric_by_source.get(metric_name, {})
            if not source_values:
                # 该指标无数据
                continue

            unique_values = set(source_values.values())

            if len(unique_values) == 1:
                # 所有来源一致 → 公共指标
                common_metrics[metric_name] = next(iter(unique_values))
            else:
                # 来源间存在差异 → 来源特定指标（异常证据！）
                for src, val in source_values.items():
                    source_metrics[src][metric_name] = val
                logger.info(
                    f"[STSRSAdapter] 检测到来源差异: metric={metric_name}, "
                    f"values={dict(source_values)}, "
                    f"fusion_key={key.to_string()}"
                )

        # ---- Step 3: 构建输出 ----
        metrics = RailMetrics(**common_metrics) if common_metrics else RailMetrics()

        return RailMetricRecord(
            timestamp=key.timestamp,
            train_id=key.train_id,
            signal_id=key.signal_id,
            metrics=metrics,
            source_metrics=dict(source_metrics) if source_metrics else {},
            source_files=sorted(source_files),
        )

    @staticmethod
    def _parse_numeric(value: Any) -> Optional[float]:
        """尝试将值解析为浮点数，失败则返回 None"""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip())
        except (ValueError, TypeError):
            return None

    def _parse_metric_value(self, metric_name: str, value: Any) -> Any:
        """
        根据指标类型解析值。

        - 字符串类型指标 (location, signal_status, overlap_status): 保持原值
        - 数值类型指标: 解析为 float，失败则返回原值
        """
        if metric_name in self._STRING_METRIC_FIELDS:
            return str(value).strip() if value is not None else None
        parsed = self._parse_numeric(value)
        return parsed if parsed is not None else str(value).strip()

    @staticmethod
    def _build_metrics(row: Dict[str, Any]) -> RailMetrics:
        """从行数据构建 RailMetrics"""
        metric_fields = set(RailMetrics.model_fields.keys())
        data = {}
        for key, value in row.items():
            if key in metric_fields and value is not None:
                try:
                    data[key] = float(value) if key not in (
                        "location", "signal_status", "overlap_status"
                    ) else str(value)
                except (ValueError, TypeError):
                    data[key] = str(value)
        return RailMetrics(**data) if data else RailMetrics()

    @property
    def stats(self) -> Dict[str, Any]:
        """返回当前适配器统计信息"""
        return {
            "fusion_groups": len(self._fusion_map),
            "total_records": sum(len(v) for v in self._fusion_map.values()),
        }
