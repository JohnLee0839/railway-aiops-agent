from __future__ import annotations

import pickle
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from stsrs_data_engineering.baseline_training import (
    SplitEvaluation,
    _compute_metrics_from_confusion_matrix,
    _iterate_batches,
    _load_json,
    _relation_sql,
    _write_confusion_matrix_csv,
)
from stsrs_data_engineering.config import ensure_parent, load_yaml_file, write_json


@dataclass
class AblationVariantResult:
    variant_name: str
    description: str
    model_name: str
    model_path: str
    feature_columns: list[str]
    dropped_features: list[str]
    feature_count: int
    sampled_training_rows: int
    validation_macro_f1: float
    test_macro_f1: float
    validation_macro_f1_delta_vs_reference: float
    test_macro_f1_delta_vs_reference: float
    split_evaluations: list[SplitEvaluation]


@dataclass
class V1AblationResult:
    experiment_name: str
    config_path: str
    reference_manifest_path: str
    parent_experiment: str
    feature_manifest_path: str
    encoded_feature_manifest_path: str
    target_column: str
    target_id_column: str
    target_mapping: dict[str, int]
    full_training_rows: int
    sample_per_class: int
    primary_metric_name: str
    sample_hash_columns: list[str]
    variants: list[AblationVariantResult]


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _build_hist_gradient_boosting_classifier(
    model_config: dict[str, Any],
    random_seed: int,
) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
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


def _build_sample_query(
    parquet_path: Path,
    feature_columns: list[str],
    sample_hash_columns: list[str],
    target_id_column: str,
    sample_per_class: int,
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
            ORDER BY hash({hash_inputs}), {stable_order_projection}
        ) AS sampled_rank
    FROM {relation_sql}
)
SELECT
    {projected_columns}
