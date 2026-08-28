from __future__ import annotations

import copy
import json
import pickle
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
from sklearn.linear_model import SGDClassifier
from sklearn.preprocessing import StandardScaler

from stsrs_data_engineering.config import ensure_parent, load_yaml_file, write_json


@dataclass
class ClassMetric:
    label: str
    label_id: int
    precision: float
    recall: float
    f1: float
    support: int


@dataclass
class SplitEvaluation:
    split_name: str
    row_count: int
    accuracy: float
    balanced_accuracy: float
    macro_precision: float
    macro_recall: float
    macro_f1: float
    weighted_f1: float
    confusion_matrix_path: str
    class_metrics: list[ClassMetric]


@dataclass
class BaselineTrainingResult:
    model_name: str
    model_path: str
    config_path: str
    feature_manifest_path: str
    encoded_feature_manifest_path: str
    feature_columns: list[str]
    target_column: str
    target_id_column: str
    target_mapping: dict[str, int]
    training_rows: int
    epochs_completed: int
    selected_epoch: int
    primary_metric_name: str
    split_evaluations: list[SplitEvaluation]


def _quote_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _relation_sql(path: Path) -> str:
    return f"read_parquet({_quote_literal(path.resolve().as_posix())})"


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _iterate_batches(
    connection: duckdb.DuckDBPyConnection,
    parquet_path: Path,
    feature_columns: list[str],
    target_id_column: str,
    batch_size: int,
) -> tuple[np.ndarray, np.ndarray]:
    projection = ", ".join([*(f'"{column}"' for column in feature_columns), f'"{target_id_column}"'])
    cursor = connection.execute(
        f"SELECT {projection} FROM {_relation_sql(parquet_path)}"
    )
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        batch = np.asarray(rows, dtype=np.float64)
        x_batch = batch[:, :-1]
        y_batch = batch[:, -1].astype(np.int64)
        yield x_batch, y_batch


def _build_classifier(config: dict[str, Any]) -> SGDClassifier:
    classifier_config = config["classifier"]
    training_config = config["training"]
    return SGDClassifier(
        loss=str(classifier_config["loss"]),
        penalty=str(classifier_config["penalty"]),
        alpha=float(classifier_config["alpha"]),
        learning_rate=str(classifier_config["learning_rate"]),
        eta0=float(classifier_config["eta0"]),
        class_weight=str(classifier_config["class_weight"]),
        max_iter=1,
        tol=None,
        random_state=int(training_config["random_seed"]),
    )


def _build_scaler(config: dict[str, Any]) -> StandardScaler:
    scaler_config = config["scaler"]
    return StandardScaler(
        with_mean=bool(scaler_config["with_mean"]),
        with_std=bool(scaler_config["with_std"]),
    )


def _fit_scaler(
    connection: duckdb.DuckDBPyConnection,
    train_path: Path,
    feature_columns: list[str],
    target_id_column: str,
    batch_size: int,
    scaler: StandardScaler,
) -> None:
    for x_batch, _ in _iterate_batches(
        connection,
        train_path,
        feature_columns,
        target_id_column,
        batch_size,
    ):
        scaler.partial_fit(x_batch)


def _estimate_balanced_class_weight(
    connection: duckdb.DuckDBPyConnection,
    train_path: Path,
    feature_columns: list[str],
    target_id_column: str,
    batch_size: int,
    classes: np.ndarray,
) -> dict[int, float]:
    class_counts = {int(class_id): 0 for class_id in classes}
    total_rows = 0

    for _x_batch, y_batch in _iterate_batches(
        connection,
        train_path,
        feature_columns,
        target_id_column,
        batch_size,
    ):
        total_rows += int(y_batch.shape[0])
        unique_values, counts = np.unique(y_batch, return_counts=True)
        for class_id, count in zip(unique_values.tolist(), counts.tolist(), strict=True):
            class_counts[int(class_id)] += int(count)

    num_classes = len(classes)
    return {
        class_id: float(total_rows / (num_classes * count))
        for class_id, count in class_counts.items()
        if count > 0
    }


