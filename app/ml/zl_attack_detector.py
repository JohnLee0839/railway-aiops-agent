"""ZL STSRS model adapter.

This module adapts the trained model artifacts in the sibling ``ZL`` project to
the AIOps ``AttackDetector`` interface. It intentionally keeps training-time
logic out of railways_V.2 and only implements online inference.
"""

from __future__ import annotations

import math
import pickle
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, Mapping, Optional

from loguru import logger

from app.ml.attack_detector import (
    AttackDetector,
    AttackDetectorInferenceError,
    AttackDetectorInputError,
    AttackDetectorLoadError,
)
from app.models.metrics import AttackPrediction, RailMetricRecord


DEFAULT_ZL_LABEL_MAP = {
    "Normal": "UNKNOWN",
    "DoS": "DoS",
    "Jamming": "Jamming",
    "ReplayAttack": "Replay Attack",
}


@dataclass(frozen=True)
class ZLModelArtifact:
    model_name: str
    model_version: str
    model_path: Path
    feature_columns: list[str]
    target_mapping: dict[str, int]
    classifier: Any
    scaler: Any | None = None


class ZLFeatureAdapter:
    """Convert ``RailMetricRecord`` into the raw fields expected by ZL models."""

    _FIELD_MAP = {
        "Speed": "speed",
        "Distance": "distance",
        "Location": "location",
        "SignalStatus": "signal_status",
        "OverlapStatus": "overlap_status",
        "OverlapCount": "overlap_count",
        "PacketLoss": "packet_loss",
        "Latency": "latency",
        "RenewalInterval": "renewal_interval",
        "Burstiness": "burstiness",
    }

    _NUMERIC_FIELDS = {
        "Speed",
        "Distance",
        "Location",
        "OverlapCount",
        "PacketLoss",
        "Latency",
        "RenewalInterval",
        "Burstiness",
    }

    def to_raw_input(
        self,
        record: RailMetricRecord,
        required_fields: list[str],
    ) -> dict[str, Any]:
        values = self._extract_all(record)
        raw_input: dict[str, Any] = {}
        missing: list[str] = []

        for field_name in required_fields:
            if field_name not in values or values[field_name] is None:
                missing.append(field_name)
                continue
            raw_input[field_name] = values[field_name]

        if missing:
            raise AttackDetectorInputError(
                "Missing required ZL model fields: "
                + ", ".join(missing)
                + f" for record_id={record.record_id or '<empty>'}"
            )

        return raw_input

    def _extract_all(self, record: RailMetricRecord) -> dict[str, Any]:
        metrics = record.metrics
        values: dict[str, Any] = {}

        for zl_field, rail_field in self._FIELD_MAP.items():
            raw_value = getattr(metrics, rail_field, None)
            if zl_field == "RenewalInterval" and raw_value is None:
                raw_value = self._source_metric(record, "train", "renewal_interval")
                if raw_value is None:
                    raw_value = self._source_metric(
                        record, "control_center", "renewal_interval"
                    )

            if zl_field in self._NUMERIC_FIELDS:
                values[zl_field] = self._coerce_float(raw_value)
            elif zl_field == "SignalStatus":
                values[zl_field] = self._normalize_signal_status(raw_value)
            elif zl_field == "OverlapStatus":
                values[zl_field] = self._normalize_overlap_status(raw_value)
            else:
                values[zl_field] = raw_value

        return values

    @staticmethod
    def _source_metric(
        record: RailMetricRecord,
        source_name: str,
        metric_name: str,
    ) -> Any:
        source = record.source_metrics or {}
        source_values = source.get(source_name, {})
        if isinstance(source_values, Mapping):
            return source_values.get(metric_name)
        return None

    @staticmethod
    def _coerce_float(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed):
            return None
        return parsed

    @staticmethod
    def _normalize_signal_status(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        lookup = {
            "green": "Green",
            "yellow": "Yellow",
            "red": "Red",
            "danger": "Red",
            "offline": "Red",
            "failure": "Red",
        }
        return lookup.get(text.lower(), text)

    @staticmethod
    def _normalize_overlap_status(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        lookup = {
            "yes": "Yes",
            "true": "Yes",
            "1": "Yes",
            "abnormal": "Yes",
            "conflict": "Yes",
            "error": "Yes",
            "no": "No",
            "false": "No",
            "0": "No",
            "normal": "No",
            "unknown": "No",
        }
        return lookup.get(text.lower(), text)


class ZLAttackDetector(AttackDetector):
    """Inference adapter for the trained ZL STSRS threat detector."""

    def __init__(
        self,
        project_root: str | Path,
        model_version: str = "V2",
        model_path: str | Path | None = None,
        manifest_path: str | Path | None = None,
        confidence_threshold: float = 0.0,
        label_map: Optional[Dict[str, str]] = None,
        feature_adapter: Optional[ZLFeatureAdapter] = None,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.requested_model_version = model_version
        self.configured_model_path = Path(model_path).expanduser() if model_path else None
        self.configured_manifest_path = (
            Path(manifest_path).expanduser() if manifest_path else None
        )
        self.confidence_threshold = confidence_threshold
        self.label_map = dict(DEFAULT_ZL_LABEL_MAP)
        if label_map:
            self.label_map.update(label_map)
        self.feature_adapter = feature_adapter or ZLFeatureAdapter()
        self._artifact: ZLModelArtifact | None = None

    @property
    def model_version(self) -> str:
        if self._artifact:
            return f"zl-{self._artifact.model_version}:{self._artifact.model_name}"
        return f"zl-{self.requested_model_version}"

    def predict(self, metrics: RailMetricRecord) -> AttackPrediction:
        start = perf_counter()
        artifact = self._load_artifact()
        raw_input = self.feature_adapter.to_raw_input(metrics, artifact.feature_columns)

        np = self._import_numpy()
        x = np.asarray(
            [[raw_input[column] for column in artifact.feature_columns]],
            dtype=np.float64,
        )
        try:
            transformed_x = x if artifact.scaler is None else artifact.scaler.transform(x)
        except Exception as exc:
            raise AttackDetectorInferenceError(str(exc)) from exc
        raw_probabilities = self._predict_probabilities(artifact, transformed_x)

        id_to_label = {
            label_id: label for label, label_id in artifact.target_mapping.items()
        }
        predicted_label_id = int(np.argmax(raw_probabilities))
        raw_label = id_to_label[predicted_label_id]
        confidence = float(raw_probabilities[predicted_label_id])
        raw_probability_map = {
            id_to_label[label_id]: float(raw_probabilities[label_id])
            for label_id in sorted(id_to_label)
        }
        probabilities = self._map_probabilities(raw_probability_map)
        attack_type = self._map_label(raw_label)
        if confidence < self.confidence_threshold:
            attack_type = "UNKNOWN"

        logger.info(
            "[ZLAttackDetector] predict: "
            f"record={metrics.record_id or '<empty>'}, "
            f"raw_label={raw_label}, attack_type={attack_type}, "
            f"confidence={confidence:.0%}, model={artifact.model_name}"
        )

        return AttackPrediction(
            attack_type=attack_type,
            confidence=confidence,
            probabilities=probabilities,
            model_version=self.model_version,
            detector_backend="zl",
            fallback_used=False,
            fallback_reason=None,
            inference_ms=round((perf_counter() - start) * 1000.0, 3),
            feature_vector={
                "feature_columns": artifact.feature_columns,
                "raw_input": raw_input,
                "raw_label": raw_label,
                "raw_probabilities": raw_probability_map,
                "model_path": str(artifact.model_path),
            },
        )

    def _load_artifact(self) -> ZLModelArtifact:
        if self._artifact is not None:
            return self._artifact

        model_path = self._resolve_model_path()
        try:
            with model_path.open("rb") as handle:
                payload = pickle.load(handle)

            feature_columns = [str(column) for column in payload["feature_columns"]]
            target_mapping = {
                str(label): int(label_id)
                for label, label_id in payload["target_mapping"].items()
            }
            classifier = payload["classifier"]
        except FileNotFoundError as exc:
            raise AttackDetectorLoadError(str(exc)) from exc
        except (ImportError, ModuleNotFoundError) as exc:
            raise AttackDetectorLoadError(
                f"ZL model dependency unavailable while loading {model_path}: {exc}"
            ) from exc
        except (pickle.UnpicklingError, EOFError, KeyError, TypeError, ValueError) as exc:
            raise AttackDetectorLoadError(
                f"ZL model artifact is incompatible or incomplete: {exc}"
            ) from exc

        self._artifact = ZLModelArtifact(
            model_name=str(payload.get("model_name", model_path.stem)),
            model_version=self.requested_model_version,
            model_path=model_path,
            feature_columns=feature_columns,
            target_mapping=target_mapping,
            classifier=classifier,
            scaler=payload.get("scaler"),
        )
        logger.info(
            "[ZLAttackDetector] loaded model: "
            f"version={self._artifact.model_version}, "
            f"name={self._artifact.model_name}, path={model_path}, "
            f"features={feature_columns}"
        )
        return self._artifact

    def _resolve_model_path(self) -> Path:
        candidates: list[Path] = []

        if self.configured_model_path:
            candidates.append(self._resolve_under_project(self.configured_model_path))

        manifest = self._load_manifest()
        if manifest:
            result = manifest.get("result", {})
            manifest_model_path = result.get("model_path")
            model_name = result.get("model_name")
            if manifest_model_path:
                candidates.append(self._resolve_under_project(Path(str(manifest_model_path))))
            if model_name:
                candidates.append(
                    self.project_root / "models" / "baseline" / f"{model_name}.pkl"
                )

        candidates.append(
            self.project_root
            / "models"
            / "baseline"
            / "v2_compact_top3_hist_gradient_boosting.pkl"
        )

        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate.resolve()

        rendered = ", ".join(str(candidate) for candidate in candidates)
        raise AttackDetectorLoadError(f"ZL model file not found. Tried: {rendered}")

    def _load_manifest(self) -> dict[str, Any] | None:
        import json

        manifest_path = self.configured_manifest_path
        if manifest_path is None:
            manifest_path = (
                self.project_root
                / "metadata"
                / "manifests"
                / "v2_compact_tree_manifest.json"
            )
        manifest_path = self._resolve_under_project(manifest_path)
        if not manifest_path.exists():
            return None
        with manifest_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _resolve_under_project(self, path: Path) -> Path:
        if path.is_absolute() and path.exists():
            return path
        if path.is_absolute():
            return self.project_root / path.name if path.name else path
        return self.project_root / path

    @staticmethod
    def _import_numpy():
        try:
            import numpy as np
        except ImportError as exc:
            raise AttackDetectorLoadError(
                "ZLAttackDetector requires numpy. Install railways_V.2 ML "
                "dependencies before enabling the ZL backend."
            ) from exc
        return np

    def _predict_probabilities(self, artifact: ZLModelArtifact, transformed_x: Any) -> Any:
        np = self._import_numpy()
        classifier = artifact.classifier
        if hasattr(classifier, "predict_proba"):
            try:
                probabilities = np.asarray(
                    classifier.predict_proba(transformed_x)[0],
                    dtype=np.float64,
                )
            except Exception as exc:
                raise AttackDetectorInferenceError(str(exc)) from exc
        elif hasattr(classifier, "decision_function"):
            try:
                logits = np.asarray(
                    classifier.decision_function(transformed_x)[0],
                    dtype=np.float64,
                )
            except Exception as exc:
                raise AttackDetectorInferenceError(str(exc)) from exc
            stabilized = logits - float(np.max(logits))
            exponentiated = np.exp(stabilized)
            probabilities = exponentiated / float(np.sum(exponentiated))
        else:
            raise AttackDetectorInferenceError(
                f"Loaded ZL model {artifact.model_name} does not support probabilities."
            )

        if probabilities.ndim != 1:
            raise AttackDetectorInferenceError(
                f"Expected 1D probability output, got shape {probabilities.shape}."
            )
        if probabilities.shape[0] != len(artifact.target_mapping):
            raise AttackDetectorInferenceError(
                "Probability output length does not match target mapping size: "
                f"{probabilities.shape[0]} != {len(artifact.target_mapping)}"
            )
        total = float(sum(probabilities))
        if not math.isfinite(total) or abs(total - 1.0) > 1e-6:
            raise AttackDetectorInferenceError(
                f"Predicted probabilities must sum to 1.0, got {total}."
            )
        return probabilities

    def _map_label(self, raw_label: str) -> str:
        return self.label_map.get(raw_label, raw_label)

    def _map_probabilities(self, raw_probabilities: dict[str, float]) -> dict[str, float]:
        mapped: dict[str, float] = {}
        for raw_label, probability in raw_probabilities.items():
            label = self._map_label(raw_label)
            mapped[label] = mapped.get(label, 0.0) + float(probability)
        return mapped
