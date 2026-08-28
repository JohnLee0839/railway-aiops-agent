from __future__ import annotations

import pickle
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from stsrs_data_engineering.baseline_training import _load_json, _relation_sql
from stsrs_data_engineering.config import ensure_parent, load_yaml_file, write_json
from stsrs_data_engineering.v1_ablation import (
    _build_hist_gradient_boosting_classifier,
    _evaluate_split,
    _load_balanced_training_sample,
)


@dataclass
class V2CompactTreeResult:
    model_name: str
    model_path: str
    config_path: str
    parent_experiment: str
    promoted_ablation_variant: str
    reference_model_manifest_path: str
    reference_ablation_manifest_path: str
    reference_model_path: str
    reference_feature_count: int
    reference_model_size_bytes: int
    feature_columns: list[str]
    sample_hash_columns: list[str]
    target_column: str
    target_id_column: str
    target_mapping: dict[str, int]
    full_training_rows: int
    sampled_training_rows: int
    sample_per_class: int
    feature_count: int
    feature_reduction_count: int
    feature_reduction_ratio: float
    model_size_bytes: int
    validation_macro_f1_delta_vs_v1: float
    test_macro_f1_delta_vs_v1: float
    primary_metric_name: str
    split_evaluations: list[dict]


def _write_v2_report(
    project_root: Path,
    result: V2CompactTreeResult,
    generated_at: str,
) -> Path:
    report_path = project_root / "reports" / "model_training" / "v2_compact_tree_report.md"
    ensure_parent(report_path)
    lines = [
        "# V2 Compact Tree Report",
        "",
        f"Generated at: {generated_at}",
        "",
        "## Summary",
        "",
        f"- Model name: `{result.model_name}`",
        f"- Model path: `{result.model_path}`",
        f"- Config path: `{result.config_path}`",
        f"- Parent experiment: `{result.parent_experiment}`",
        f"- Promoted ablation variant: `{result.promoted_ablation_variant}`",
        f"- Reference model manifest path: `{result.reference_model_manifest_path}`",
        f"- Reference ablation manifest path: `{result.reference_ablation_manifest_path}`",
        f"- Reference model path: `{result.reference_model_path}`",
        f"- Feature columns: `{result.feature_columns}`",
        f"- Stable sample hash columns: `{result.sample_hash_columns}`",
        f"- Target column: `{result.target_column}`",
        f"- Target ID column: `{result.target_id_column}`",
        f"- Target mapping: `{result.target_mapping}`",
        f"- Full training rows: {result.full_training_rows}",
        f"- Sampled training rows: {result.sampled_training_rows}",
        f"- Sample per class: {result.sample_per_class}",
        f"- Feature count: {result.feature_count}",
        f"- Reference feature count: {result.reference_feature_count}",
        f"- Feature reduction count: {result.feature_reduction_count}",
        f"- Feature reduction ratio: {result.feature_reduction_ratio:.6f}",
        f"- Model size bytes: {result.model_size_bytes}",
        f"- Reference model size bytes: {result.reference_model_size_bytes}",
        f"- Validation macro F1 delta vs V1: {result.validation_macro_f1_delta_vs_v1:+.6f}",
        f"- Test macro F1 delta vs V1: {result.test_macro_f1_delta_vs_v1:+.6f}",
        f"- Primary metric: `{result.primary_metric_name}`",
        "",
        "## Iteration Rationale",
        "",
        "- V1 diagnostics showed that Distance, PacketLoss, and Latency carried almost all of the predictive signal.",
        "- V1 ablation confirmed that the top-3 subset retained nearly all performance while removing 9 engineered inputs.",
        "- This V2 iteration formalizes that compact subset as a standalone, easier-to-explain model version.",
        "",
    ]

    for evaluation in result.split_evaluations:
        lines.extend(
            [
                f"## {evaluation['split_name']}",
                "",
                f"- Row count: {evaluation['row_count']}",
                f"- Accuracy: {evaluation['accuracy']:.6f}",
                f"- Balanced accuracy: {evaluation['balanced_accuracy']:.6f}",
                f"- Macro precision: {evaluation['macro_precision']:.6f}",
                f"- Macro recall: {evaluation['macro_recall']:.6f}",
                f"- Macro F1: {evaluation['macro_f1']:.6f}",
                f"- Weighted F1: {evaluation['weighted_f1']:.6f}",
                f"- Confusion matrix path: `{evaluation['confusion_matrix_path']}`",
                "",
                "### Class Metrics",
                "",
                "| Label | Label ID | Precision | Recall | F1 | Support |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for metric in evaluation["class_metrics"]:
            lines.append(
                f"| {metric['label']} | {metric['label_id']} | {metric['precision']:.6f} | "
                f"{metric['recall']:.6f} | {metric['f1']:.6f} | {metric['support']} |"
            )
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run_v2_compact_tree(project_root: Path | None = None) -> int:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    config_path = root / "configs" / "modeling" / "v2_compact_top3_hist_gradient_boosting.yaml"
    config = load_yaml_file(config_path)

    feature_manifest_path = root / "metadata" / "manifests" / "feature_manifest.json"
    encoded_feature_manifest_path = root / "metadata" / "manifests" / "encoded_feature_manifest.json"
    reference_model_manifest_path = root / str(config["reference_model_manifest"])
    reference_ablation_manifest_path = root / str(config["reference_ablation_manifest"])

    feature_manifest = _load_json(feature_manifest_path)
    _ = feature_manifest
    encoded_feature_manifest = _load_json(encoded_feature_manifest_path)
    reference_model_manifest = _load_json(reference_model_manifest_path)
    reference_ablation_manifest = _load_json(reference_ablation_manifest_path)

    encoded_result = encoded_feature_manifest["result"]
    reference_model_result = reference_model_manifest["result"]
    ablation_result = reference_ablation_manifest["result"]

    feature_columns = [str(column) for column in config["feature_selection"]["selected_feature_columns"]]
    promoted_ablation_variant = str(config["feature_selection"]["source_ablation_variant"])
    target_column = str(encoded_result["target_column"])
    target_id_column = str(encoded_result["target_id_column"])
    target_mapping = {str(label): int(label_id) for label, label_id in encoded_result["target_mapping"].items()}
    sample_hash_columns = [str(column) for column in ablation_result["sample_hash_columns"]]

    model_name = str(config["model_name"])
    parent_experiment = str(config["iteration_notes"]["parent_experiment"])
    sample_per_class = int(config["training"]["sample_per_class"])
    batch_size = int(config["training"]["batch_size"])
    random_seed = int(config["training"]["random_seed"])
    primary_metric_name = str(config["evaluation"]["primary_metric"])

    train_path = root / "data" / "features" / "encoded" / "train.parquet"
    validation_path = root / "data" / "features" / "encoded" / "validation.parquet"
    test_path = root / "data" / "features" / "encoded" / "test.parquet"

    generated_at = datetime.now(timezone.utc).isoformat()
    connection = duckdb.connect(database=":memory:")
    try:
        full_training_rows = int(connection.execute(f"SELECT COUNT(*) FROM {_relation_sql(train_path)}").fetchone()[0])
        classifier = _build_hist_gradient_boosting_classifier(config["model"], random_seed)
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
                "sample_hash_columns": sample_hash_columns,
                "target_column": target_column,
                "target_id_column": target_id_column,
                "target_mapping": target_mapping,
                "config": config,
                "classifier": classifier,
            },
            handle,
        )

    reference_model_path = Path(str(reference_model_result["model_path"]))
    reference_feature_count = len(reference_model_result["feature_columns"])
    reference_validation_macro_f1 = next(
        evaluation["macro_f1"]
        for evaluation in reference_model_result["split_evaluations"]
        if evaluation["split_name"] == "validation"
    )
    reference_test_macro_f1 = next(
        evaluation["macro_f1"]
        for evaluation in reference_model_result["split_evaluations"]
        if evaluation["split_name"] == "test"
    )

    validation_macro_f1 = next(
        evaluation.macro_f1 for evaluation in split_evaluations if evaluation.split_name == "validation"
    )
    test_macro_f1 = next(
        evaluation.macro_f1 for evaluation in split_evaluations if evaluation.split_name == "test"
    )

    result = V2CompactTreeResult(
        model_name=model_name,
        model_path=str(model_path),
        config_path=str(config_path),
        parent_experiment=parent_experiment,
        promoted_ablation_variant=promoted_ablation_variant,
        reference_model_manifest_path=str(reference_model_manifest_path),
        reference_ablation_manifest_path=str(reference_ablation_manifest_path),
        reference_model_path=str(reference_model_path),
        reference_feature_count=reference_feature_count,
        reference_model_size_bytes=reference_model_path.stat().st_size,
        feature_columns=feature_columns,
        sample_hash_columns=sample_hash_columns,
        target_column=target_column,
        target_id_column=target_id_column,
        target_mapping=target_mapping,
        full_training_rows=full_training_rows,
        sampled_training_rows=sampled_training_rows,
        sample_per_class=sample_per_class,
        feature_count=len(feature_columns),
        feature_reduction_count=reference_feature_count - len(feature_columns),
        feature_reduction_ratio=(reference_feature_count - len(feature_columns)) / reference_feature_count,
        model_size_bytes=model_path.stat().st_size,
        validation_macro_f1_delta_vs_v1=float(validation_macro_f1 - reference_validation_macro_f1),
        test_macro_f1_delta_vs_v1=float(test_macro_f1 - reference_test_macro_f1),
        primary_metric_name=primary_metric_name,
        split_evaluations=[
            {
                **{
                    key: value
                    for key, value in asdict(evaluation).items()
                    if key != "class_metrics"
                },
                "class_metrics": [asdict(metric) for metric in evaluation.class_metrics],
            }
            for evaluation in split_evaluations
        ],
    )

    report_path = _write_v2_report(root, result, generated_at)
    manifest_path = root / "metadata" / "manifests" / "v2_compact_tree_manifest.json"
    write_json(
        manifest_path,
        {
            "generated_at": generated_at,
            "report_path": str(report_path),
            "result": asdict(result),
        },
    )
    return 0
