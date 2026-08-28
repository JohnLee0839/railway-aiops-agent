"""
AttackDetector — 监督学习攻击检测模型接口

设计原则:
- 只定义推理接口 (predict)，不包含训练逻辑
- Mock 实现保证系统可运行，待真实模型就绪后替换
- 可插拔: 替换底层实现（XGBoost / LightGBM / Transformer）不影响 Agent 架构

职责分工:
- AttackDetector: 回答 "What happened?" (分类: DoS / Jamming / Replay / ...)
- TriageAgent:    回答 "Why? Impact? How to fix?" (解释 + RAG + 诊断报告)

使用示例:
    detector = MockAttackDetector()
    prediction = detector.predict(metric_record)
    print(prediction.attack_type)  # "UNKNOWN"
"""

from time import perf_counter
from typing import Optional
from loguru import logger

from app.models.metrics import (
    RailMetricRecord,
    AttackPrediction,
)


# ============================================================
# 抽象接口
# ============================================================

class AttackDetector:
    """
    攻击检测模型抽象接口（Inference-only）。

    子类必须实现 predict() 方法。

    未来实现示例:
    - XGBoostAttackDetector: 加载 .json/.pkl 模型文件
    - LightGBMAttackDetector: 加载 LightGBM booster
    - TransformerAttackDetector: ONNX 推理
    - EnsembleAttackDetector: 多模型投票
    """

    @property
    def model_version(self) -> str:
        """模型版本标识"""
        return "abstract"

    def predict(self, metrics: RailMetricRecord) -> AttackPrediction:
        """
        根据铁路监测指标预测攻击类型。

        Args:
            metrics: 融合后的 RailMetricRecord（不含 AttackInfo）

        Returns:
            AttackPrediction（含 attack_type, confidence, probabilities）
        """
        raise NotImplementedError(
            "AttackDetector.predict() must be implemented by subclass"
        )


class AttackDetectorError(Exception):
    """Base class for detector errors that are safe to classify for audit."""

    reason_code = "detector_error"


class AttackDetectorLoadError(AttackDetectorError):
    """The detector cannot load its model artifact or runtime dependency."""

    reason_code = "model_load_failed"


class AttackDetectorInputError(AttackDetectorError):
    """The input record does not satisfy the detector contract."""

    reason_code = "invalid_input"


class AttackDetectorInferenceError(AttackDetectorError):
    """The loaded detector produced an invalid or failed inference result."""

    reason_code = "inference_failed"


class FallbackAttackDetector(AttackDetector):
    """Run a primary detector and fall back when inference is unavailable."""

    def __init__(self, primary: AttackDetector, fallback: AttackDetector):
        self.primary = primary
        self.fallback = fallback

    @property
    def model_version(self) -> str:
        return f"{self.primary.model_version}|fallback={self.fallback.model_version}"

    def predict(self, metrics: RailMetricRecord) -> AttackPrediction:
        start = perf_counter()
        try:
            prediction = self.primary.predict(metrics)
            prediction.fallback_used = False
            prediction.fallback_reason = None
            return prediction
        except AttackDetectorLoadError as e:
            reason_code = getattr(e, "reason_code", "model_load_failed")
            logger.warning(
                "[FallbackAttackDetector] primary detector failed; "
                f"using fallback {type(self.fallback).__name__}: "
                f"reason={reason_code}, error={e}"
            )
            prediction = self.fallback.predict(metrics)
            prediction.detector_backend = self._backend_name(self.fallback)
            prediction.fallback_used = True
            prediction.fallback_reason = reason_code
            prediction.inference_ms = round((perf_counter() - start) * 1000.0, 3)
            feature_vector = prediction.feature_vector or {}
            feature_vector.update(
                {
                    "primary_detector": type(self.primary).__name__,
                    "primary_model_version": self.primary.model_version,
                    "primary_error_reason": reason_code,
                    "primary_error_message": str(e),
                    "fallback_detector": type(self.fallback).__name__,
                }
            )
            prediction.feature_vector = feature_vector
            return prediction
        except AttackDetectorInputError:
            logger.warning("[FallbackAttackDetector] invalid input; fallback disabled")
            raise
        except AttackDetectorInferenceError:
            logger.warning("[FallbackAttackDetector] inference contract error; fallback disabled")
            raise

    @staticmethod
    def _backend_name(detector: AttackDetector) -> str:
        if isinstance(detector, MockAttackDetector):
            return "mock"
        if isinstance(detector, RuleBasedAttackDetector):
            return "rule"
        return type(detector).__name__.replace("AttackDetector", "").lower()


# ============================================================
# Mock 实现（开发/测试用）
# ============================================================

class MockAttackDetector(AttackDetector):
    """
    Mock 攻击检测器 — 始终返回 UNKNOWN。

    目的:
    1. 保证系统在模型就绪前可运行
    2. 验证 AttackDetector → TriageAgent 集成链路
    3. 为测试提供确定性输出

    替换为真实模型时:
    - 只需实现 AttackDetector 子类
    - 在 IncidentRouter 中替换 MockAttackDetector 实例
    - 不影响 TriageAgent / RunbookAgent / ActionOrchestrator
    """

    @property
    def model_version(self) -> str:
        return "mock-v0.1.0"

    def predict(self, metrics: RailMetricRecord) -> AttackPrediction:
        """
        Mock 预测: 始终返回 UNKNOWN（无攻击检测能力）。

        真实实现将基于 FeatureVector 执行分类推理。
        """
        logger.info(
            f"[MockAttackDetector] Mock predict: "
            f"train={metrics.train_id}, signal={metrics.signal_id}, "
            f"result=UNKNOWN (mock)"
        )
        return AttackPrediction(
            attack_type="UNKNOWN",
            confidence=0.0,
            probabilities={},
            model_version=self.model_version,
            detector_backend="mock",
        )


