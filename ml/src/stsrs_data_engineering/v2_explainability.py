from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import shap

from stsrs_data_engineering.baseline_training import _load_json, _relation_sql
from stsrs_data_engineering.config import ensure_parent, load_yaml_file, write_json
from stsrs_data_engineering.v1_diagnostics import _load_pickle


@dataclass
class ShapGlobalMetric:
    feature_name: str
    mean_abs_shap: float


@dataclass
class ShapPerClassMetric:
    class_label: str
    class_id: int
    feature_name: str
    mean_abs_shap: float


@dataclass
class ShapBaseValueMetric:
    class_label: str
    class_id: int
    base_value: float


@dataclass
class ShapLocalContribution:
    sample_name: str
    true_label: str
    true_label_id: int
    predicted_label: str
    predicted_label_id: int
    true_class_probability: float
    predicted_class_probability: float
    feature_name: str
    feature_value: float
    shap_true_class: float
    shap_predicted_class: float


@dataclass
class V2ExplainabilityResult:
    experiment_name: str
    model_name: str
    model_path: str
    config_path: str
    reference_model_manifest_path: str
    validation_sample_rows: int
    sample_per_class: int
    local_examples_per_class: int
    global_importance_path: str
    per_class_importance_path: str
    base_value_path: str
    local_contribution_path: str
    top_global_features: list[ShapGlobalMetric]
    base_values: list[ShapBaseValueMetric]


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _load_validation_sample(
    connection: duckdb.DuckDBPyConnection,
    validation_path: Path,
    feature_columns: list[str],
    sample_hash_columns: list[str],
    target_id_column: str,
    sample_per_class: int,
) -> tuple[np.ndarray, np.ndarray]:
    projected = ", ".join([*(_quote_identifier(column) for column in feature_columns), _quote_identifier(target_id_column)])
    stable_order_columns = [*sample_hash_columns, target_id_column]
    hash_inputs = ", ".join(_quote_identifier(column) for column in stable_order_columns)
    stable_order_projection = ", ".join(_quote_identifier(column) for column in sample_hash_columns)
    rows = connection.execute(
        f"""
WITH ranked AS (
    SELECT
        {projected},
        ROW_NUMBER() OVER (
            PARTITION BY {_quote_identifier(target_id_column)}
            ORDER BY hash({hash_inputs}), {stable_order_projection}
        ) AS sampled_rank
    FROM {_relation_sql(validation_path)}
)
SELECT {projected}
FROM ranked
WHERE sampled_rank <= {sample_per_class}
"""
    ).fetchall()
    if not rows:
        raise ValueError("V2 explainability sample query returned no rows.")
    batch = np.asarray(rows, dtype=np.float64)
    return batch[:, :-1], batch[:, -1].astype(np.int64)


def _coerce_shap_values(explainer: shap.TreeExplainer, x_sample: np.ndarray, check_additivity: bool) -> tuple[np.ndarray, np.ndarray]:
    try:
        explanation = explainer(x_sample, check_additivity=check_additivity)
        raw_values = explanation.values
        raw_base_values = explanation.base_values
    except TypeError:
        raw_values = explainer.shap_values(x_sample, check_additivity=check_additivity)
        raw_base_values = explainer.expected_value

    if isinstance(raw_values, list):
        shap_values = np.stack(raw_values, axis=-1)
    else:
        shap_values = np.asarray(raw_values, dtype=np.float64)

    if shap_values.ndim == 2:
        shap_values = shap_values[:, :, np.newaxis]
    elif shap_values.ndim == 3:
        if shap_values.shape[0] == x_sample.shape[0] and shap_values.shape[1] == x_sample.shape[1]:
            pass
        elif shap_values.shape[1] == x_sample.shape[0] and shap_values.shape[2] == x_sample.shape[1]:
            shap_values = np.moveaxis(shap_values, 0, -1)
        else:
            raise ValueError(f"Unexpected SHAP value shape: {shap_values.shape}")
    else:
        raise ValueError(f"Unsupported SHAP value shape: {shap_values.shape}")

    base_values = np.asarray(raw_base_values, dtype=np.float64)
    if base_values.ndim == 0:
        base_values = base_values.reshape(1)
    if base_values.ndim == 2:
        if base_values.shape[0] == x_sample.shape[0]:
            base_values = base_values.mean(axis=0)
        else:
            base_values = base_values.reshape(-1)
    return shap_values, base_values


def _write_global_importance_csv(path: Path, metrics: list[ShapGlobalMetric]) -> None:
    ensure_parent(path)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["feature_name", "mean_abs_shap"])
        for metric in metrics:
            writer.writerow([metric.feature_name, metric.mean_abs_shap])


