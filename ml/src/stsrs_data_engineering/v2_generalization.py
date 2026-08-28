from __future__ import annotations

import csv
import pickle
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from stsrs_data_engineering.baseline_training import (
    _compute_metrics_from_confusion_matrix,
    _load_json,
    _relation_sql,
)
from stsrs_data_engineering.config import ensure_parent, load_yaml_file, write_json
from stsrs_data_engineering.v1_ablation import _build_hist_gradient_boosting_classifier, _evaluate_split
from stsrs_data_engineering.v1_diagnostics import _load_pickle


@dataclass
class TemporalBucketMetric:
    split_name: str
    bucket_id: int
    row_count: int
    min_timestamp: str
    max_timestamp: str
    observed_class_count: int
    label_distribution: dict[str, int]
    accuracy: float
    balanced_accuracy: float
    macro_precision: float
    macro_recall: float
    macro_f1: float
    weighted_f1: float


@dataclass
class SeedStabilityRun:
    run_name: str
    model_random_seed: int
    sample_salt: int
    sampled_training_rows: int
    validation_macro_f1: float
    test_macro_f1: float
    validation_macro_f1_delta_vs_v2: float
    test_macro_f1_delta_vs_v2: float


@dataclass
class SummaryMetric:
    metric_name: str
    mean_value: float
    std_value: float
    min_value: float
    max_value: float


@dataclass
class V2GeneralizationResult:
    experiment_name: str
    model_name: str
    model_path: str
    config_path: str
    reference_model_manifest_path: str
    temporal_bucket_metric_path: str
    seed_stability_path: str
    temporal_bucket_metrics: list[TemporalBucketMetric]
    seed_stability_runs: list[SeedStabilityRun]
    seed_summary_metrics: list[SummaryMetric]


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _quote_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _build_salted_sample_query(
    parquet_path: Path,
    feature_columns: list[str],
    sample_hash_columns: list[str],
    target_id_column: str,
    sample_per_class: int,
    sample_salt: int,
) -> str:
    projected_columns = ", ".join(
        [*(_quote_identifier(column) for column in feature_columns), _quote_identifier(target_id_column)]
    )
    stable_order_columns = [*sample_hash_columns, target_id_column]
    hash_inputs = ", ".join(_quote_identifier(column) for column in stable_order_columns)
    stable_order_projection = ", ".join(_quote_identifier(column) for column in sample_hash_columns)
    relation_sql = _relation_sql(parquet_path)
    return f"""
WITH ranked AS (
    SELECT
        {projected_columns},
        ROW_NUMBER() OVER (
            PARTITION BY {_quote_identifier(target_id_column)}
            ORDER BY hash({hash_inputs}, {sample_salt}), {stable_order_projection}
        ) AS sampled_rank
    FROM {relation_sql}
)
SELECT
    {projected_columns}
FROM ranked
WHERE sampled_rank <= {sample_per_class}
"""


def _load_salted_training_sample(
    connection: duckdb.DuckDBPyConnection,
    train_path: Path,
    feature_columns: list[str],
    sample_hash_columns: list[str],
    target_id_column: str,
    sample_per_class: int,
    sample_salt: int,
) -> tuple[np.ndarray, np.ndarray]:
    rows = connection.execute(
        _build_salted_sample_query(
            parquet_path=train_path,
            feature_columns=feature_columns,
            sample_hash_columns=sample_hash_columns,
            target_id_column=target_id_column,
            sample_per_class=sample_per_class,
            sample_salt=sample_salt,
        )
    ).fetchall()
    if not rows:
        raise ValueError("Salted training sample query returned no rows.")
    batch = np.asarray(rows, dtype=np.float64)
    return batch[:, :-1], batch[:, -1].astype(np.int64)


def _build_target_id_case_expr(target_column: str, target_mapping: dict[str, int]) -> str:
    when_clauses = [
        f"WHEN {_quote_identifier(target_column)} = {_quote_literal(label)} THEN {label_id}"
        for label, label_id in target_mapping.items()
    ]
    return "CASE " + " ".join(when_clauses) + " ELSE NULL END"


