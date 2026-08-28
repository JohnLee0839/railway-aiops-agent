from __future__ import annotations

import json
import math
import pickle
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from stsrs_data_engineering.config import ensure_parent, load_pipeline_config, load_yaml_file


@dataclass(frozen=True)
class ModelRegistryEntry:
    model_version: str
    manifest_path: Path
    description: str


@dataclass
class LoadedModelArtifact:
    model_version: str
    model_name: str
    manifest_path: Path
    model_path: Path
    feature_columns: list[str]
    target_column: str
    target_id_column: str
    target_mapping: dict[str, int]
    classifier: Any
    scaler: Any | None
    config: dict[str, Any]


@dataclass(frozen=True)
class InputValidationResult:
    validated_raw_input: dict[str, Any]
    required_raw_fields: list[str]
    encoded_features: dict[str, float]


@dataclass(frozen=True)
class PredictionResult:
    request_id: str
    generated_at: str
    model_version: str
    model_name: str
    predicted_label: str
    predicted_label_id: int
    confidence: float
    probabilities: dict[str, float]
    validated_raw_input: dict[str, Any]
    encoded_features: dict[str, float]
    required_raw_fields: list[str]


class FeatureBuilder:
    def __init__(self, schema_config: dict[str, Any], encoded_feature_manifest: dict[str, Any]) -> None:
        self._schema_columns = list(schema_config["columns"])
        self._encoded_result = encoded_feature_manifest["result"]
        self._numeric_features = {
            str(column["name"]): dict(column)
            for column in self._schema_columns
            if str(column["role"]) == "feature" and str(column["logical_type"]) == "numeric"
        }
        self._categorical_features = {
            str(column["name"]): {
                "allowed_values": [str(value) for value in column["allowed_values"]],
                "nullable": bool(column["nullable"]),
            }
            for column in self._schema_columns
            if str(column["role"]) == "feature" and str(column["logical_type"]) == "categorical"
        }
        self._encoded_feature_columns = list(self._encoded_result["encoded_feature_columns"])

    def required_raw_fields_for_model(self, feature_columns: list[str]) -> list[str]:
        raw_fields: list[str] = []
        for feature_name in feature_columns:
            if feature_name in self._numeric_features:
                raw_fields.append(feature_name)
                continue
            source_name = self._parse_categorical_feature_name(feature_name)
            raw_fields.append(source_name)
        return sorted(set(raw_fields))

    def build_features(
        self,
        raw_input: Mapping[str, Any],
        feature_columns: list[str],
    ) -> InputValidationResult:
        validated_raw_input: dict[str, Any] = {}
        required_raw_fields = self.required_raw_fields_for_model(feature_columns)

        for field_name in required_raw_fields:
            if field_name not in raw_input:
                raise ValueError(f"Missing required input field: {field_name}")

            raw_value = raw_input[field_name]
            if field_name in self._numeric_features:
                validated_raw_input[field_name] = self._coerce_numeric(field_name, raw_value)
            else:
                validated_raw_input[field_name] = self._coerce_categorical(field_name, raw_value)

        encoded_features: dict[str, float] = {}
        for feature_name in feature_columns:
            if feature_name in self._numeric_features:
                encoded_features[feature_name] = float(validated_raw_input[feature_name])
                continue

            source_name, allowed_value = self._split_categorical_feature_name(feature_name)
            source_value = str(validated_raw_input[source_name])
            encoded_features[feature_name] = 1.0 if source_value == allowed_value else 0.0

        return InputValidationResult(
            validated_raw_input=validated_raw_input,
            required_raw_fields=required_raw_fields,
            encoded_features=encoded_features,
        )

    def _parse_categorical_feature_name(self, feature_name: str) -> str:
        source_name, _allowed_value = self._split_categorical_feature_name(feature_name)
        return source_name

    def _split_categorical_feature_name(self, feature_name: str) -> tuple[str, str]:
        prefix = "__is_"
        if prefix not in feature_name:
            raise ValueError(f"Unsupported feature column for online encoding: {feature_name}")
        source_name, allowed_value = feature_name.split(prefix, maxsplit=1)
        if source_name not in self._categorical_features:
            raise ValueError(f"Unknown categorical source feature: {source_name}")
        if allowed_value not in self._categorical_features[source_name]["allowed_values"]:
            raise ValueError(
                f"Feature {feature_name} references unknown categorical value {allowed_value} for {source_name}"
            )
        return source_name, allowed_value

    def _coerce_numeric(self, field_name: str, raw_value: Any) -> float:
        if isinstance(raw_value, bool):
            raise ValueError(f"Field {field_name} must be numeric, but received boolean.")
        try:
            numeric_value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Field {field_name} must be numeric, but received {raw_value!r}.") from exc
        if not math.isfinite(numeric_value):
            raise ValueError(f"Field {field_name} must be finite, but received {raw_value!r}.")
        return numeric_value

    def _coerce_categorical(self, field_name: str, raw_value: Any) -> str:
        if raw_value is None:
            raise ValueError(f"Field {field_name} must be non-null.")
        category = str(raw_value)
        allowed_values = self._categorical_features[field_name]["allowed_values"]
        if category not in allowed_values:
            raise ValueError(
                f"Field {field_name} has unsupported value {category!r}. Allowed values: {allowed_values}"
            )
        return category