def _write_per_class_importance_csv(path: Path, metrics: list[ShapPerClassMetric]) -> None:
    ensure_parent(path)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["class_label", "class_id", "feature_name", "mean_abs_shap"])
        for metric in metrics:
            writer.writerow([metric.class_label, metric.class_id, metric.feature_name, metric.mean_abs_shap])


def _write_base_value_csv(path: Path, metrics: list[ShapBaseValueMetric]) -> None:
    ensure_parent(path)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["class_label", "class_id", "base_value"])
        for metric in metrics:
            writer.writerow([metric.class_label, metric.class_id, metric.base_value])


def _write_local_contribution_csv(path: Path, rows: list[ShapLocalContribution]) -> None:
    ensure_parent(path)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "sample_name",
                "true_label",
                "true_label_id",
                "predicted_label",
                "predicted_label_id",
                "true_class_probability",
                "predicted_class_probability",
                "feature_name",
                "feature_value",
                "shap_true_class",
                "shap_predicted_class",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.sample_name,
                    row.true_label,
                    row.true_label_id,
                    row.predicted_label,
                    row.predicted_label_id,
                    row.true_class_probability,
                    row.predicted_class_probability,
                    row.feature_name,
                    row.feature_value,
                    row.shap_true_class,
                    row.shap_predicted_class,
                ]
            )