def _compute_temporal_bucket_metrics(
    connection: duckdb.DuckDBPyConnection,
    split_name: str,
    split_path: Path,
    classifier: Any,
    feature_columns: list[str],
    target_column: str,
    target_mapping: dict[str, int],
    num_buckets: int,
) -> list[TemporalBucketMetric]:
    id_to_label = {label_id: label for label, label_id in target_mapping.items()}
    target_case_expr = _build_target_id_case_expr(target_column, target_mapping)
    relation_sql = _relation_sql(split_path)
    order_clause = ', '.join(_quote_identifier(column) for column in ("Timestamp", "TrainID", "SignalID"))
    bucketed_sql = f"""
WITH bucketed AS (
    SELECT
        {_quote_identifier('Timestamp')} AS Timestamp,
        {", ".join(_quote_identifier(column) for column in feature_columns)},
        {target_case_expr} AS AttackLabelId,
        NTILE({num_buckets}) OVER (ORDER BY {order_clause}) AS bucket_id
    FROM {relation_sql}
)
SELECT
    bucket_id,
    MIN(Timestamp) AS min_timestamp,
    MAX(Timestamp) AS max_timestamp,
    COUNT(*) AS row_count
FROM bucketed
GROUP BY bucket_id
ORDER BY bucket_id
"""
    bucket_rows = connection.execute(bucketed_sql).fetchall()

    metrics: list[TemporalBucketMetric] = []
    for bucket_id, min_timestamp, max_timestamp, row_count in bucket_rows:
        select_sql = f"""
WITH bucketed AS (
    SELECT
        {", ".join(_quote_identifier(column) for column in feature_columns)},
        {target_case_expr} AS AttackLabelId,
        NTILE({num_buckets}) OVER (ORDER BY {order_clause}) AS bucket_id
    FROM {relation_sql}
)
SELECT
    {", ".join(_quote_identifier(column) for column in feature_columns)},
    AttackLabelId
FROM bucketed
WHERE bucket_id = {int(bucket_id)}
"""
        rows = connection.execute(select_sql).fetchall()
        batch = np.asarray(rows, dtype=np.float64)
        x_bucket = batch[:, :-1]
        y_bucket = batch[:, -1].astype(np.int64)
        y_pred = classifier.predict(x_bucket)

        num_classes = len(target_mapping)
        confusion_matrix = np.zeros((num_classes, num_classes), dtype=np.int64)
        np.add.at(confusion_matrix, (y_bucket, y_pred), 1)
        support = confusion_matrix.sum(axis=1).astype(np.int64)
        observed_mask = support > 0
        observed_class_count = int(observed_mask.sum())
        (
            accuracy,
            balanced_accuracy,
            macro_precision,
            macro_recall,
            macro_f1,
            weighted_f1,
            _class_metrics,
        ) = _compute_metrics_from_confusion_matrix(confusion_matrix, target_mapping)
        if observed_class_count > 0:
            predicted = confusion_matrix.sum(axis=0).astype(np.int64)
            true_positive = np.diag(confusion_matrix).astype(np.int64)
            precision = np.divide(
                true_positive,
                predicted,
                out=np.zeros_like(true_positive, dtype=np.float64),
                where=predicted != 0,
            )
            recall = np.divide(
                true_positive,
                support,
                out=np.zeros_like(true_positive, dtype=np.float64),
                where=support != 0,
            )
            f1 = np.divide(
                2 * precision * recall,
                precision + recall,
                out=np.zeros_like(precision, dtype=np.float64),
                where=(precision + recall) != 0,
            )
            balanced_accuracy = float(recall[observed_mask].mean())
            macro_precision = float(precision[observed_mask].mean())
            macro_recall = float(recall[observed_mask].mean())
            macro_f1 = float(f1[observed_mask].mean())
        label_distribution = {
            id_to_label[class_id]: int(support[class_id])
            for class_id in sorted(id_to_label)
            if int(support[class_id]) > 0
        }

        metrics.append(
            TemporalBucketMetric(
                split_name=split_name,
                bucket_id=int(bucket_id),
                row_count=int(row_count),
                min_timestamp=str(min_timestamp),
                max_timestamp=str(max_timestamp),
                observed_class_count=observed_class_count,
                label_distribution=label_distribution,
                accuracy=accuracy,
                balanced_accuracy=balanced_accuracy,
                macro_precision=macro_precision,
                macro_recall=macro_recall,
                macro_f1=macro_f1,
                weighted_f1=weighted_f1,
            )
        )
    return metrics


