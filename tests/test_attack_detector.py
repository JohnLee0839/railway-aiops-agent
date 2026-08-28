"""
Test supervised learning attack detection interface.

Verifies:
1. FeatureExtractor produces correct FeatureVector from RailMetricRecord
2. renewal_interval_difference computed correctly
3. MockAttackDetector returns UNKNOWN
4. RuleBasedAttackDetector detects DoS/Replay patterns
5. AttackPrediction attaches to Incident
6. TriageAgent accepts AttackPrediction input
7. Full pipeline: RailMetricRecord -> FeatureExtractor -> AttackDetector -> Incident
"""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.metrics import (
    RailMetricRecord,
    RailMetrics,
    FeatureVector,
    AttackPrediction,
)


# ================================================================
# Test: FeatureExtractor
# ================================================================

def test_feature_extractor_basic():
    """Verify: FeatureExtractor extracts features from RailMetricRecord"""
    from app.ml.feature_extractor import FeatureExtractor

    extractor = FeatureExtractor()

    record = RailMetricRecord(
        timestamp=datetime.utcnow(),
        train_id="7Y36",
        signal_id="YT919",
        metrics=RailMetrics(
            speed=40.96,
            distance=14.75,
            packet_loss=95.23,
            latency=354.46,
            burstiness=4.80,
            signal_status="Green",
            overlap_status="No",
            overlap_count=0,
        ),
        source_metrics={
            "control_center": {"renewal_interval": 0.0},
            "train": {"renewal_interval": 170.0},
        },
        source_files=["control_center", "train"],
    )

    features = extractor.extract(record)

    assert isinstance(features, FeatureVector), f"Expected FeatureVector, got {type(features)}"
    assert features.packet_loss == 95.23
    assert features.latency == 354.46
    assert features.burstiness == 4.80
    assert features.train_id == "7Y36"
    assert features.signal_id == "YT919"
    print(f"[PASS] FeatureExtractor basic extraction: packet_loss={features.packet_loss}, latency={features.latency}")


def test_renewal_interval_difference():
    """Verify: renewal_interval_difference = train - control_center"""
    from app.ml.feature_extractor import FeatureExtractor

    extractor = FeatureExtractor()

    record = RailMetricRecord(
        timestamp=datetime.utcnow(),
        train_id="7Y36",
        signal_id="YT919",
        metrics=RailMetrics(packet_loss=95.0, latency=350.0, burstiness=4.8),
        source_metrics={
            "control_center": {"renewal_interval": 0.0},
            "train": {"renewal_interval": 170.0},
        },
    )

    features = extractor.extract(record)

    assert features.renewal_interval_difference == 170.0, (
        f"Expected diff=170.0, got {features.renewal_interval_difference}"
    )
    # train(170) / control_center(0) -> ratio is None (division by zero)
    assert features.renewal_interval_ratio is None, (
        f"Expected ratio=None (div by zero), got {features.renewal_interval_ratio}"
    )
    print(f"[PASS] renewal_interval_difference={features.renewal_interval_difference} (170.0 expected)")


def test_renewal_interval_ratio():
    """Verify: renewal_interval_ratio when both values are non-zero"""
    from app.ml.feature_extractor import FeatureExtractor

    extractor = FeatureExtractor()

    record = RailMetricRecord(
        timestamp=datetime.utcnow(),
        train_id="T001",
        signal_id="S001",
        metrics=RailMetrics(packet_loss=50.0, latency=200.0, burstiness=0.3),
        source_metrics={
            "control_center": {"renewal_interval": 100.0},
            "train": {"renewal_interval": 200.0},
        },
    )

    features = extractor.extract(record)

    assert features.renewal_interval_difference == 100.0
    assert features.renewal_interval_ratio == 2.0, (
        f"Expected ratio=2.0, got {features.renewal_interval_ratio}"
    )
    print(f"[PASS] renewal_interval_ratio={features.renewal_interval_ratio} (2.0 expected)")


