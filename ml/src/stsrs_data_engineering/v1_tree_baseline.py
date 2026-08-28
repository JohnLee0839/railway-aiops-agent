from __future__ import annotations

import json
import pickle
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from stsrs_data_engineering.baseline_training import (
    ClassMetric,
    SplitEvaluation,
    _compute_metrics_from_confusion_matrix,
    _iterate_batches,
    _load_json,
    _relation_sql,
    _write_confusion_matrix_csv,
)
from stsrs_data_engineering.config import ensure_parent, load_yaml_file, write_json


@dataclass
class V1TreeBaselineResult:
    model_name: str
    model_path: str
    config_path: str
    parent_experiment: str
    feature_manifest_path: str
    encoded_feature_manifest_path: str
    feature_columns: list[str]
    target_column: str
    target_id_column: str
    target_mapping: dict[str, int]
    full_training_rows: int
    sampled_training_rows: int
    sample_per_class: int
    primary_metric_name: str
    split_evaluations: list[SplitEvaluation]


def _build_sample_query(
    parquet_path: Path,
    feature_columns: list[str],
    target_id_column: str,
    sample_per_class: int,
) -> str:
    projected_columns = ", ".join(
        [*(_quote_identifier(column) for column in feature_columns), _quote_identifier(target_id_column)]
    )
    hash_inputs = ", ".join([*(_quote_identifier(column) for column in feature_columns), _quote_identifier(target_id_column)])
    relation_sql = _relation_sql(parquet_path)
    return f"""
WITH ranked AS (
    SELECT
        {projected_columns},
        ROW_NUMBER() OVER (
            PARTITION BY {_quote_identifier(target_id_column)}
            ORDER BY hash({hash_inputs})
        ) AS sampled_rank
    FROM {relation_sql}
)
SELECT
    {projected_columns}
FROM ranked
WHERE sampled_rank <= {sample_per_class}
"""


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _load_balanced_training_sample(
    connection: duckdb.DuckDBPyConnection,
    train_path: Path,
    feature_columns: list[str],
    target_id_column: str,
    sample_per_class: int,
) -> tuple[np.ndarray, np.ndarray]:
    sample_query = _build_sample_query(train_path, feature_columns, target_id_column, sample_per_class)
    rows = connection.execute(sample_query).fetchall()
    if not rows:
        raise ValueError("Balanced training sample query returned no rows.")
    batch = np.asarray(rows, dtype=np.float64)
    return batch[:, :-1], batch[:, -1].astype(np.int64)


def _evaluate_split(
    connection: duckdb.DuckDBPyConnection,
    split_name: str,
    split_path: Path,
    feature_columns: list[str],
    target_id_column: str,
    classifier: HistGradientBoostingClassifier,
    target_mapping: dict[str, int],
    confusion_matrix_path: Path,
    batch_size: int,
) -> SplitEvaluation:
    num_classes = len(target_mapping)
    confusion_matrix = np.zeros((num_classes, num_classes), dtype=np.int64)
    row_count = 0

    for x_batch, y_batch in _iterate_batches(
        connection,
        split_path,
        feature_columns,
        target_id_column,
        batch_size,
    ):
        y_pred = classifier.predict(x_batch)
        row_count += int(y_batch.shape[0])
        np.add.at(confusion_matrix, (y_batch, y_pred), 1)

    _write_confusion_matrix_csv(confusion_matrix_path, confusion_matrix, target_mapping)
    (
        accuracy,
        balanced_accuracy,
        macro_precision,
        macro_recall,
        macro_f1,
        weighted_f1,
        class_metrics,
    ) = _compute_metrics_from_confusion_matrix(confusion_matrix, target_mapping)

    return SplitEvaluation(
        split_name=split_name,
        row_count=row_count,
        accuracy=accuracy,
        balanced_accuracy=balanced_accuracy,
        macro_precision=macro_precision,
        macro_recall=macro_recall,
        macro_f1=macro_f1,
        weighted_f1=weighted_f1,
        confusion_matrix_path=str(confusion_matrix_path),
        class_metrics=class_metrics,
    )


