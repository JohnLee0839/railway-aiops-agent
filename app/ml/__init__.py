"""
ML module — Supervised learning attack detection interface.

Design principles:
- Provides an inference-only interface (no training logic)
- Mock implementation for development/testing
- Pluggable: swap MockAttackDetector with real model without changing agent architecture
- FeatureExtractor bridges RailMetricRecord -> structured feature vector
"""

from app.ml.attack_detector import (
    AttackDetector,
    FallbackAttackDetector,
    MockAttackDetector,
    RuleBasedAttackDetector,
    create_attack_detector,
)
from app.ml.feature_extractor import FeatureExtractor
from app.ml.zl_attack_detector import ZLAttackDetector, ZLFeatureAdapter

__all__ = [
    "AttackDetector",
    "FallbackAttackDetector",
    "MockAttackDetector",
    "RuleBasedAttackDetector",
    "create_attack_detector",
    "FeatureExtractor",
    "ZLAttackDetector",
    "ZLFeatureAdapter",
]