class ModelService:
    def __init__(self, project_root: Path | None = None) -> None:
        self._project_root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        self._service_config_path = self._project_root / "configs" / "serving" / "model_service.yaml"
        self._service_config = load_yaml_file(self._service_config_path)
        self._pipeline_config = load_pipeline_config(self._project_root)
        self._encoded_feature_manifest = self._load_json(
            self._project_root / "metadata" / "manifests" / "encoded_feature_manifest.json"
        )
        self._feature_builder = FeatureBuilder(
            schema_config=self._pipeline_config["schema"],
            encoded_feature_manifest=self._encoded_feature_manifest,
        )
        self._registry = self._build_registry()
        logging_config = dict(self._service_config["logging"])
        self._log_path = self._project_root / str(logging_config["log_path"])
        self._include_raw_input = bool(logging_config["include_raw_input"])
        self._include_encoded_features = bool(logging_config["include_encoded_features"])
        self._model_cache: dict[str, LoadedModelArtifact] = {}

    def list_versions(self) -> list[str]:
        return sorted(self._registry)

    def default_version(self) -> str:
        return str(self._service_config["default_model_version"])

    def load_model(self, model_version: str | None = None) -> LoadedModelArtifact:
        resolved_version = model_version or self.default_version()
        if resolved_version in self._model_cache:
            return self._model_cache[resolved_version]
        if resolved_version not in self._registry:
            raise ValueError(
                f"Unknown model version {resolved_version!r}. Available versions: {self.list_versions()}"
            )

        registry_entry = self._registry[resolved_version]
        manifest = self._load_json(registry_entry.manifest_path)
        result = manifest["result"]
        model_path = Path(str(result["model_path"]))
        payload = self._load_pickle(model_path)

        artifact = LoadedModelArtifact(
            model_version=resolved_version,
            model_name=str(result["model_name"]),
            manifest_path=registry_entry.manifest_path,
            model_path=model_path,
            feature_columns=list(payload["feature_columns"]),
            target_column=str(payload["target_column"]),
            target_id_column=str(payload["target_id_column"]),
            target_mapping={str(label): int(label_id) for label, label_id in payload["target_mapping"].items()},
            classifier=payload["classifier"],
            scaler=payload.get("scaler"),
            config=dict(payload["config"]),
        )
        self._model_cache[resolved_version] = artifact
        return artifact

    def predict(
        self,
        raw_input: Mapping[str, Any],
        model_version: str | None = None,
        request_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> PredictionResult:
        resolved_request_id = request_id or str(uuid.uuid4())
        generated_at = datetime.now(timezone.utc).isoformat()
        metadata_payload = dict(metadata or {})

        try:
            if not isinstance(raw_input, Mapping):
                raise ValueError("raw_input must be a mapping of field names to values.")

            artifact = self.load_model(model_version)
            validation_result = self._feature_builder.build_features(raw_input, artifact.feature_columns)
            x = np.asarray(
                [[validation_result.encoded_features[column] for column in artifact.feature_columns]],
                dtype=np.float64,
            )
            transformed_x = x if artifact.scaler is None else artifact.scaler.transform(x)
            probabilities = self._predict_probabilities(artifact, transformed_x)
            id_to_label = {label_id: label for label, label_id in artifact.target_mapping.items()}

            predicted_label_id = int(np.argmax(probabilities))
            predicted_label = id_to_label[predicted_label_id]
            confidence = float(probabilities[predicted_label_id])
            probability_mapping = {
                id_to_label[label_id]: float(probabilities[label_id])
                for label_id in sorted(id_to_label)
            }

            self._validate_probabilities(probability_mapping)

            result = PredictionResult(
                request_id=resolved_request_id,
                generated_at=generated_at,
                model_version=artifact.model_version,
                model_name=artifact.model_name,
                predicted_label=predicted_label,
                predicted_label_id=predicted_label_id,
                confidence=confidence,
                probabilities=probability_mapping,
                validated_raw_input=validation_result.validated_raw_input,
                encoded_features=validation_result.encoded_features,
                required_raw_fields=validation_result.required_raw_fields,
            )
            self._log_inference(
                status="success",
                result=result,
                metadata=metadata_payload,
            )
            return result
        except Exception as exc:
            self._log_failure(
                request_id=resolved_request_id,
                generated_at=generated_at,
                raw_input=raw_input,
                model_version=model_version,
                metadata=metadata_payload,
                error_message=str(exc),
            )
            raise

    def _build_registry(self) -> dict[str, ModelRegistryEntry]:
        registry: dict[str, ModelRegistryEntry] = {}
        registry_config = dict(self._service_config["model_registry"])
        for model_version, item in registry_config.items():
            registry[str(model_version)] = ModelRegistryEntry(
                model_version=str(model_version),
                manifest_path=self._project_root / str(item["manifest_path"]),
                description=str(item["description"]),
            )
        return registry

    def _predict_probabilities(
        self,
        artifact: LoadedModelArtifact,
        transformed_x: np.ndarray,
    ) -> np.ndarray:
        if hasattr(artifact.classifier, "predict_proba"):
            probabilities = np.asarray(artifact.classifier.predict_proba(transformed_x)[0], dtype=np.float64)
        elif hasattr(artifact.classifier, "decision_function"):
            logits = np.asarray(artifact.classifier.decision_function(transformed_x)[0], dtype=np.float64)
            probabilities = self._softmax(logits)
        else:
            raise ValueError(
                f"Loaded model {artifact.model_name} does not support predict_proba or decision_function."
            )

        if probabilities.ndim != 1:
            raise ValueError(f"Expected 1D probability output, but received shape {probabilities.shape}.")
        if probabilities.shape[0] != len(artifact.target_mapping):
            raise ValueError(
                f"Probability output length {probabilities.shape[0]} does not match target mapping size "
                f"{len(artifact.target_mapping)}."
            )
        return probabilities

    def _validate_probabilities(self, probability_mapping: dict[str, float]) -> None:
        total_probability = float(sum(probability_mapping.values()))
        if not math.isfinite(total_probability):
            raise ValueError("Predicted probabilities are not finite.")
        if abs(total_probability - 1.0) > 1e-6:
            raise ValueError(
                f"Predicted probabilities must sum to 1.0, but summed to {total_probability:.12f}."
            )
        for label, probability in probability_mapping.items():
            if probability < 0.0 or probability > 1.0:
                raise ValueError(f"Predicted probability for {label} must be in [0, 1], but was {probability}.")

    def _log_inference(
        self,
        status: str,
        result: PredictionResult,
        metadata: dict[str, Any],
    ) -> None:
        payload = {
            "timestamp_utc": result.generated_at,
            "status": status,
            "request_id": result.request_id,
            "model_version": result.model_version,
            "model_name": result.model_name,
            "prediction": {
                "predicted_label": result.predicted_label,
                "predicted_label_id": result.predicted_label_id,
                "confidence": result.confidence,
                "probabilities": result.probabilities,
            },
            "required_raw_fields": result.required_raw_fields,
            "metadata": metadata,
        }
        if self._include_raw_input:
            payload["validated_raw_input"] = result.validated_raw_input
        if self._include_encoded_features:
            payload["encoded_features"] = result.encoded_features
        self._append_log(payload)

    def _log_failure(
        self,
        request_id: str,
        generated_at: str,
        raw_input: Mapping[str, Any] | Any,
        model_version: str | None,
        metadata: dict[str, Any],
        error_message: str,
    ) -> None:
        payload = {
            "timestamp_utc": generated_at,
            "status": "failure",
            "request_id": request_id,
            "model_version": model_version or self.default_version(),
            "error_message": error_message,
            "metadata": metadata,
        }
        if self._include_raw_input:
            payload["raw_input"] = dict(raw_input) if isinstance(raw_input, Mapping) else raw_input
        self._append_log(payload)

    def _append_log(self, payload: dict[str, Any]) -> None:
        ensure_parent(self._log_path)
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")

    def _load_json(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _load_pickle(self, path: Path) -> object:
        with path.open("rb") as handle:
            return pickle.load(handle)

    def _softmax(self, logits: np.ndarray) -> np.ndarray:
        stabilized = logits - float(np.max(logits))
        exponentiated = np.exp(stabilized)
        return exponentiated / float(np.sum(exponentiated))


def build_model_service(project_root: Path | None = None) -> ModelService:
    return ModelService(project_root=project_root)


def prediction_result_to_dict(result: PredictionResult) -> dict[str, Any]:
    return asdict(result)