def _write_v1_report(
    project_root: Path,
    result: V1TreeBaselineResult,
    generated_at: str,
) -> Path:
    report_path = project_root / "reports" / "model_training" / "v1_tree_baseline_report.md"
    ensure_parent(report_path)
    lines = [
        "# V1 Tree Baseline Report",
        "",
        f"Generated at: {generated_at}",
        "",
        "## Summary",
        "",
        f"- Model name: `{result.model_name}`",
        f"- Model path: `{result.model_path}`",
        f"- Config path: `{result.config_path}`",
        f"- Parent experiment: `{result.parent_experiment}`",
        f"- Feature manifest path: `{result.feature_manifest_path}`",
        f"- Encoded feature manifest path: `{result.encoded_feature_manifest_path}`",
        f"- Feature columns: `{result.feature_columns}`",
        f"- Target column: `{result.target_column}`",
        f"- Target ID column: `{result.target_id_column}`",
        f"- Target mapping: `{result.target_mapping}`",
        f"- Full training rows: {result.full_training_rows}",
        f"- Sampled training rows: {result.sampled_training_rows}",
        f"- Sample per class: {result.sample_per_class}",
        f"- Primary metric: `{result.primary_metric_name}`",
        "",
    ]

    for evaluation in result.split_evaluations:
        lines.extend(
            [
                f"## {evaluation.split_name}",
                "",
                f"- Row count: {evaluation.row_count}",
                f"- Accuracy: {evaluation.accuracy:.6f}",
                f"- Balanced accuracy: {evaluation.balanced_accuracy:.6f}",
                f"- Macro precision: {evaluation.macro_precision:.6f}",
                f"- Macro recall: {evaluation.macro_recall:.6f}",
                f"- Macro F1: {evaluation.macro_f1:.6f}",
                f"- Weighted F1: {evaluation.weighted_f1:.6f}",
                f"- Confusion matrix path: `{evaluation.confusion_matrix_path}`",
                "",
                "### Class Metrics",
                "",
                "| Label | Label ID | Precision | Recall | F1 | Support |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for metric in evaluation.class_metrics:
            lines.append(
                f"| {metric.label} | {metric.label_id} | {metric.precision:.6f} | "
                f"{metric.recall:.6f} | {metric.f1:.6f} | {metric.support} |"
            )
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run_v1_tree_baseline(project_root: Path | None = None) -> int:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    config_path = root / "configs" / "modeling" / "v1_hist_gradient_boosting.yaml"
    config = load_yaml_file(config_path)
    feature_manifest_path = root / "metadata" / "manifests" / "feature_manifest.json"
    encoded_feature_manifest_path = root / "metadata" / "manifests" / "encoded_feature_manifest.json"
    encoded_feature_manifest = _load_json(encoded_feature_manifest_path)

    encoded_result = encoded_feature_manifest["result"]
    feature_columns = list(encoded_result["encoded_feature_columns"])
    target_column = str(encoded_result["target_column"])
    target_id_column = str(encoded_result["target_id_column"])
    target_mapping = {str(label): int(label_id) for label, label_id in encoded_result["target_mapping"].items()}

    model_name = str(config["model_name"])
    parent_experiment = str(config["iteration_notes"]["parent_experiment"])
    sample_per_class = int(config["training"]["sample_per_class"])
    primary_metric_name = str(config["evaluation"]["primary_metric"])
    random_seed = int(config["training"]["random_seed"])

    model_config = config["model"]
    classifier = HistGradientBoostingClassifier(
        learning_rate=float(model_config["learning_rate"]),
        max_iter=int(model_config["max_iter"]),
        max_leaf_nodes=int(model_config["max_leaf_nodes"]),
        min_samples_leaf=int(model_config["min_samples_leaf"]),
        l2_regularization=float(model_config["l2_regularization"]),
        early_stopping=bool(model_config["early_stopping"]),
        validation_fraction=float(model_config["validation_fraction"]),
        n_iter_no_change=int(model_config["n_iter_no_change"]),
        random_state=random_seed,
    )

    train_path = root / "data" / "features" / "encoded" / "train.parquet"
    validation_path = root / "data" / "features" / "encoded" / "validation.parquet"
    test_path = root / "data" / "features" / "encoded" / "test.parquet"

    generated_at = datetime.now(timezone.utc).isoformat()
    connection = duckdb.connect(database=":memory:")
    try:
        full_training_rows = int(connection.execute(f"SELECT COUNT(*) FROM {_relation_sql(train_path)}").fetchone()[0])
        x_train, y_train = _load_balanced_training_sample(
            connection=connection,
            train_path=train_path,
            feature_columns=feature_columns,
            target_id_column=target_id_column,
            sample_per_class=sample_per_class,
        )
        classifier.fit(x_train, y_train)
        sampled_training_rows = int(y_train.shape[0])

        confusion_dir = root / "reports" / "model_training"
        batch_size = 100000
        split_evaluations = [
            _evaluate_split(
                connection=connection,
                split_name=split_name,
                split_path=split_path,
                feature_columns=feature_columns,
                target_id_column=target_id_column,
                classifier=classifier,
                target_mapping=target_mapping,
                confusion_matrix_path=confusion_dir / f"{model_name}_{split_name}_confusion_matrix.csv",
                batch_size=batch_size,
            )
            for split_name, split_path in (
                ("train", train_path),
                ("validation", validation_path),
                ("test", test_path),
            )
        ]
    finally:
        connection.close()

    model_path = root / "models" / "baseline" / f"{model_name}.pkl"
    ensure_parent(model_path)
    with model_path.open("wb") as handle:
        pickle.dump(
            {
                "generated_at": generated_at,
                "model_name": model_name,
                "feature_columns": feature_columns,
                "target_column": target_column,
                "target_id_column": target_id_column,
                "target_mapping": target_mapping,
                "config": config,
                "classifier": classifier,
            },
            handle,
        )

    result = V1TreeBaselineResult(
        model_name=model_name,
        model_path=str(model_path),
        config_path=str(config_path),
        parent_experiment=parent_experiment,
        feature_manifest_path=str(feature_manifest_path),
        encoded_feature_manifest_path=str(encoded_feature_manifest_path),
        feature_columns=feature_columns,
        target_column=target_column,
        target_id_column=target_id_column,
        target_mapping=target_mapping,
        full_training_rows=full_training_rows,
        sampled_training_rows=sampled_training_rows,
        sample_per_class=sample_per_class,
        primary_metric_name=primary_metric_name,
        split_evaluations=split_evaluations,
    )

    report_path = _write_v1_report(root, result, generated_at)
    manifest_path = root / "metadata" / "manifests" / "v1_tree_baseline_manifest.json"
    write_json(
        manifest_path,
        {
            "generated_at": generated_at,
            "report_path": str(report_path),
            "result": {
                **{
                    key: value
                    for key, value in asdict(result).items()
                    if key != "split_evaluations"
                },
                "split_evaluations": [
                    {
                        **{
                            key: value
                            for key, value in asdict(evaluation).items()
                            if key != "class_metrics"
                        },
                        "class_metrics": [asdict(metric) for metric in evaluation.class_metrics],
                    }
                    for evaluation in result.split_evaluations
                ],
            },
        },
    )
    return 0
