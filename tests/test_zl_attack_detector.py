import pickle
from datetime import datetime

import pytest

from app.models.incident import (
    ActionStatus,
    IncidentSource,
    TriageResult,
)
from app.models.incident import (
    IncidentState,
    MockActionResult,
    RunbookPlan,
    Severity,
    VerificationResult,
)
from app.models.metrics import AttackPrediction, RailMetricRecord, RailMetrics


class FakeClassifier:
    def predict_proba(self, _x):
        return [[0.02, 0.91, 0.04, 0.03]]


def _metric_record(**overrides):
    metrics = {
        "speed": 40.96,
        "distance": 14.75,
        "packet_loss": 95.23,
        "latency": 354.46,
        "burstiness": 4.80,
        "signal_status": "Green",
        "overlap_status": "No",
        "overlap_count": 0,
    }
    metrics.update(overrides)
    return RailMetricRecord(
        record_id="RMR-ZL-TEST",
        timestamp=datetime.utcnow(),
        train_id="7Y36",
        signal_id="YT919",
        metrics=RailMetrics(**metrics),
        source_metrics={
            "control_center": {"renewal_interval": 0.0},
            "train": {"renewal_interval": 170.0},
        },
        source_files=["control_center", "train"],
    )


def test_zl_feature_adapter_maps_compact_v2_fields():
    from app.ml.zl_attack_detector import ZLFeatureAdapter

    adapter = ZLFeatureAdapter()
    raw_input = adapter.to_raw_input(
        _metric_record(),
        required_fields=["Distance", "PacketLoss", "Latency"],
    )

    assert raw_input == {
        "Distance": 14.75,
        "PacketLoss": 95.23,
        "Latency": 354.46,
    }


def test_zl_feature_adapter_reports_missing_required_fields():
    from app.ml.attack_detector import AttackDetectorInputError
    from app.ml.zl_attack_detector import ZLFeatureAdapter

    adapter = ZLFeatureAdapter()
    record = _metric_record(distance=None)

    with pytest.raises(AttackDetectorInputError, match="Distance"):
        adapter.to_raw_input(record, required_fields=["Distance", "PacketLoss", "Latency"])


def test_event_normalizer_unwraps_process_metrics_payload():
    from app.events.event_normalizer import EventNormalizer

    payload = {
        "metrics_snapshot": {
            "record_id": "RMR-METRICS",
            "timestamp": "2025-08-14T09:08:15",
            "train_id": "7Y36",
            "signal_id": "YT919",
            "metrics": {
                "distance": 14.75,
                "packet_loss": 95.23,
                "latency": 354.46,
            },
            "source_metrics": {
                "control_center": {"renewal_interval": 0.0},
                "train": {"renewal_interval": 170.0},
            },
            "source_files": ["control_center", "train"],
        },
        "train_id": "7Y36",
        "signal_id": "YT919",
    }

    incident = EventNormalizer().normalize(payload, IncidentSource.STSRS)

    assert incident.metrics_snapshot is not None
    assert incident.metrics_snapshot["record_id"] == "RMR-METRICS"
    assert incident.metrics_snapshot["metrics"]["packet_loss"] == 95.23
    assert incident.metrics_snapshot["source_metrics"]["train"]["renewal_interval"] == 170.0


def test_zl_attack_detector_predicts_with_pickle_payload(tmp_path):
    pytest.importorskip("numpy")

    from app.ml.zl_attack_detector import ZLAttackDetector

    model_path = tmp_path / "model.pkl"
    with model_path.open("wb") as handle:
        pickle.dump(
            {
                "model_name": "fake_zl_model",
                "feature_columns": ["Distance", "PacketLoss", "Latency"],
                "target_mapping": {
                    "Normal": 0,
                    "DoS": 1,
                    "Jamming": 2,
                    "ReplayAttack": 3,
                },
                "classifier": FakeClassifier(),
            },
            handle,
        )

    detector = ZLAttackDetector(
        project_root=tmp_path,
        model_version="TEST",
        model_path=model_path,
    )
    prediction = detector.predict(_metric_record())

    assert isinstance(prediction, AttackPrediction)
    assert prediction.attack_type == "DoS"
    assert prediction.confidence == pytest.approx(0.91)
    assert prediction.probabilities["UNKNOWN"] == pytest.approx(0.02)
    assert prediction.probabilities["Replay Attack"] == pytest.approx(0.03)
    assert prediction.model_version == "zl-TEST:fake_zl_model"
    assert prediction.detector_backend == "zl"
    assert prediction.fallback_used is False
    assert prediction.fallback_reason is None
    assert prediction.inference_ms is not None
    assert prediction.feature_vector["raw_input"]["PacketLoss"] == 95.23