def _write_temporal_bucket_csv(path: Path, metrics: list[TemporalBucketMetric]) -> None:
    ensure_parent(path)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "split_name",
                "bucket_id",
                "row_count",
                "min_timestamp",
                "max_timestamp",
                "observed_class_count",
                "label_distribution",
                "accuracy",
                "balanced_accuracy",
                "macro_precision",
                "macro_recall",
                "macro_f1",
                "weighted_f1",
            ]
        )
        for metric in metrics:
            writer.writerow(
                [
                    metric.split_name,
                    metric.bucket_id,
                    metric.row_count,
                    metric.min_timestamp,
                    metric.max_timestamp,
                    metric.observed_class_count,
                    str(metric.label_distribution),
                    metric.accuracy,
                    metric.balanced_accuracy,
                    metric.macro_precision,
                    metric.macro_recall,
                    metric.macro_f1,
                    metric.weighted_f1,
                ]
            )


def _write_seed_stability_csv(path: Path, rows: list[SeedStabilityRun]) -> None:
    ensure_parent(path)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "run_name",
                "model_random_seed",
                "sample_salt",
                "sampled_training_rows",
                "validation_macro_f1",
                "test_macro_f1",
                "validation_macro_f1_delta_vs_v2",
                "test_macro_f1_delta_vs_v2",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.run_name,
                    row.model_random_seed,
                    row.sample_salt,
                    row.sampled_training_rows,
                    row.validation_macro_f1,
                    row.test_macro_f1,
                    row.validation_macro_f1_delta_vs_v2,
                    row.test_macro_f1_delta_vs_v2,
                ]
            )


def _summarize_seed_metrics(rows: list[SeedStabilityRun]) -> list[SummaryMetric]:
    summary_specs = {
        "validation_macro_f1": np.asarray([row.validation_macro_f1 for row in rows], dtype=np.float64),
        "test_macro_f1": np.asarray([row.test_macro_f1 for row in rows], dtype=np.float64),
    }
    return [
        SummaryMetric(
            metric_name=metric_name,
            mean_value=float(values.mean()),
            std_value=float(values.std()),
            min_value=float(values.min()),
            max_value=float(values.max()),
        )
        for metric_name, values in summary_specs.items()
    ]


