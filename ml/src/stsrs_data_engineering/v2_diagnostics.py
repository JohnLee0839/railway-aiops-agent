from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from sklearn.inspection import permutation_importance

from stsrs_data_engineering.baseline_training import _load_json
from stsrs_data_engineering.config import write_json
from stsrs_data_engineering.v1_diagnostics import (
    LeakagePairMetric,
    PermutationImportanceMetric,
    _compute_leakage_pair_metric,
    _load_pickle,
    _write_leakage_csv,
    _write_permutation_importance_csv,
)


@dataclass
class ClassMetricDelta:
    split_name: str
    label: str
    label_id: int
    v1_f1: float
    v2_f1: float
    f1_delta: float
    v1_precision: float
    v2_precision: float
    precision_delta: float
    v1_recall: float
    v2_recall: float
    recall_delta: float


@dataclass
class V2DiagnosticsResult:
    model_name: str
    model_path: str
    reference_model_name: str
    reference_model_path: str
    validation_sample_rows: int
    permutation_repeats: int
    primary_scoring: str
    permutation_importance_path: str
    leakage_check_path: str
    class_metric_delta_path: str
    top_permutation_importances: list[PermutationImportanceMetric]
    leakage_pair_metrics: list[LeakagePairMetric]
    class_metric_deltas: list[ClassMetricDelta]


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _quote_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _relation_sql(path: Path) -> str:
    return f"read_parquet({_quote_literal(path.resolve().as_posix())})"


def _read_validation_sample_stable(
    connection: duckdb.DuckDBPyConnection,
    validation_path: Path,
    feature_columns: list[str],
    sample_hash_columns: list[str],
    target_id_column: str,
    sample_per_class: int,
):
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
    import numpy as np

    batch = np.asarray(rows, dtype=np.float64)
    return batch[:, :-1], batch[:, -1].astype(np.int64)


def _build_class_metric_deltas(
    v1_manifest_result: dict,
    v2_manifest_result: dict,
) -> list[ClassMetricDelta]:
    deltas: list[ClassMetricDelta] = []
    for split_name in ("validation", "test"):
        v1_split = next(
            evaluation for evaluation in v1_manifest_result["split_evaluations"] if evaluation["split_name"] == split_name
        )
        v2_split = next(
            evaluation for evaluation in v2_manifest_result["split_evaluations"] if evaluation["split_name"] == split_name
        )
        v2_metric_by_label = {metric["label"]: metric for metric in v2_split["class_metrics"]}
        for v1_metric in v1_split["class_metrics"]:
            v2_metric = v2_metric_by_label[v1_metric["label"]]
            deltas.append(
                ClassMetricDelta(
                    split_name=split_name,
                    label=str(v1_metric["label"]),
                    label_id=int(v1_metric["label_id"]),
                    v1_f1=float(v1_metric["f1"]),
                    v2_f1=float(v2_metric["f1"]),
                    f1_delta=float(v2_metric["f1"] - v1_metric["f1"]),
                    v1_precision=float(v1_metric["precision"]),
                    v2_precision=float(v2_metric["precision"]),
                    precision_delta=float(v2_metric["precision"] - v1_metric["precision"]),
                    v1_recall=float(v1_metric["recall"]),
                    v2_recall=float(v2_metric["recall"]),
                    recall_delta=float(v2_metric["recall"] - v1_metric["recall"]),
                )
            )
    return deltas


def _write_class_metric_delta_csv(output_path: Path, metrics: list[ClassMetricDelta]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "split_name",
                "label",
                "label_id",
                "v1_f1",
                "v2_f1",
                "f1_delta",
                "v1_precision",
                "v2_precision",
                "precision_delta",
                "v1_recall",
                "v2_recall",
                "recall_delta",
            ]
        )
        for metric in metrics:
            writer.writerow(
                [
                    metric.split_name,
                    metric.label,
                    metric.label_id,
                    metric.v1_f1,
                    metric.v2_f1,
                    metric.f1_delta,
                    metric.v1_precision,
                    metric.v2_precision,
                    metric.precision_delta,
                    metric.v1_recall,
                    metric.v2_recall,
                    metric.recall_delta,
                ]
            )