# ================================================================
# Test: AttackPrediction model
# ================================================================

def test_attack_prediction_model():
    """Verify: AttackPrediction model works correctly"""
    pred = AttackPrediction(
        attack_type="DoS",
        confidence=0.95,
        probabilities={"DoS": 0.95, "Jamming": 0.03, "Replay": 0.02},
        model_version="xgboost-v1.0",
    )

    assert pred.attack_type == "DoS"
    assert pred.confidence == 0.95
    assert "DoS" in pred.probabilities
    assert pred.model_version == "xgboost-v1.0"

    # Serialization
    d = pred.model_dump()
    assert d["attack_type"] == "DoS"
    assert d["confidence"] == 0.95

    print(f"[PASS] AttackPrediction model: {pred.attack_type}, conf={pred.confidence}")


# ================================================================
# Test: MockAttackDetector
# ================================================================

def test_mock_attack_detector():
    """Verify: MockAttackDetector always returns UNKNOWN"""
    from app.ml.attack_detector import MockAttackDetector

    detector = MockAttackDetector()

    record = RailMetricRecord(
        timestamp=datetime.utcnow(),
        train_id="7Y36",
        signal_id="YT919",
        metrics=RailMetrics(packet_loss=95.0, latency=354.0, burstiness=4.8),
        source_metrics={
            "control_center": {"renewal_interval": 0.0},
            "train": {"renewal_interval": 170.0},
        },
    )

    prediction = detector.predict(record)

    assert isinstance(prediction, AttackPrediction)
    assert prediction.attack_type == "UNKNOWN"
    assert prediction.confidence == 0.0
    assert prediction.model_version == "mock-v0.1.0"
    print(f"[PASS] MockAttackDetector: attack_type={prediction.attack_type}")


# ================================================================
# Test: RuleBasedAttackDetector
# ================================================================

def test_rule_based_detector_dos():
    """Verify: RuleBasedAttackDetector detects DoS pattern"""
    from app.ml.attack_detector import RuleBasedAttackDetector

    detector = RuleBasedAttackDetector(confidence_cap=0.6)

    # Typical DoS pattern: high packet_loss + high latency + high burstiness
    record = RailMetricRecord(
        timestamp=datetime.utcnow(),
        train_id="7Y36",
        signal_id="YT919",
        metrics=RailMetrics(
            packet_loss=95.23,
            latency=354.46,
            burstiness=4.80,
            signal_status="Green",
        ),
    )

    prediction = detector.predict(record)

    assert prediction.attack_type == "DoS", (
        f"Expected DoS, got {prediction.attack_type}"
    )
    assert prediction.confidence <= 0.6, f"Confidence {prediction.confidence} exceeds cap 0.6"
    print(f"[PASS] RuleBasedAttackDetector detects DoS: conf={prediction.confidence:.0%}")


def test_rule_based_detector_replay():
    """Verify: RuleBasedAttackDetector detects Replay via source conflict"""
    from app.ml.attack_detector import RuleBasedAttackDetector

    detector = RuleBasedAttackDetector(confidence_cap=0.6)

    # Replay pattern: renewal_interval source conflict
    record = RailMetricRecord(
        timestamp=datetime.utcnow(),
        train_id="7Y36",
        signal_id="YT919",
        metrics=RailMetrics(
            packet_loss=50.0,
            latency=100.0,
            burstiness=0.2,
        ),
        source_metrics={
            "control_center": {"renewal_interval": 0.0},
            "train": {"renewal_interval": 170.0},
        },
    )

    prediction = detector.predict(record)

    assert prediction.attack_type == "Replay", (
        f"Expected Replay, got {prediction.attack_type}"
    )
    print(f"[PASS] RuleBasedAttackDetector detects Replay: diff=170, conf={prediction.confidence:.0%}")