def _write_v2_explainability_report(
    project_root: Path,
    result: V2ExplainabilityResult,
    generated_at: str,
) -> Path:
    report_path = project_root / "reports" / "model_training" / "v2_explainability_report.md"
    ensure_parent(report_path)
    lines = [
        "# V2 Explainability Report",
        "",
        f"Generated at: {generated_at}",
        "",
        "## Summary",
        "",
        f"- Experiment name: `{result.experiment_name}`",
        f"- Model name: `{result.model_name}`",
        f"- Model path: `{result.model_path}`",
        f"- Config path: `{result.config_path}`",
        f"- Reference model manifest path: `{result.reference_model_manifest_path}`",
        f"- Validation sample rows: {result.validation_sample_rows}",
        f"- Sample per class: {result.sample_per_class}",
        f"- Local examples per class: {result.local_examples_per_class}",
        f"- Global importance path: `{result.global_importance_path}`",
        f"- Per-class importance path: `{result.per_class_importance_path}`",
        f"- Base value path: `{result.base_value_path}`",
        f"- Local contribution path: `{result.local_contribution_path}`",
        "",
        "## Global Mean Absolute SHAP",
        "",
        "| Feature | Mean |SHAP| |",
        "| --- | ---: |",
    ]
    for metric in result.top_global_features:
        lines.append(f"| {metric.feature_name} | {metric.mean_abs_shap:.6f} |")

    lines.extend(
        [
            "",
            "## Base Values",
            "",
            "| Class | Class ID | Base Value |",
            "| --- | ---: | ---: |",
        ]
    )
    for metric in result.base_values:
        lines.append(f"| {metric.class_label} | {metric.class_id} | {metric.base_value:.6f} |")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run_v2_explainability(project_root: Path | None = None) -> int:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    config_path = root / "configs" / "modeling" / "v2_explainability.yaml"
    config = load_yaml_file(config_path)
    model_manifest_path = root / str(config["reference_model_manifest"])
    model_manifest = _load_json(model_manifest_path)
    model_result = model_manifest["result"]

    model_path = Path(model_result["model_path"])
    model_payload = _load_pickle(model_path)
    classifier = model_payload["classifier"]
    feature_columns = list(model_payload["feature_columns"])
    sample_hash_columns = list(model_payload["sample_hash_columns"])
    target_id_column = str(model_payload["target_id_column"])
    target_mapping = {str(label): int(label_id) for label, label_id in model_payload["target_mapping"].items()}
    id_to_label = {label_id: label for label, label_id in target_mapping.items()}

    sample_per_class = int(config["explainability"]["sample_per_class"])
    local_examples_per_class = int(config["explainability"]["local_examples_per_class"])
    check_additivity = bool(config["explainability"]["check_additivity"])

    validation_path = root / "data" / "features" / "encoded" / "validation.parquet"
    generated_at = datetime.now(timezone.utc).isoformat()

    connection = duckdb.connect(database=":memory:")
    try:
        x_sample, y_sample = _load_validation_sample(
            connection=connection,
            validation_path=validation_path,
            feature_columns=feature_columns,
            sample_hash_columns=sample_hash_columns,
            target_id_column=target_id_column,
            sample_per_class=sample_per_class,
        )
    finally:
        connection.close()

    explainer = shap.TreeExplainer(classifier)
    shap_values, base_values = _coerce_shap_values(explainer, x_sample, check_additivity)
    y_pred = classifier.predict(x_sample)
    y_proba = classifier.predict_proba(x_sample)

    global_scores = np.mean(np.abs(shap_values), axis=(0, 2))
    global_metrics = [
        ShapGlobalMetric(feature_name=feature_name, mean_abs_shap=float(global_scores[index]))
        for index, feature_name in enumerate(feature_columns)
    ]
    global_metrics.sort(key=lambda item: item.mean_abs_shap, reverse=True)

    per_class_metrics: list[ShapPerClassMetric] = []
    for class_id in sorted(id_to_label):
        class_scores = np.mean(np.abs(shap_values[:, :, class_id]), axis=0)
        for feature_index, feature_name in enumerate(feature_columns):
            per_class_metrics.append(
                ShapPerClassMetric(
                    class_label=id_to_label[class_id],
                    class_id=class_id,
                    feature_name=feature_name,
                    mean_abs_shap=float(class_scores[feature_index]),
                )
            )

    normalized_base_values = base_values.reshape(-1)
    if normalized_base_values.shape[0] != len(id_to_label):
        normalized_base_values = normalized_base_values[: len(id_to_label)]
    base_value_metrics = [
        ShapBaseValueMetric(
            class_label=id_to_label[class_id],
            class_id=class_id,
            base_value=float(normalized_base_values[class_id]),
        )
        for class_id in sorted(id_to_label)
    ]

    local_rows: list[ShapLocalContribution] = []
    examples_taken_by_class = {class_id: 0 for class_id in id_to_label}
    for row_index, true_class_id in enumerate(y_sample.tolist()):
        if examples_taken_by_class[true_class_id] >= local_examples_per_class:
            continue
        predicted_class_id = int(y_pred[row_index])
        sample_name = f"validation_true_{id_to_label[true_class_id]}_{examples_taken_by_class[true_class_id] + 1}"
        for feature_index, feature_name in enumerate(feature_columns):
            local_rows.append(
                ShapLocalContribution(
                    sample_name=sample_name,
                    true_label=id_to_label[true_class_id],
                    true_label_id=true_class_id,
                    predicted_label=id_to_label[predicted_class_id],
                    predicted_label_id=predicted_class_id,
                    true_class_probability=float(y_proba[row_index, true_class_id]),
                    predicted_class_probability=float(y_proba[row_index, predicted_class_id]),
                    feature_name=feature_name,
                    feature_value=float(x_sample[row_index, feature_index]),
                    shap_true_class=float(shap_values[row_index, feature_index, true_class_id]),
                    shap_predicted_class=float(shap_values[row_index, feature_index, predicted_class_id]),
                )
            )
        examples_taken_by_class[true_class_id] += 1
        if all(count >= local_examples_per_class for count in examples_taken_by_class.values()):
            break

    report_dir = root / "reports" / "model_training"
    global_path = report_dir / "v2_shap_global_importance.csv"
    per_class_path = report_dir / "v2_shap_per_class_importance.csv"
    base_value_path = report_dir / "v2_shap_base_values.csv"
    local_path = report_dir / "v2_shap_local_contributions.csv"
    _write_global_importance_csv(global_path, global_metrics)
    _write_per_class_importance_csv(per_class_path, per_class_metrics)
    _write_base_value_csv(base_value_path, base_value_metrics)
    _write_local_contribution_csv(local_path, local_rows)

    result = V2ExplainabilityResult(
        experiment_name=str(config["experiment_name"]),
        model_name=str(model_result["model_name"]),
        model_path=str(model_path),
        config_path=str(config_path),
        reference_model_manifest_path=str(model_manifest_path),
        validation_sample_rows=int(y_sample.shape[0]),
        sample_per_class=sample_per_class,
        local_examples_per_class=local_examples_per_class,
        global_importance_path=str(global_path),
        per_class_importance_path=str(per_class_path),
        base_value_path=str(base_value_path),
        local_contribution_path=str(local_path),
        top_global_features=global_metrics,
        base_values=base_value_metrics,
    )

    report_path = _write_v2_explainability_report(root, result, generated_at)
    manifest_path = root / "metadata" / "manifests" / "v2_explainability_manifest.json"
    write_json(
        manifest_path,
        {
            "generated_at": generated_at,
            "report_path": str(report_path),
            "result": {
                **{
                    key: value
                    for key, value in asdict(result).items()
                    if key not in {"top_global_features", "base_values"}
                },
                "top_global_features": [asdict(metric) for metric in result.top_global_features],
                "base_values": [asdict(metric) for metric in result.base_values],
            },
        },
    )
    return 0