def _train_classifier(
    connection: duckdb.DuckDBPyConnection,
    train_path: Path,
    feature_columns: list[str],
    target_id_column: str,
    batch_size: int,
    epochs: int,
    scaler: StandardScaler,
    classifier: SGDClassifier,
    classes: np.ndarray,
) -> None:
    first_batch = True
    for _epoch_index in range(epochs):
        for x_batch, y_batch in _iterate_batches(
            connection,
            train_path,
            feature_columns,
            target_id_column,
            batch_size,
        ):
            x_scaled = scaler.transform(x_batch)
            if first_batch:
                classifier.partial_fit(x_scaled, y_batch, classes=classes)
                first_batch = False
            else:
                classifier.partial_fit(x_scaled, y_batch)


def _compute_metrics_from_confusion_matrix(
    confusion_matrix: np.ndarray,
    target_mapping: dict[str, int],
) -> tuple[float, float, float, float, float, float, list[ClassMetric]]:
    support = confusion_matrix.sum(axis=1).astype(np.int64)
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

    total = int(support.sum())
    accuracy = 0.0 if total == 0 else float(true_positive.sum() / total)
    balanced_accuracy = float(recall.mean()) if recall.size else 0.0
    macro_precision = float(precision.mean()) if precision.size else 0.0
    macro_recall = float(recall.mean()) if recall.size else 0.0
    macro_f1 = float(f1.mean()) if f1.size else 0.0
    weighted_f1 = 0.0 if total == 0 else float(np.sum(f1 * support) / total)

    id_to_label = {label_id: label for label, label_id in target_mapping.items()}
    class_metrics = [
        ClassMetric(
            label=id_to_label[label_id],
            label_id=label_id,
            precision=float(precision[label_id]),
            recall=float(recall[label_id]),
            f1=float(f1[label_id]),
            support=int(support[label_id]),
        )
        for label_id in sorted(id_to_label)
    ]
    return (
        accuracy,
        balanced_accuracy,
        macro_precision,
        macro_recall,
        macro_f1,
        weighted_f1,
        class_metrics,
    )


def _write_confusion_matrix_csv(
    path: Path,
    confusion_matrix: np.ndarray,
    target_mapping: dict[str, int],
) -> None:
    ensure_parent(path)
    id_to_label = {label_id: label for label, label_id in target_mapping.items()}
    ordered_labels = [id_to_label[label_id] for label_id in sorted(id_to_label)]
    lines = [",".join(["true\\pred", *ordered_labels])]
    for row_label_id, row_values in enumerate(confusion_matrix):
        row_label = id_to_label[row_label_id]
        lines.append(",".join([row_label, *(str(int(value)) for value in row_values)]))
    path.write_text("\n".join(lines), encoding="utf-8")


