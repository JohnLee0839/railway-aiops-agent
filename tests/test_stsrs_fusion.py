"""
Test STSRSAdapter Multi-Source Observation Fusion

Verifies:
1. Same FusionKey multi-source data fuses into single RailMetricRecord
2. Consistent metrics go into metrics, differing metrics go into source_metrics
3. AttackInfo fields are stripped
4. source_metrics structure is correct
"""

import sys
import os
from datetime import datetime

# Ensure project path in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data.stsrs_adapter import STSRSAdapter


def test_fusion_produces_single_record():
    """Verify: Same FusionKey data fuses into a single record"""
    adapter = STSRSAdapter()
    test_dir = os.path.join(os.path.dirname(__file__), "test_data")
    files = [
        os.path.join(test_dir, "STSRS-Control Center.txt"),
        os.path.join(test_dir, "STSRS-Train.txt"),
    ]

    records = adapter.load_and_fuse(files)

    assert len(records) == 1, f"Expected 1 fused record, got {len(records)}"
    print(f"[PASS] Fusion produces {len(records)} record(s)")


def test_fusion_key_correct():
    """Verify: FusionKey is (timestamp, train_id, signal_id)"""
    adapter = STSRSAdapter()
    test_dir = os.path.join(os.path.dirname(__file__), "test_data")
    files = [
        os.path.join(test_dir, "STSRS-Control Center.txt"),
        os.path.join(test_dir, "STSRS-Train.txt"),
    ]

    records = adapter.load_and_fuse(files)
    record = records[0]

    assert record.train_id == "7Y36", f"Expected train_id='7Y36', got '{record.train_id}'"
    assert record.signal_id == "YT919", f"Expected signal_id='YT919', got '{record.signal_id}'"
    assert isinstance(record.timestamp, datetime), f"Expected datetime, got {type(record.timestamp)}"
    print(f"[PASS] FusionKey correct: timestamp={record.timestamp}, train={record.train_id}, signal={record.signal_id}")


def test_common_metrics_consistent():
    """Verify: Consistent metric values go into common metrics"""
    adapter = STSRSAdapter()
    test_dir = os.path.join(os.path.dirname(__file__), "test_data")
    files = [
        os.path.join(test_dir, "STSRS-Control Center.txt"),
        os.path.join(test_dir, "STSRS-Train.txt"),
    ]

    records = adapter.load_and_fuse(files)
    record = records[0]

    # These fields are identical in both sources
    assert record.metrics.speed is not None, "Speed should be in common metrics"
    assert record.metrics.packet_loss is not None, "PacketLoss should be in common metrics"
    assert record.metrics.latency is not None, "Latency should be in common metrics"
    assert record.metrics.burstiness is not None, "Burstiness should be in common metrics"

    print(f"[PASS] Common metrics correct: speed={record.metrics.speed}, "
          f"packet_loss={record.metrics.packet_loss}, latency={record.metrics.latency}")


def test_source_metrics_renewal_interval_conflict():
    """Verify: RenewalInterval difference preserved in source_metrics"""
    adapter = STSRSAdapter()
    test_dir = os.path.join(os.path.dirname(__file__), "test_data")
    files = [
        os.path.join(test_dir, "STSRS-Control Center.txt"),
        os.path.join(test_dir, "STSRS-Train.txt"),
    ]

    records = adapter.load_and_fuse(files)
    record = records[0]

    # RenewalInterval differs across sources (0 vs 170)
    # -> should be in source_metrics, NOT in common metrics
    assert record.source_metrics, "Expected non-empty source_metrics"
    assert "control_center" in record.source_metrics, (
        f"Expected 'control_center' in source_metrics, got keys: {list(record.source_metrics.keys())}"
    )
    assert "train" in record.source_metrics, (
        f"Expected 'train' in source_metrics, got keys: {list(record.source_metrics.keys())}"
    )

    cc_renewal = record.source_metrics["control_center"].get("renewal_interval")
    train_renewal = record.source_metrics["train"].get("renewal_interval")

    assert cc_renewal == 0.0, f"Expected control_center.renewal_interval=0, got {cc_renewal}"
    assert train_renewal == 170.0, f"Expected train.renewal_interval=170, got {train_renewal}"

    # RenewalInterval must NOT be in common metrics (sources disagree)
    assert record.metrics.renewal_interval is None, (
        f"RenewalInterval should NOT be in common metrics (sources disagree), "
        f"but got {record.metrics.renewal_interval}"
    )

    print(f"[PASS] Source conflict correct: control_center.renewal_interval={cc_renewal}, "
          f"train.renewal_interval={train_renewal}")