def test_zl_attack_detector_confidence_threshold_maps_to_unknown(tmp_path):
    pytest.importorskip("numpy")

    from app.ml.zl_attack_detector import ZLAttackDetector

    model_path = tmp_path / "model.pkl"
    with model_path.open("wb") as handle:
        pickle.dump(
            {
                "model_name": "fake_zl_model",
                "feature_columns": ["Distance", "PacketLoss", "Latency"],
                "target_mapping": {
                    "Normal": 0,
                    "DoS": 1,
                    "Jamming": 2,
                    "ReplayAttack": 3,
                },
                "classifier": FakeClassifier(),
            },
            handle,
        )

    detector = ZLAttackDetector(
        project_root=tmp_path,
        model_version="TEST",
        model_path=model_path,
        confidence_threshold=0.95,
    )
    prediction = detector.predict(_metric_record())

    assert prediction.attack_type == "UNKNOWN"
    assert prediction.confidence == pytest.approx(0.91)


def test_fallback_detector_marks_model_load_failure(tmp_path):
    from app.ml.attack_detector import FallbackAttackDetector, RuleBasedAttackDetector
    from app.ml.zl_attack_detector import ZLAttackDetector

    primary = ZLAttackDetector(
        project_root=tmp_path,
        model_version="TEST",
        model_path=tmp_path / "missing.pkl",
    )
    detector = FallbackAttackDetector(primary, RuleBasedAttackDetector())

    prediction = detector.predict(_metric_record())

    assert prediction.detector_backend == "rule"
    assert prediction.fallback_used is True
    assert prediction.fallback_reason == "model_load_failed"
    assert prediction.inference_ms is not None
    assert prediction.feature_vector["primary_detector"] == "ZLAttackDetector"


def test_fallback_detector_does_not_hide_invalid_input(tmp_path):
    pytest.importorskip("numpy")

    from app.ml.attack_detector import (
        AttackDetectorInputError,
        FallbackAttackDetector,
        RuleBasedAttackDetector,
    )
    from app.ml.zl_attack_detector import ZLAttackDetector

    model_path = tmp_path / "model.pkl"
    with model_path.open("wb") as handle:
        pickle.dump(
            {
                "model_name": "fake_zl_model",
                "feature_columns": ["Distance", "PacketLoss", "Latency"],
                "target_mapping": {
                    "Normal": 0,
                    "DoS": 1,
                    "Jamming": 2,
                    "ReplayAttack": 3,
                },
                "classifier": FakeClassifier(),
            },
            handle,
        )

    primary = ZLAttackDetector(
        project_root=tmp_path,
        model_version="TEST",
        model_path=model_path,
    )
    detector = FallbackAttackDetector(primary, RuleBasedAttackDetector())

    with pytest.raises(AttackDetectorInputError):
        detector.predict(_metric_record(distance=None))


class DirectDeduplicator:
    def process(self, incident):
        return incident

    def drain_output(self):
        return []


class StubDetector:
    @property
    def model_version(self):
        return "stub-v1"

    def predict(self, _metrics):
        return AttackPrediction(
            attack_type="DoS",
            confidence=0.99,
            probabilities={"DoS": 0.99, "UNKNOWN": 0.01},
            model_version=self.model_version,
        )


class StubTriageAgent:
    async def triage(self, _incident):
        return TriageResult(
            root_cause="High packet loss and latency match DoS",
            attack_type="DoS",
            severity=Severity.P1,
            impact_scope=["Train-7Y36", "Signal-YT919"],
            upstream_assets=[],
            downstream_assets=[],
            confidence=0.98,
            evidence=["AttackDetector predicted DoS"],
        )


class StubRunbookAgent:
    async def generate_plan(self, _incident, _triage_result):
        return RunbookPlan(
            actions=[{
                "action": "verify_network_health",
                "description": "验证网络健康",
            }],
            source_kb="TestKB",
            confidence=0.95,
            reasoning="test",
        )


class StubActionOrchestrator:
    async def execute_plan(self, _incident, _plan, _thread_id):
        return [
            MockActionResult(
                action_name="verify_network_health",
                success=True,
                message="ok",
            )
        ]