# ============================================================
# 基于规则的轻量检测器（Fallback / 过渡方案）
# ============================================================

class RuleBasedAttackDetector(AttackDetector):
    """
    基于规则的轻量攻击检测器。

    在监督学习模型就绪前的过渡方案。
    基于阈值规则判断攻击类型，置信度较低（上限 0.6）。

    规则:
    - PacketLoss > 0.5 + Latency > 200ms + Burstiness > 0.5 → DoS (conf=0.5)
    - PacketLoss > 0.5 + SignalStatus 异常 → Jamming (conf=0.45)
    - RenewalInterval 来源冲突 → Replay (conf=0.4)
    - 默认 → UNKNOWN (conf=0.0)
    """

    # 阈值
    PACKET_LOSS_DOS = 0.5
    LATENCY_DOS = 200.0
    BURSTINESS_DOS = 0.5

    def __init__(self, confidence_cap: float = 0.6):
        """
        Args:
            confidence_cap: 置信度上限（规则方法无法超过此值）
        """
        self.confidence_cap = confidence_cap

    @property
    def model_version(self) -> str:
        return "rule-based-v0.1.0"

    def predict(self, metrics: RailMetricRecord) -> AttackPrediction:
        """
        基于阈值规则进行攻击检测。

        注意: 置信度被限制在 confidence_cap 以下，
        因为基于规则的方法不如监督学习可靠。
        """
        m = metrics.metrics  # RailMetrics
        source = metrics.source_metrics or {}

        packet_loss = self._safe_float(m.packet_loss)
        latency = self._safe_float(m.latency)
        burstiness = self._safe_float(m.burstiness)
        signal_status = (m.signal_status or "").upper()

        # —— Rule 1: DoS 检测 ——
        if (packet_loss > self.PACKET_LOSS_DOS
                and latency > self.LATENCY_DOS
                and burstiness > self.BURSTINESS_DOS):
            return self._make_prediction(
                "DoS", 0.50, {"DoS": 0.50, "Jamming": 0.25, "Replay": 0.10, "Unknown": 0.15}
            )

        # —— Rule 2: Jamming 检测 ——
        if packet_loss > self.PACKET_LOSS_DOS and signal_status in ("RED", "DANGER", "OFFLINE"):
            return self._make_prediction(
                "Jamming", 0.45, {"Jamming": 0.45, "DoS": 0.25, "Unknown": 0.30}
            )

        # —— Rule 3: Replay 检测（来源冲突） ——
        cc_renewal = source.get("control_center", {}).get("renewal_interval")
        train_renewal = source.get("train", {}).get("renewal_interval")
        if cc_renewal is not None and train_renewal is not None:
            try:
                if abs(float(train_renewal) - float(cc_renewal)) > 10:
                    return self._make_prediction(
                        "Replay", 0.40, {"Replay": 0.40, "DoS": 0.20, "Unknown": 0.40}
                    )
            except (ValueError, TypeError):
                pass

        # —— Default: UNKNOWN ——
        return AttackPrediction(
            attack_type="UNKNOWN",
            confidence=0.0,
            probabilities={},
            model_version=self.model_version,
            detector_backend="rule",
        )

    def _make_prediction(
        self, attack_type: str, confidence: float, probabilities: dict
    ) -> AttackPrediction:
        """构建预测结果，限制置信度上限"""
        capped_conf = min(confidence, self.confidence_cap)
        return AttackPrediction(
            attack_type=attack_type,
            confidence=capped_conf,
            probabilities=probabilities,
            model_version=self.model_version,
            detector_backend="rule",
        )

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        if value is None:
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default


def create_attack_detector() -> AttackDetector:
    """Create the configured attack detector used by the incident pipeline."""
    from pathlib import Path

    from app.config import config

    backend = config.ml_attack_detector_backend.lower().strip()
    fallback_name = config.zl_detector_fallback.lower().strip()

    if backend == "mock":
        return MockAttackDetector()
    if backend == "rule":
        return RuleBasedAttackDetector()
    if backend != "zl":
        logger.warning(
            f"Unknown ml_attack_detector_backend={config.ml_attack_detector_backend!r}; "
            "falling back to MockAttackDetector"
        )
        return MockAttackDetector()

    from app.ml.zl_attack_detector import ZLAttackDetector

    project_root = Path(__file__).resolve().parents[2]
    zl_model_root = Path(config.zl_model_root)
    if not zl_model_root.is_absolute():
        zl_model_root = project_root / zl_model_root

    primary = ZLAttackDetector(
        project_root=zl_model_root,
        model_version=config.zl_model_version,
        model_path=config.zl_model_path or None,
        manifest_path=config.zl_model_manifest_path or None,
        confidence_threshold=config.zl_confidence_threshold,
    )

    if fallback_name == "none":
        return primary
    if fallback_name == "mock":
        return FallbackAttackDetector(primary, MockAttackDetector())
    if fallback_name == "rule":
        return FallbackAttackDetector(primary, RuleBasedAttackDetector())

    logger.warning(
        f"Unknown zl_detector_fallback={config.zl_detector_fallback!r}; "
        "using rule fallback"
    )
    return FallbackAttackDetector(primary, RuleBasedAttackDetector())