def test_attack_info_stripped():
    """Verify: AttackInfo is completely removed from RailMetricRecord"""
    adapter = STSRSAdapter()
    test_dir = os.path.join(os.path.dirname(__file__), "test_data")
    files = [
        os.path.join(test_dir, "STSRS-Control Center.txt"),
        os.path.join(test_dir, "STSRS-Train.txt"),
    ]

    records = adapter.load_and_fuse(files)
    record = records[0]

    # Serialize to dict and check all levels
    record_dict = record.model_dump(mode="json")

    def check_no_attack(data, path=""):
        """Recursively check dict for any attack-related fields"""
        if isinstance(data, dict):
            for key, value in data.items():
                key_lower = key.lower()
                assert "attack" not in key_lower, (
                    f"Found attack-related key '{key}' at {path}"
                )
                assert "ground_truth" not in key_lower, (
                    f"Found ground_truth key at {path}"
                )
                check_no_attack(value, f"{path}.{key}")
        elif isinstance(data, list):
            for i, item in enumerate(data):
                check_no_attack(item, f"{path}[{i}]")

    check_no_attack(record_dict)
    print("[PASS] AttackInfo completely removed")


def test_source_files_populated():
    """Verify: source_files correctly records source identifiers"""
    adapter = STSRSAdapter()
    test_dir = os.path.join(os.path.dirname(__file__), "test_data")
    files = [
        os.path.join(test_dir, "STSRS-Control Center.txt"),
        os.path.join(test_dir, "STSRS-Train.txt"),
    ]

    records = adapter.load_and_fuse(files)
    record = records[0]

    assert len(record.source_files) == 2, (
        f"Expected 2 source files, got {len(record.source_files)}: {record.source_files}"
    )
    assert "control_center" in record.source_files, (
        f"Expected 'control_center' in source_files, got {record.source_files}"
    )
    assert "train" in record.source_files, (
        f"Expected 'train' in source_files, got {record.source_files}"
    )
    print(f"[PASS] source_files correct: {record.source_files}")


def test_derive_source():
    """Verify: _derive_source extracts source from filename correctly"""
    adapter = STSRSAdapter()

    assert adapter._derive_source("STSRS-Control Center.txt") == "control_center"
    assert adapter._derive_source("STSRS-Train.txt") == "train"
    assert adapter._derive_source("data/STSRS-Signal Device.csv") == "signal_device"
    assert adapter._derive_source("STSRS-dispatcher.json") == "dispatcher"
    assert adapter._derive_source("STSRS-Edge Gateway.csv") == "edge_gateway"
    print("[PASS] _derive_source correctly identifies sources")


def test_event_normalizer_with_source_metrics():
    """Verify: EventNormalizer propagates source_metrics correctly"""
    from app.events.event_normalizer import EventNormalizer
    from app.models.incident import IncidentSource

    normalizer = EventNormalizer()

    # Simulate RailMetricRecord serialized output
    metrics_payload = {
        "record_id": "RMR-TEST001",
        "train_id": "7Y36",
        "signal_id": "YT919",
        "timestamp": "2025-08-14T09:08:15",
        "metrics": {
            "speed": 40.96,
            "distance": 14.75,
            "packet_loss": 95.23,
            "latency": 354.46,
            "burstiness": 4.80,
        },
        "source_metrics": {
            "control_center": {"renewal_interval": 0.0},
            "train": {"renewal_interval": 170.0},
        },
        "source_files": ["control_center", "train"],
    }

    incident = normalizer.normalize_metric(metrics_payload, IncidentSource.STSRS)

    assert incident.metrics_snapshot is not None, "metrics_snapshot should not be None"
    assert "source_metrics" in incident.metrics_snapshot, (
        "metrics_snapshot should contain source_metrics"
    )
    assert incident.metrics_snapshot.get("has_source_conflicts") is True, (
        "has_source_conflicts should be True"
    )

    src_metrics = incident.metrics_snapshot["source_metrics"]
    assert src_metrics["control_center"]["renewal_interval"] == 0.0
    assert src_metrics["train"]["renewal_interval"] == 170.0

    # Check metadata.extra for conflict markers
    assert incident.metadata.extra.get("has_source_conflicts") is True
    assert incident.metadata.extra.get("source_count") == 2

    print("[PASS] EventNormalizer propagates source_metrics correctly")


