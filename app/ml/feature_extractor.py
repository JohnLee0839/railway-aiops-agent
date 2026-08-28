"""
FeatureExtractor — 从 RailMetricRecord 提取结构化特征向量

职责:
- 输入: RailMetricRecord（融合后的多源观测数据）
- 输出: FeatureVector（供监督学习模型推理的结构化特征）

设计原则:
- 不依赖任何 ML 框架（TensorFlow/PyTorch/sklearn）
- 纯 Python 数值计算
- 处理缺失值: 默认填充为 0 或 None
- 派生特征从 source_metrics 计算
"""

from typing import Optional
from loguru import logger

from app.models.metrics import RailMetricRecord, FeatureVector


class FeatureExtractor:
    """
    将 RailMetricRecord 转换为 FeatureVector。

    特征提取逻辑:

    1. 基础指标: 直接从 metrics 对象取值
    2. 来源冲突特征:
       - renewal_interval_difference = train.renewal_interval - control_center.renewal_interval
       - renewal_interval_ratio = train.renewal_interval / control_center.renewal_interval
         这两个特征对 DoS/Replay 检测至关重要 — 非零/非1表明观测不一致
    3. 状态特征: 保持原始字符串编码

    使用示例:
        extractor = FeatureExtractor()
        record = RailMetricRecord(...)
        features = extractor.extract(record)
        print(features.renewal_interval_difference)
    """

    # 来源名称约定
    SOURCE_TRAIN = "train"
    SOURCE_CONTROL_CENTER = "control_center"

    def extract(self, record: RailMetricRecord) -> FeatureVector:
        """
        从 RailMetricRecord 提取特征向量。

        Args:
            record: 融合后的监测记录

        Returns:
            FeatureVector 对象
        """
        # ---- 基础指标 ----
        packet_loss = self._safe_float(record.metrics.packet_loss)
        latency = self._safe_float(record.metrics.latency)
        burstiness = self._safe_float(record.metrics.burstiness)

        # ---- 来源冲突特征 ----
        renewal_diff, renewal_ratio = self._compute_renewal_features(record)

        # ---- 信号状态 ----
        signal_status = record.metrics.signal_status
        overlap_status = record.metrics.overlap_status
        overlap_count = record.metrics.overlap_count

        # ---- 运动特征 ----
        speed = self._safe_float(record.metrics.speed)
        distance = self._safe_float(record.metrics.distance)

        # ---- 元数据 ----
        train_id = record.train_id or ""
        signal_id = record.signal_id or ""

        features = FeatureVector(
            packet_loss=packet_loss,
            latency=latency,
            burstiness=burstiness,
            renewal_interval_difference=renewal_diff,
            renewal_interval_ratio=renewal_ratio,
            signal_status=signal_status,
            overlap_status=overlap_status,
            overlap_count=overlap_count,
            speed=speed,
            distance=distance,
            train_id=train_id,
            signal_id=signal_id,
        )

        logger.debug(
            f"[FeatureExtractor] FeatureVector extracted: "
            f"packet_loss={features.packet_loss}, latency={features.latency}, "
            f"renewal_diff={features.renewal_interval_difference}, "
            f"train={features.train_id}, signal={features.signal_id}"
        )

        return features

    def _compute_renewal_features(self, record: RailMetricRecord):
        """
        计算 renewal_interval 的来源冲突特征。

        Returns:
            (difference, ratio) 元组

        - difference = train - control_center
          > 0 表示列车观测到的续期间隔大于控制中心期望值
        - ratio = train / control_center
          当 control_center 为 0 时，ratio 为 None（避免除零）
        """
        source_metrics = record.source_metrics or {}

        cc_value = source_metrics.get(self.SOURCE_CONTROL_CENTER, {}).get("renewal_interval")
        train_value = source_metrics.get(self.SOURCE_TRAIN, {}).get("renewal_interval")

        diff = None
        ratio = None

        if cc_value is not None and train_value is not None:
            cc_f = self._safe_float(cc_value)
            train_f = self._safe_float(train_value)
            if cc_f is not None and train_f is not None:
                diff = round(train_f - cc_f, 4)
                if cc_f != 0:
                    ratio = round(train_f / cc_f, 4)

        return diff, ratio

    @staticmethod
    def _safe_float(value) -> float:
        """安全转换为 float，None 或无法转换时返回 0.0"""
        if value is None:
            return 0.0
        try:
            return float(value)
        except (ValueError, TypeError):
            return 0.0