def _write_v2_generalization_report(
    project_root: Path,
    result: V2GeneralizationResult,
    generated_at: str,
) -> Path:
    report_path = project_root / "reports" / "model_training" / "v2_generalization_validation_report.md"
    ensure_parent(report_path)
    lines = [
        "# V2 Generalization Validation Report",
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
        f"- Temporal bucket metric path: `{result.temporal_bucket_metric_path}`",
        f"- Seed stability path: `{result.seed_stability_path}`",
        "",
        "## Seed Stability Summary",
        "",
        "| Metric | Mean | Std | Min | Max |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for metric in result.seed_summary_metrics:
        lines.append(
            f"| {metric.metric_name} | {metric.mean_value:.6f} | {metric.std_value:.6f} | {metric.min_value:.6f} | {metric.max_value:.6f} |"
        )

    lines.extend(
        [
            "",
            "## Temporal Bucket Metrics",
            "",
            "| Split | Bucket | Row Count | Observed Classes | Min Timestamp | Max Timestamp | Macro F1 | Balanced Accuracy | Label Distribution |",
            "| --- | ---: | ---: | ---: | --- | --- | ---: | ---: | --- |",
        ]
    )
    for metric in result.temporal_bucket_metrics:
        lines.append(
            f"| {metric.split_name} | {metric.bucket_id} | {metric.row_count} | {metric.observed_class_count} | "
            f"{metric.min_timestamp} | {metric.max_timestamp} | {metric.macro_f1:.6f} | {metric.balanced_accuracy:.6f} | "
            f"`{metric.label_distribution}` |"
        )

    lines.extend(
        [
            "",
            "## Seed Stability Runs",
            "",
            "| Run | Model Seed | Sample Salt | Validation Macro F1 | Delta vs V2 | Test Macro F1 | Delta vs V2 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in result.seed_stability_runs:
        lines.append(
            f"| {row.run_name} | {row.model_random_seed} | {row.sample_salt} | {row.validation_macro_f1:.6f} | "
            f"{row.validation_macro_f1_delta_vs_v2:+.6f} | {row.test_macro_f1:.6f} | {row.test_macro_f1_delta_vs_v2:+.6f} |"
        )
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run_v2_generalization(project_root: Path | None = None) -> int:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    config_path = root / "configs" / "modeling" / "v2_generalization_validation.yaml"
    config = load_yaml_file(config_path)
    model_manifest_path = root / str(config["reference_model_manifest"])
    training_config_path = root / str(config["reference_training_config"])

    model_manifest = _load_json(model_manifest_path)
    model_result = model_manifest["result"]
    training_config = load_yaml_file(training_config_path)

    model_path = Path(model_result["model_path"])
    model_payload = _load_pickle(model_path)
    reference_classifier = model_payload["classifier"]
    feature_columns = list(model_payload["feature_columns"])
    sample_hash_columns = list(model_payload["sample_hash_columns"])
    target_column = str(model_payload["target_column"])
    target_id_column = str(model_payload["target_id_column"])
    target_mapping = {str(label): int(label_id) for label, label_id in model_payload["target_mapping"].items()}

    official_validation_macro_f1 = next(
        split["macro_f1"] for split in model_result["split_evaluations"] if split["split_name"] == "validation"
    )
    official_test_macro_f1 = next(
        split["macro_f1"] for split in model_result["split_evaluations"] if split["split_name"] == "test"
    )

    temporal_split_names = [str(name) for name in config["temporal_stability"]["split_names"]]
    num_buckets = int(config["temporal_stability"]["num_buckets"])
    sample_per_class = int(config["seed_stability"]["sample_per_class"])
    model_random_seeds = [int(value) for value in config["seed_stability"]["model_random_seeds"]]
    sample_salts = [int(value) for value in config["seed_stability"]["sample_salts"]]
    if len(model_random_seeds) != len(sample_salts):
        raise ValueError("model_random_seeds and sample_salts must have the same length.")

    train_path = root / "data" / "features" / "encoded" / "train.parquet"
    validation_path = root / "data" / "features" / "encoded" / "validation.parquet"
    test_path = root / "data" / "features" / "encoded" / "test.parquet"
    split_paths = {
        "validation": validation_path,
        "test": test_path,
    }
    serving_split_paths = {
        "validation": root / "data" / "serving" / "validation" / "validation.parquet",
        "test": root / "data" / "serving" / "test" / "test.parquet",
    }

    generated_at = datetime.now(timezone.utc).isoformat()

    connection = duckdb.connect(database=":memory:")
    try:
        temporal_bucket_metrics: list[TemporalBucketMetric] = []
        for split_name in temporal_split_names:
            temporal_bucket_metrics.extend(
                _compute_temporal_bucket_metrics(
                    connection=connection,
                    split_name=split_name,
                    split_path=serving_split_paths[split_name],
                    classifier=reference_classifier,
                    feature_columns=feature_columns,
                    target_column=target_column,
                    target_mapping=target_mapping,
                    num_buckets=num_buckets,
                )
            )

        seed_stability_runs: list[SeedStabilityRun] = []
        for model_random_seed, sample_salt in zip(model_random_seeds, sample_salts, strict=True):
            classifier = _build_hist_gradient_boosting_classifier(training_config["model"], model_random_seed)
            x_train, y_train = _load_salted_training_sample(
                connection=connection,
                train_path=train_path,
                feature_columns=feature_columns,
                sample_hash_columns=sample_hash_columns,
                target_id_column=target_id_column,
                sample_per_class=sample_per_class,
                sample_salt=sample_salt,
            )
            classifier.fit(x_train, y_train)
            sampled_training_rows = int(y_train.shape[0])

            split_evaluations = [
                _evaluate_split(
                    connection=connection,
                    split_name=split_name,
                    split_path=split_path,
                    feature_columns=feature_columns,
                    target_id_column=target_id_column,
                    classifier=classifier,
                    target_mapping=target_mapping,
                    confusion_matrix_path=root
                    / "reports"
                    / "model_training"
                    / f"scratch_v2_generalization_{model_random_seed}_{split_name}_confusion_matrix.csv",
                    batch_size=int(training_config["training"]["batch_size"]),
                )
                for split_name, split_path in (("validation", validation_path), ("test", test_path))
            ]

            validation_macro_f1 = next(
                evaluation.macro_f1 for evaluation in split_evaluations if evaluation.split_name == "validation"
            )
            test_macro_f1 = next(
                evaluation.macro_f1 for evaluation in split_evaluations if evaluation.split_name == "test"
            )

            seed_stability_runs.append(
                SeedStabilityRun(
                    run_name=f"seed_{model_random_seed}_salt_{sample_salt}",
                    model_random_seed=model_random_seed,
                    sample_salt=sample_salt,
                    sampled_training_rows=sampled_training_rows,
                    validation_macro_f1=float(validation_macro_f1),
                    test_macro_f1=float(test_macro_f1),
                    validation_macro_f1_delta_vs_v2=float(validation_macro_f1 - official_validation_macro_f1),
                    test_macro_f1_delta_vs_v2=float(test_macro_f1 - official_test_macro_f1),
                )
            )
    finally:
        connection.close()

    temporal_bucket_path = root / "reports" / "model_training" / "v2_temporal_bucket_metrics.csv"
    seed_stability_path = root / "reports" / "model_training" / "v2_seed_stability.csv"
    _write_temporal_bucket_csv(temporal_bucket_path, temporal_bucket_metrics)
    _write_seed_stability_csv(seed_stability_path, seed_stability_runs)
    seed_summary_metrics = _summarize_seed_metrics(seed_stability_runs)

    result = V2GeneralizationResult(
        experiment_name=str(config["experiment_name"]),
        model_name=str(model_result["model_name"]),
        model_path=str(model_path),
        config_path=str(config_path),
        reference_model_manifest_path=str(model_manifest_path),
        temporal_bucket_metric_path=str(temporal_bucket_path),
        seed_stability_path=str(seed_stability_path),
        temporal_bucket_metrics=temporal_bucket_metrics,
        seed_stability_runs=seed_stability_runs,
        seed_summary_metrics=seed_summary_metrics,
    )

    report_path = _write_v2_generalization_report(root, result, generated_at)
    manifest_path = root / "metadata" / "manifests" / "v2_generalization_manifest.json"
    write_json(
        manifest_path,
        {
            "generated_at": generated_at,
            "report_path": str(report_path),
            "result": {
                **{
                    key: value
                    for key, value in asdict(result).items()
                    if key not in {"temporal_bucket_metrics", "seed_stability_runs", "seed_summary_metrics"}
                },
                "temporal_bucket_metrics": [asdict(metric) for metric in result.temporal_bucket_metrics],
                "seed_stability_runs": [asdict(metric) for metric in result.seed_stability_runs],
                "seed_summary_metrics": [asdict(metric) for metric in result.seed_summary_metrics],
            },
        },
    )
    return 0