class StubVerifier:
    async def verify(self, incident, _plan, _results, _thread_id, retry_cycle=0):
        return VerificationResult(
            incident_id=incident.incident_id,
            action_status=ActionStatus.SUCCESS,
            reason="all good",
            next_state=IncidentState.VERIFIED,
        )


class StubReplanner:
    async def decide(self, _incident_id, _thread_id, _trace_id, _verification):
        from app.agents.replanner import ReplanAction

        return ReplanAction.RESOLVE


@pytest.mark.asyncio
async def test_metrics_route_carries_attack_prediction_through_pipeline():
    from app.core.incident_router import IncidentRouter
    from app.core.incident_store import incident_store

    router = IncidentRouter(attack_detector=StubDetector())
    router.deduplicator = DirectDeduplicator()
    router.triage_agent = StubTriageAgent()
    router.runbook_agent = StubRunbookAgent()
    router.action_orchestrator = StubActionOrchestrator()
    router.verifier = StubVerifier()
    router.replanner = StubReplanner()

    events = []
    async for event in router.route(
        raw_event={
            "record_id": "RMR-E2E",
            "timestamp": "2025-08-14T09:08:15",
            "train_id": "7Y36",
            "signal_id": "YT919",
            "metrics": {
                "distance": 14.75,
                "packet_loss": 95.23,
                "latency": 354.46,
                "burstiness": 4.8,
                "signal_status": "Green",
                "overlap_status": "No",
            },
        },
        source=IncidentSource.STSRS,
        thread_id="thread-zl-e2e",
    ):
        events.append(event)

    created = next(event for event in events if event["type"] == "incident_created")
    incident_id = created["incident_id"]
    try:
        detector_event = next(
            event
            for event in events
            if event["type"] == "incident_triaged"
            and "attack_prediction" in event.get("data", {})
        )
        complete_event = events[-1]

        assert detector_event["data"]["attack_prediction"]["attack_type"] == "DoS"
        assert detector_event["data"]["attack_prediction"]["model_version"] == "stub-v1"
        assert complete_event["type"] == "complete"
        assert complete_event["final_state"] == "RESOLVED"
        assert complete_event["triage"]["attack_type"] == "DoS"
    finally:
        incident_store.delete(incident_id)


@pytest.mark.asyncio
async def test_single_metrics_request_bypasses_buffered_dedup():
    from app.core.incident_router import IncidentRouter
    from app.core.incident_store import incident_store

    router = IncidentRouter(attack_detector=StubDetector())
    router.triage_agent = StubTriageAgent()
    router.runbook_agent = StubRunbookAgent()
    router.action_orchestrator = StubActionOrchestrator()
    router.verifier = StubVerifier()
    router.replanner = StubReplanner()

    events = []
    async for event in router.route(
        raw_event={
            "record_id": "RMR-SINGLE",
            "timestamp": "2025-08-14T09:08:15",
            "train_id": "7Y36",
            "signal_id": "YT919",
            "metrics": {
                "distance": 14.75,
                "packet_loss": 95.23,
                "latency": 354.46,
                "burstiness": 4.8,
            },
        },
        source=IncidentSource.STSRS,
        thread_id="thread-zl-single",
    ):
        events.append(event)

    created = next(event for event in events if event["type"] == "incident_created")
    incident_id = created["incident_id"]
    try:
        assert not any(event.get("stage") == "dedup" for event in events)
        assert events[-1]["type"] == "complete"
        assert events[-1]["final_state"] == "RESOLVED"
    finally:
        incident_store.delete(incident_id)


def test_real_zl_v2_pickle_smoke_if_environment_available():
    pytest.importorskip("numpy")
    pytest.importorskip("sklearn")

    from pathlib import Path

    repository_root = Path(__file__).resolve().parents[1]
    model_path = repository_root / "ml" / "models" / "baseline" / (
        "v2_compact_top3_hist_gradient_boosting.pkl"
    )

    from app.ml.zl_attack_detector import ZLAttackDetector

    detector = ZLAttackDetector(
        project_root=repository_root / "ml",
        model_version="V2",
        model_path=model_path,
    )
    prediction = detector.predict(_metric_record())

    assert prediction.model_version.startswith("zl-V2:")
    assert prediction.detector_backend == "zl"
    assert prediction.fallback_used is False
    assert set(prediction.probabilities) == {
        "UNKNOWN",
        "DoS",
        "Jamming",
        "Replay Attack",
    }
    assert sum(prediction.probabilities.values()) == pytest.approx(1.0)