FROM ranked
WHERE sampled_rank <= {sample_per_class}
"""


def _load_balanced_training_sample(
    connection: duckdb.DuckDBPyConnection,
    train_path: Path,
    feature_columns: list[str],
    sample_hash_columns: list[str],
    target_id_column: str,
    sample_per_class: int,
) -> tuple[np.ndarray, np.ndarray]:
    sample_query = _build_sample_query(
        parquet_path=train_path,
        feature_columns=feature_columns,
        sample_hash_columns=sample_hash_columns,
        target_id_column=target_id_column,
        sample_per_class=sample_per_class,
    )
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


def _resolve_variant_feature_columns(
    variant_config: dict[str, Any],
    base_feature_columns: list[str],
) -> list[str]:
    include_features = variant_config.get("include_features")
    drop_features = set(variant_config.get("drop_features", []))
    if include_features is not None:
        feature_columns = [str(column) for column in include_features]
    else:
        feature_columns = [column for column in base_feature_columns if column not in drop_features]

    unknown_features = [column for column in feature_columns if column not in base_feature_columns]
    if unknown_features:
        raise ValueError(
            f"Variant `{variant_config['variant_name']}` references unknown features: {unknown_features}"
        )
    if not feature_columns:
        raise ValueError(f"Variant `{variant_config['variant_name']}` resolves to zero features.")
    return feature_columns


def _extract_split_metric(
    split_evaluations: list[SplitEvaluation],
    split_name: str,
    metric_name: str,
) -> float:
    for evaluation in split_evaluations:
        if evaluation.split_name == split_name:
            return float(getattr(evaluation, metric_name))
    raise ValueError(f"Split `{split_name}` not found in evaluations.")


def _write_v1_ablation_report(
    project_root: Path,
    result: V1AblationResult,
    generated_at: str,
) -> Path:
    report_path = project_root / "reports" / "model_training" / "v1_ablation_report.md"
    ensure_parent(report_path)

    reference_variant = next(variant for variant in result.variants if variant.variant_name == "full_reference")
    drop_distance = next(variant for variant in result.variants if variant.variant_name == "drop_distance")
    drop_packetloss = next(variant for variant in result.variants if variant.variant_name == "drop_packetloss")
    drop_latency = next(variant for variant in result.variants if variant.variant_name == "drop_latency")
    top3_only = next(variant for variant in result.variants if variant.variant_name == "top3_only")
    top2_only = next(
        variant for variant in result.variants if variant.variant_name == "top2_distance_packetloss"
    )

    lines = [
        "# V1 Ablation Report",
        "",
        f"Generated at: {generated_at}",
        "",
        "## Summary",
        "",
        f"- Experiment name: `{result.experiment_name}`",
        f"- Config path: `{result.config_path}`",
        f"- Reference manifest path: `{result.reference_manifest_path}`",
        f"- Parent experiment: `{result.parent_experiment}`",
        f"- Feature manifest path: `{result.feature_manifest_path}`",
        f"- Encoded feature manifest path: `{result.encoded_feature_manifest_path}`",
        f"- Target column: `{result.target_column}`",
        f"- Target ID column: `{result.target_id_column}`",
        f"- Target mapping: `{result.target_mapping}`",
        f"- Full training rows: {result.full_training_rows}",
        f"- Sample per class: {result.sample_per_class}",
        f"- Primary metric: `{result.primary_metric_name}`",
        f"- Stable sample hash columns: `{result.sample_hash_columns}`",
        "",
        "## Variant Comparison",
        "",
        "| Variant | Feature Count | Validation Macro F1 | Delta vs Reference | Test Macro F1 | Delta vs Reference |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for variant in result.variants:
        lines.append(
            f"| {variant.variant_name} | {variant.feature_count} | {variant.validation_macro_f1:.6f} | "
            f"{variant.validation_macro_f1_delta_vs_reference:+.6f} | {variant.test_macro_f1:.6f} | "
            f"{variant.test_macro_f1_delta_vs_reference:+.6f} |"
        )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            f"- Dropping `Distance` changes validation macro F1 from `{reference_variant.validation_macro_f1:.6f}` to "
            f"`{drop_distance.validation_macro_f1:.6f}` ({drop_distance.validation_macro_f1_delta_vs_reference:+.6f}).",
            f"- Dropping `PacketLoss` changes validation macro F1 from `{reference_variant.validation_macro_f1:.6f}` to "
            f"`{drop_packetloss.validation_macro_f1:.6f}` ({drop_packetloss.validation_macro_f1_delta_vs_reference:+.6f}).",
            f"- Dropping `Latency` changes validation macro F1 from `{reference_variant.validation_macro_f1:.6f}` to "
            f"`{drop_latency.validation_macro_f1:.6f}` ({drop_latency.validation_macro_f1_delta_vs_reference:+.6f}).",
            f"- Keeping only `Distance + PacketLoss + Latency` reaches validation macro F1 "
            f"`{top3_only.validation_macro_f1:.6f}`.",
            f"- Keeping only `Distance + PacketLoss` reaches validation macro F1 "
            f"`{top2_only.validation_macro_f1:.6f}`.",
            "",
        ]
    )

    for variant in result.variants:
        lines.extend(
            [
                f"## {variant.variant_name}",
                "",
                f"- Description: {variant.description}",
                f"- Model name: `{variant.model_name}`",
                f"- Model path: `{variant.model_path}`",
                f"- Feature columns: `{variant.feature_columns}`",
                f"- Dropped features: `{variant.dropped_features}`",
                f"- Sampled training rows: {variant.sampled_training_rows}",
                "",
            ]
        )
        for evaluation in variant.split_evaluations:
            lines.extend(
                [
                    f"### {evaluation.split_name}",
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
                ]
            )

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run_v1_ablation(project_root: Path | None = None) -> int:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    config_path = root / "configs" / "modeling" / "v1_ablation_hist_gradient_boosting.yaml"
    config = load_yaml_file(config_path)
    feature_manifest_path = root / "metadata" / "manifests" / "feature_manifest.json"
    encoded_feature_manifest_path = root / "metadata" / "manifests" / "encoded_feature_manifest.json"
    reference_manifest_path = root / str(config["reference_model_manifest"])

    encoded_feature_manifest = _load_json(encoded_feature_manifest_path)
    reference_manifest = _load_json(reference_manifest_path)

    encoded_result = encoded_feature_manifest["result"]
    reference_result = reference_manifest["result"]
    base_feature_columns = list(encoded_result["encoded_feature_columns"])
    target_column = str(encoded_result["target_column"])
    target_id_column = str(encoded_result["target_id_column"])
    target_mapping = {str(label): int(label_id) for label, label_id in encoded_result["target_mapping"].items()}

    experiment_name = str(config["experiment_name"])
    parent_experiment = str(config["iteration_notes"]["parent_experiment"])
    sample_per_class = int(config["training"]["sample_per_class"])
    batch_size = int(config["training"]["batch_size"])
    random_seed = int(config["training"]["random_seed"])
    primary_metric_name = str(config["evaluation"]["primary_metric"])

    model_config = config["model"]
    train_path = root / "data" / "features" / "encoded" / "train.parquet"
    validation_path = root / "data" / "features" / "encoded" / "validation.parquet"
    test_path = root / "data" / "features" / "encoded" / "test.parquet"

    sample_hash_columns = list(reference_result["feature_columns"])
    generated_at = datetime.now(timezone.utc).isoformat()

    connection = duckdb.connect(database=":memory:")
    try:
        full_training_rows = int(connection.execute(f"SELECT COUNT(*) FROM {_relation_sql(train_path)}").fetchone()[0])
        variant_results: list[AblationVariantResult] = []
        for variant_config in config["variants"]:
            variant_name = str(variant_config["variant_name"])
            description = str(variant_config["description"])
            feature_columns = _resolve_variant_feature_columns(variant_config, base_feature_columns)
            dropped_features = [column for column in base_feature_columns if column not in feature_columns]

            classifier = _build_hist_gradient_boosting_classifier(model_config, random_seed)
            x_train, y_train = _load_balanced_training_sample(
                connection=connection,
                train_path=train_path,
                feature_columns=feature_columns,
                sample_hash_columns=sample_hash_columns,
                target_id_column=target_id_column,
                sample_per_class=sample_per_class,
            )
            classifier.fit(x_train, y_train)
            sampled_training_rows = int(y_train.shape[0])

            model_name = f"{experiment_name}_{variant_name}"
            confusion_dir = root / "reports" / "model_training"
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

            model_path = root / "models" / "baseline" / f"{model_name}.pkl"
            ensure_parent(model_path)
            with model_path.open("wb") as handle:
                pickle.dump(
                    {
                        "generated_at": generated_at,
                        "experiment_name": experiment_name,
                        "variant_name": variant_name,
                        "description": description,
                        "model_name": model_name,
                        "feature_columns": feature_columns,
                        "target_column": target_column,
                        "target_id_column": target_id_column,
                        "target_mapping": target_mapping,
                        "sample_hash_columns": sample_hash_columns,
                        "config": config,
                        "classifier": classifier,
                    },
                    handle,
                )

            variant_results.append(
                AblationVariantResult(
                    variant_name=variant_name,
                    description=description,
                    model_name=model_name,
                    model_path=str(model_path),
                    feature_columns=feature_columns,
                    dropped_features=dropped_features,
                    feature_count=len(feature_columns),
                    sampled_training_rows=sampled_training_rows,
                    validation_macro_f1=_extract_split_metric(split_evaluations, "validation", "macro_f1"),
                    test_macro_f1=_extract_split_metric(split_evaluations, "test", "macro_f1"),
                    validation_macro_f1_delta_vs_reference=0.0,
                    test_macro_f1_delta_vs_reference=0.0,
                    split_evaluations=split_evaluations,
                )
            )
    finally:
        connection.close()

    reference_variant = next(variant for variant in variant_results if variant.variant_name == "full_reference")
    for variant in variant_results:
        variant.validation_macro_f1_delta_vs_reference = (
            variant.validation_macro_f1 - reference_variant.validation_macro_f1
        )
        variant.test_macro_f1_delta_vs_reference = variant.test_macro_f1 - reference_variant.test_macro_f1

    result = V1AblationResult(
        experiment_name=experiment_name,
        config_path=str(config_path),
        reference_manifest_path=str(reference_manifest_path),
        parent_experiment=parent_experiment,
        feature_manifest_path=str(feature_manifest_path),
        encoded_feature_manifest_path=str(encoded_feature_manifest_path),
        target_column=target_column,
        target_id_column=target_id_column,
        target_mapping=target_mapping,
        full_training_rows=full_training_rows,
        sample_per_class=sample_per_class,
        primary_metric_name=primary_metric_name,
        sample_hash_columns=sample_hash_columns,
        variants=variant_results,
    )

    report_path = _write_v1_ablation_report(root, result, generated_at)
    manifest_path = root / "metadata" / "manifests" / "v1_ablation_manifest.json"
    write_json(
        manifest_path,
        {
            "generated_at": generated_at,
            "report_path": str(report_path),
            "result": {
                **{
                    key: value
                    for key, value in asdict(result).items()
                    if key != "variants"
                },
                "variants": [
                    {
                        **{
                            key: value
                            for key, value in asdict(variant).items()
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
                            for evaluation in variant.split_evaluations
                        ],
                    }
                    for variant in result.variants
                ],
            },
        },
    )
    return 0