def test_rule_based_detector_unknown():
    """Verify: RuleBasedAttackDetector returns UNKNOWN for normal metrics"""
    from app.ml.attack_detector import RuleBasedAttackDetector

    detector = RuleBasedAttackDetector()

    record = RailMetricRecord(
        timestamp=datetime.utcnow(),
        train_id="T001",
        signal_id="S001",
        metrics=RailMetrics(packet_loss=0.05, latency=50.0, burstiness=0.1),
    )

    prediction = detector.predict(record)

    assert prediction.attack_type == "UNKNOWN"
    assert prediction.confidence == 0.0
    print(f"[PASS] RuleBasedAttackDetector returns UNKNOWN for normal metrics")


# ================================================================
# Test: Incident with AttackPrediction
# ================================================================

def test_incident_with_attack_prediction():
    """Verify: Incident can carry AttackPrediction"""
    from app.models.incident import Incident, IncidentSource, IncidentMetadata

    prediction = AttackPrediction(
        attack_type="DoS",
        confidence=0.95,
        probabilities={"DoS": 0.95, "Jamming": 0.03, "Replay": 0.02},
        model_version="rule-based-v0.1.0",
    )

    incident = Incident(
        source=IncidentSource.STSRS,
        metadata=IncidentMetadata(train_id="7Y36", signal_id="YT919"),
        metrics_snapshot={
            "metrics": {"packet_loss": 95.0, "latency": 354.0},
            "source_metrics": {"control_center": {"renewal_interval": 0}, "train": {"renewal_interval": 170}},
        },
        attack_prediction=prediction.model_dump(),
        description="DoS attack detection test",
    )

    assert incident.attack_prediction is not None
    assert incident.attack_prediction["attack_type"] == "DoS"
    assert incident.attack_prediction["confidence"] == 0.95
    print(f"[PASS] Incident carries AttackPrediction: {incident.attack_prediction['attack_type']}")


# ================================================================
# Test: IncidentRouter._build_metric_record
# ================================================================

def test_build_metric_record_from_incident():
    """Verify: RailMetricRecord can be reconstructed from an Incident's metrics_snapshot

    Uses the same algorithm as IncidentRouter._build_metric_record() but
    avoids importing IncidentRouter (which triggers the Milvus chain).
    """
    from datetime import datetime as dt
    from app.models.incident import Incident, IncidentSource, IncidentMetadata
    from app.models.metrics import RailMetrics

    incident = Incident(
        source=IncidentSource.STSRS,
        metadata=IncidentMetadata(train_id="7Y36", signal_id="YT919"),
        metrics_snapshot={
            "record_id": "RMR-TEST",
            "train_id": "7Y36",
            "signal_id": "YT919",
            "timestamp": "2025-08-14T09:08:15",
            "metrics": {
                "speed": 40.96,
                "packet_loss": 95.23,
                "latency": 354.46,
                "burstiness": 4.80,
                "signal_status": "Green",
                "overlap_status": "No",
                "overlap_count": 0,
            },
            "source_metrics": {
                "control_center": {"renewal_interval": 0.0},
                "train": {"renewal_interval": 170.0},
            },
            "source_files": ["control_center", "train"],
        },
    )

    # Reconstruct RailMetricRecord (same logic as IncidentRouter._build_metric_record)
    snapshot = incident.metrics_snapshot or {}

    metrics_data = {}
    raw_metrics = snapshot.get("metrics", {})
    if isinstance(raw_metrics, dict):
        for field_name in RailMetrics.model_fields:
            if field_name in raw_metrics and raw_metrics[field_name] is not None:
                metrics_data[field_name] = raw_metrics[field_name]

    metrics = RailMetrics(**metrics_data) if metrics_data else RailMetrics()
    source_metrics = snapshot.get("source_metrics", {})
    train_id = snapshot.get("train_id") or (incident.metadata.train_id or "")
    signal_id = snapshot.get("signal_id") or (incident.metadata.signal_id or "")

    ts_raw = snapshot.get("timestamp", "")
    timestamp = dt.fromisoformat(str(ts_raw).replace("Z", "+00:00"))

    record = RailMetricRecord(
        record_id=snapshot.get("record_id", ""),
        timestamp=timestamp,
        train_id=str(train_id),
        signal_id=str(signal_id),
        metrics=metrics,
        source_metrics=source_metrics if isinstance(source_metrics, dict) else {},
        source_files=snapshot.get("source_files", []) if isinstance(snapshot.get("source_files"), list) else [],
    )

    assert record.train_id == "7Y36"
    assert record.signal_id == "YT919"
    assert record.metrics.packet_loss == 95.23
    assert record.source_metrics["control_center"]["renewal_interval"] == 0.0
    assert record.source_metrics["train"]["renewal_interval"] == 170.0
    print(f"[PASS] _build_metric_record reconstruction: train={record.train_id}, signal={record.signal_id}")