def _write_v2_diagnostics_report(
    project_root: Path,
    result: V2DiagnosticsResult,
    generated_at: str,
) -> Path:
    report_path = project_root / "reports" / "model_training" / "v2_diagnostics_report.md"
    lines = [
        "# V2 Diagnostics Report",
        "",
        f"Generated at: {generated_at}",
        "",
        "## Summary",
        "",
        f"- Model name: `{result.model_name}`",
        f"- Model path: `{result.model_path}`",
        f"- Reference model name: `{result.reference_model_name}`",
        f"- Reference model path: `{result.reference_model_path}`",
        f"- Validation sample rows: {result.validation_sample_rows}",
        f"- Permutation repeats: {result.permutation_repeats}",
        f"- Primary scoring: `{result.primary_scoring}`",
        f"- Permutation importance path: `{result.permutation_importance_path}`",
        f"- Leakage check path: `{result.leakage_check_path}`",
        f"- Class metric delta path: `{result.class_metric_delta_path}`",
        "",
        "## Top Permutation Importances",
        "",
        "| Feature | Mean Importance | Std Importance |",
        "| --- | ---: | ---: |",
    ]
    for metric in result.top_permutation_importances:
        lines.append(
            f"| {metric.feature_name} | {metric.mean_importance:.6f} | {metric.std_importance:.6f} |"
        )

    lines.extend(
        [
            "",
            "## Split Leakage Checks",
            "",
            "| Left Split | Right Split | Exact Row Overlap | Feature-only Overlap | Conflicting Feature Overlap |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    for metric in result.leakage_pair_metrics:
        lines.append(
            f"| {metric.left_split} | {metric.right_split} | {metric.exact_row_overlap_count} | "
            f"{metric.feature_only_overlap_count} | {metric.conflicting_feature_overlap_count} |"
        )

    lines.extend(
        [
            "",
            "## V2 vs V1 Class Metric Deltas",
            "",
            "| Split | Label | V1 F1 | V2 F1 | F1 Delta | V1 Precision | V2 Precision | Precision Delta | V1 Recall | V2 Recall | Recall Delta |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for metric in result.class_metric_deltas:
        lines.append(
            f"| {metric.split_name} | {metric.label} | {metric.v1_f1:.6f} | {metric.v2_f1:.6f} | {metric.f1_delta:+.6f} | "
            f"{metric.v1_precision:.6f} | {metric.v2_precision:.6f} | {metric.precision_delta:+.6f} | "
            f"{metric.v1_recall:.6f} | {metric.v2_recall:.6f} | {metric.recall_delta:+.6f} |"
        )

    lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run_v2_diagnostics(project_root: Path | None = None) -> int:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    v2_manifest_path = root / "metadata" / "manifests" / "v2_compact_tree_manifest.json"
    v1_manifest_path = root / "metadata" / "manifests" / "v1_tree_baseline_manifest.json"
    v2_manifest = _load_json(v2_manifest_path)
    v1_manifest = _load_json(v1_manifest_path)
    v2_result = v2_manifest["result"]
    v1_result = v1_manifest["result"]

    model_path = Path(v2_result["model_path"])
    model_payload = _load_pickle(model_path)
    classifier = model_payload["classifier"]
    feature_columns = list(model_payload["feature_columns"])
    sample_hash_columns = list(model_payload["sample_hash_columns"])
    target_id_column = str(model_payload["target_id_column"])

    validation_path = root / "data" / "features" / "encoded" / "validation.parquet"
    split_paths = {
        "train": root / "data" / "features" / "encoded" / "train.parquet",
        "validation": validation_path,
        "test": root / "data" / "features" / "encoded" / "test.parquet",
    }

    sample_per_class = 25000
    permutation_repeats = 5
    primary_scoring = "f1_macro"
    generated_at = datetime.now(timezone.utc).isoformat()

    connection = duckdb.connect(database=":memory:")
    try:
        x_validation, y_validation = _read_validation_sample_stable(
            connection=connection,
            validation_path=validation_path,
            feature_columns=feature_columns,
            sample_hash_columns=sample_hash_columns,
            target_id_column=target_id_column,
            sample_per_class=sample_per_class,
        )
        permutation = permutation_importance(
            classifier,
            x_validation,
            y_validation,
            scoring=primary_scoring,
            n_repeats=permutation_repeats,
            random_state=42,
            n_jobs=1,
        )
        permutation_metrics = [
            PermutationImportanceMetric(
                feature_name=feature_name,
                mean_importance=float(permutation.importances_mean[index]),
                std_importance=float(permutation.importances_std[index]),
            )
            for index, feature_name in enumerate(feature_columns)
        ]
        permutation_metrics.sort(key=lambda item: item.mean_importance, reverse=True)

        leakage_pair_metrics = [
            _compute_leakage_pair_metric(
                connection=connection,
                left_split=left_split,
                right_split=right_split,
                left_path=split_paths[left_split],
                right_path=split_paths[right_split],
                feature_columns=feature_columns,
                target_id_column=target_id_column,
            )
            for left_split, right_split in (("train", "validation"), ("train", "test"), ("validation", "test"))
        ]
    finally:
        connection.close()

    class_metric_deltas = _build_class_metric_deltas(v1_result, v2_result)

    permutation_path = root / "reports" / "model_training" / "v2_permutation_importance.csv"
    leakage_path = root / "reports" / "model_training" / "v2_split_leakage_checks.csv"
    class_delta_path = root / "reports" / "model_training" / "v2_vs_v1_class_metric_deltas.csv"
    _write_permutation_importance_csv(permutation_path, permutation_metrics)
    _write_leakage_csv(leakage_path, leakage_pair_metrics)
    _write_class_metric_delta_csv(class_delta_path, class_metric_deltas)

    result = V2DiagnosticsResult(
        model_name=str(v2_result["model_name"]),
        model_path=str(model_path),
        reference_model_name=str(v1_result["model_name"]),
        reference_model_path=str(v1_result["model_path"]),
        validation_sample_rows=int(y_validation.shape[0]),
        permutation_repeats=permutation_repeats,
        primary_scoring=primary_scoring,
        permutation_importance_path=str(permutation_path),
        leakage_check_path=str(leakage_path),
        class_metric_delta_path=str(class_delta_path),
        top_permutation_importances=permutation_metrics,
        leakage_pair_metrics=leakage_pair_metrics,
        class_metric_deltas=class_metric_deltas,
    )

    report_path = _write_v2_diagnostics_report(root, result, generated_at)
    manifest_path = root / "metadata" / "manifests" / "v2_diagnostics_manifest.json"
    write_json(
        manifest_path,
        {
            "generated_at": generated_at,
            "report_path": str(report_path),
            "result": {
                **{
                    key: value
                    for key, value in asdict(result).items()
                    if key not in {"top_permutation_importances", "leakage_pair_metrics", "class_metric_deltas"}
                },
                "top_permutation_importances": [asdict(metric) for metric in result.top_permutation_importances],
                "leakage_pair_metrics": [asdict(metric) for metric in result.leakage_pair_metrics],
                "class_metric_deltas": [asdict(metric) for metric in result.class_metric_deltas],
            },
        },
    )
    return 0