def _evaluate_split(
    connection: duckdb.DuckDBPyConnection,
    split_name: str,
    split_path: Path,
    feature_columns: list[str],
    target_id_column: str,
    batch_size: int,
    scaler: StandardScaler,
    classifier: SGDClassifier,
    target_mapping: dict[str, int],
    confusion_matrix_path: Path,
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
        x_scaled = scaler.transform(x_batch)
        y_pred = classifier.predict(x_scaled)
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


def _write_baseline_training_report(
    project_root: Path,
    result: BaselineTrainingResult,
    generated_at: str,
) -> Path:
    report_path = project_root / "reports" / "model_training" / "baseline_training_report.md"
    ensure_parent(report_path)
    lines = [
        "# Baseline Training Report",
        "",
        f"Generated at: {generated_at}",
        "",
        "## Summary",
        "",
        f"- Model name: `{result.model_name}`",
        f"- Model path: `{result.model_path}`",
        f"- Config path: `{result.config_path}`",
        f"- Feature manifest path: `{result.feature_manifest_path}`",
        f"- Encoded feature manifest path: `{result.encoded_feature_manifest_path}`",
        f"- Feature columns: `{result.feature_columns}`",
        f"- Target column: `{result.target_column}`",
        f"- Target ID column: `{result.target_id_column}`",
        f"- Target mapping: `{result.target_mapping}`",
        f"- Training rows: {result.training_rows}",
        f"- Epochs completed: {result.epochs_completed}",
        f"- Selected epoch: {result.selected_epoch}",
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


def run_baseline_training(project_root: Path | None = None) -> int:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    training_config_path = root / "configs" / "modeling" / "baseline_sgd.yaml"
    training_config = load_yaml_file(training_config_path)
    feature_manifest_path = root / "metadata" / "manifests" / "feature_manifest.json"
    encoded_feature_manifest_path = root / "metadata" / "manifests" / "encoded_feature_manifest.json"
    feature_manifest = _load_json(feature_manifest_path)
    encoded_feature_manifest = _load_json(encoded_feature_manifest_path)

    encoded_result = encoded_feature_manifest["result"]
    feature_columns = list(encoded_result["encoded_feature_columns"])
    target_column = str(encoded_result["target_column"])
    target_id_column = str(encoded_result["target_id_column"])
    target_mapping = {str(label): int(label_id) for label, label_id in encoded_result["target_mapping"].items()}

    batch_size = int(training_config["training"]["batch_size"])
    epochs = int(training_config["training"]["epochs"])
    model_name = str(training_config["model_name"])
    primary_metric_name = str(training_config["evaluation"]["primary_metric"])

    train_path = root / "data" / "features" / "encoded" / "train.parquet"
    validation_path = root / "data" / "features" / "encoded" / "validation.parquet"
    test_path = root / "data" / "features" / "encoded" / "test.parquet"

    scaler = _build_scaler(training_config)
    classifier = _build_classifier(training_config)
    classes = np.asarray(sorted(target_mapping.values()), dtype=np.int64)

    generated_at = datetime.now(timezone.utc).isoformat()

    connection = duckdb.connect(database=":memory:")
    try:
        _fit_scaler(connection, train_path, feature_columns, target_id_column, batch_size, scaler)
        if str(training_config["classifier"]["class_weight"]) == "balanced":
            classifier.set_params(
                class_weight=_estimate_balanced_class_weight(
                    connection=connection,
                    train_path=train_path,
                    feature_columns=feature_columns,
                    target_id_column=target_id_column,
                    batch_size=batch_size,
                    classes=classes,
                )
            )
        _train_classifier(
            connection,
            train_path,
            feature_columns,
            target_id_column,
            batch_size,
            epochs,
            scaler,
            classifier,
            classes,
        )

        confusion_dir = root / "reports" / "model_training"
        split_evaluations = [
            _evaluate_split(
                connection=connection,
                split_name=split_name,
                split_path=split_path,
                feature_columns=feature_columns,
                target_id_column=target_id_column,
                batch_size=batch_size,
                scaler=scaler,
                classifier=classifier,
                target_mapping=target_mapping,
                confusion_matrix_path=confusion_dir / f"{model_name}_{split_name}_confusion_matrix.csv",
            )
            for split_name, split_path in (
                ("train", train_path),
                ("validation", validation_path),
                ("test", test_path),
            )
        ]
    finally:
        connection.close()

    training_rows = next(
        evaluation.row_count for evaluation in split_evaluations if evaluation.split_name == "train"
    )
    validation_evaluation = next(
        evaluation for evaluation in split_evaluations if evaluation.split_name == "validation"
    )
    selected_epoch = epochs
    _ = validation_evaluation

    model_path = root / "models" / "baseline" / f"{model_name}.pkl"
    ensure_parent(model_path)
    with model_path.open("wb") as handle:
        pickle.dump(
            {
                "generated_at": generated_at,
                "model_name": model_name,
                "scaler": scaler,
                "classifier": classifier,
                "feature_columns": feature_columns,
                "target_column": target_column,
                "target_id_column": target_id_column,
                "target_mapping": target_mapping,
                "config": training_config,
            },
            handle,
        )

    result = BaselineTrainingResult(
        model_name=model_name,
        model_path=str(model_path),
        config_path=str(training_config_path),
        feature_manifest_path=str(feature_manifest_path),
        encoded_feature_manifest_path=str(encoded_feature_manifest_path),
        feature_columns=feature_columns,
        target_column=target_column,
        target_id_column=target_id_column,
        target_mapping=target_mapping,
        training_rows=training_rows,
        epochs_completed=epochs,
        selected_epoch=selected_epoch,
        primary_metric_name=primary_metric_name,
        split_evaluations=split_evaluations,
    )

    report_path = _write_baseline_training_report(root, result, generated_at)
    manifest_path = root / "metadata" / "manifests" / "baseline_training_manifest.json"
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