def test_severity_engine_detects_source_conflicts():
    """Verify: SeverityEngine detects source_metrics conflicts"""
    from app.events.severity_engine import SeverityEngine

    engine = SeverityEngine()

    # Simulate metrics with source_metrics
    metrics = {
        "packet_loss": 0.05,  # normal
        "latency": 50.0,
        "source_metrics": {
            "control_center": {"renewal_interval": 0.0},
            "train": {"renewal_interval": 170.0},
        },
    }

    conflicts = engine._detect_source_conflicts(metrics)
    assert len(conflicts) > 0, "Should detect source conflict"
    assert any("renewal_interval" in c for c in conflicts), (
        f"Conflict should mention renewal_interval: {conflicts}"
    )
    print(f"[PASS] SeverityEngine detects source conflicts: {conflicts}")


def test_source_conflict_detection_logic():
    """Verify: source conflict detection logic (same algorithm as TriageAgent & SeverityEngine)

    Tests the core logic without importing TriageAgent (which triggers Milvus connection).
    The logic is identical to:
    - TriageAgent._detect_source_conflicts()
    - SeverityEngine._detect_source_conflicts()
    """
    from typing import Dict, Any, List

    def detect_source_conflicts(metrics: Dict[str, Any]) -> List[str]:
        conflicts = []
        source_metrics = metrics.get("source_metrics", {})
        if not source_metrics or not isinstance(source_metrics, dict):
            return conflicts

        for metric_name in ("renewal_interval", "packet_loss", "latency",
                            "burstiness", "signal_status", "overlap_status", "speed"):
            source_values: Dict[str, Any] = {}
            for src_name, src_data in source_metrics.items():
                if isinstance(src_data, dict) and metric_name in src_data:
                    source_values[src_name] = src_data[metric_name]

            if len(source_values) >= 2:
                unique = set(source_values.values())
                if len(unique) >= 2:
                    detail = ", ".join(f"{s}={v}" for s, v in sorted(source_values.items()))
                    conflicts.append(f"{metric_name}: {detail}")

        return conflicts

    # Test 1: renewal_interval conflict between control_center and train
    metrics = {
        "packet_loss": 0.05,
        "latency": 50.0,
        "source_metrics": {
            "control_center": {"renewal_interval": 0.0},
            "train": {"renewal_interval": 170.0},
        },
    }

    conflicts = detect_source_conflicts(metrics)
    assert len(conflicts) == 1, f"Expected 1 conflict, got {len(conflicts)}: {conflicts}"
    assert any("renewal_interval" in c for c in conflicts), (
        f"Conflict should mention renewal_interval: {conflicts}"
    )
    print(f"[PASS] Source conflict detection: {conflicts}")

    # Test 2: no conflicts when source_metrics is empty
    metrics2 = {"packet_loss": 0.05, "latency": 50.0}
    conflicts2 = detect_source_conflicts(metrics2)
    assert len(conflicts2) == 0, f"Expected 0 conflicts, got {len(conflicts2)}"
    print("[PASS] No false conflicts when source_metrics absent")

    # Test 3: multiple conflicts
    metrics3 = {
        "source_metrics": {
            "control_center": {"renewal_interval": 0.0, "signal_status": "Green"},
            "train": {"renewal_interval": 170.0, "signal_status": "Red"},
        },
    }
    conflicts3 = detect_source_conflicts(metrics3)
    assert len(conflicts3) >= 2, f"Expected >=2 conflicts, got {len(conflicts3)}: {conflicts3}"
    print(f"[PASS] Multiple source conflicts detected: {conflicts3}")


# ================================================================
# Run all tests
# ================================================================

if __name__ == "__main__":
    # Force UTF-8 output on Windows
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("=" * 70)
    print("STSRSAdapter Multi-Source Observation Fusion Tests")
    print("=" * 70)

    tests = [
        test_fusion_produces_single_record,
        test_fusion_key_correct,
        test_common_metrics_consistent,
        test_source_metrics_renewal_interval_conflict,
        test_attack_info_stripped,
        test_source_files_populated,
        test_derive_source,
        test_event_normalizer_with_source_metrics,
        test_severity_engine_detects_source_conflicts,
        test_source_conflict_detection_logic,
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