# ================================================================
# Test: Full pipeline (FeatureExtractor -> AttackDetector -> Incident)
# ================================================================

def test_full_ml_pipeline():
    """Verify: Complete ML pipeline produces correct result"""
    from app.ml.feature_extractor import FeatureExtractor
    from app.ml.attack_detector import RuleBasedAttackDetector

    extractor = FeatureExtractor()
    detector = RuleBasedAttackDetector(confidence_cap=0.6)

    # Input: RailMetricRecord with DoS pattern
    record = RailMetricRecord(
        timestamp=datetime.utcnow(),
        train_id="7Y36",
        signal_id="YT919",
        metrics=RailMetrics(
            speed=40.96,
            distance=14.75,
            packet_loss=95.23,
            latency=354.46,
            burstiness=4.80,
            signal_status="Green",
            overlap_status="No",
            overlap_count=0,
        ),
        source_metrics={
            "control_center": {"renewal_interval": 0.0},
            "train": {"renewal_interval": 170.0},
        },
        source_files=["control_center", "train"],
    )

    # Step 1: Feature extraction
    features = extractor.extract(record)
    assert features.packet_loss == 95.23
    assert features.renewal_interval_difference == 170.0

    # Step 2: Attack detection
    prediction = detector.predict(record)
    assert prediction.attack_type == "DoS"  # DoS matches more rules than Replay
    assert prediction.confidence > 0

    # Step 3: Attach to Incident
    from app.models.incident import Incident, IncidentSource, IncidentMetadata
    incident = Incident(
        source=IncidentSource.STSRS,
        metadata=IncidentMetadata(train_id=record.train_id, signal_id=record.signal_id),
        metrics_snapshot={
            "metrics": {"packet_loss": features.packet_loss, "latency": features.latency},
            "source_metrics": record.source_metrics,
        },
        attack_prediction=prediction.model_dump(),
    )

    assert incident.attack_prediction["attack_type"] == "DoS"
    assert incident.attack_prediction["confidence"] > 0

    print(f"[PASS] Full ML pipeline: {record.train_id}/{record.signal_id} -> "
          f"features(feats={features.renewal_interval_difference}) -> "
          f"prediction({prediction.attack_type}:{prediction.confidence:.0%}) -> "
          f"incident({incident.incident_id})")


# ================================================================
# Run all tests
# ================================================================

if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("=" * 70)
    print("AttackDetector & FeatureExtractor Tests")
    print("=" * 70)

    tests = [
        test_feature_extractor_basic,
        test_renewal_interval_difference,
        test_renewal_interval_ratio,
        test_attack_prediction_model,
        test_mock_attack_detector,
        test_rule_based_detector_dos,
        test_rule_based_detector_replay,
        test_rule_based_detector_unknown,
        test_incident_with_attack_prediction,
        test_build_metric_record_from_incident,
        test_full_ml_pipeline,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"[FAIL] {test.__name__}")
            print(f"       {e}")
            failed += 1
        except Exception as e:
            print(f"[ERROR] {test.__name__}")
            print(f"       {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 70)
    print(f"Result: {passed} passed, {failed} failed, {len(tests)} total")
    print("=" * 70)

    if failed > 0:
        sys.exit(1)
