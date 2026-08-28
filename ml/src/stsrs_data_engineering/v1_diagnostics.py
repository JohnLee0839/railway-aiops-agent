from __future__ import annotations

import csv
import pickle
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
from sklearn.inspection import permutation_importance

from stsrs_data_engineering.config import ensure_parent, write_json


@dataclass
class PermutationImportanceMetric:
    feature_name: str
    mean_importance: float
    std_importance: float


@dataclass
class LeakagePairMetric:
    left_split: str
    right_split: str
    exact_row_overlap_count: int
    feature_only_overlap_count: int
    conflicting_feature_overlap_count: int


@dataclass
class V1DiagnosticsResult:
    model_name: str
    model_path: str
    validation_sample_rows: int
    permutation_repeats: int
    primary_scoring: str
    permutation_importance_path: str
    leakage_check_path: str
    top_permutation_importances: list[PermutationImportanceMetric]
    leakage_pair_metrics: list[LeakagePairMetric]


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _quote_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _relation_sql(path: Path) -> str:
    return f"read_parquet({_quote_literal(path.resolve().as_posix())})"


def _load_pickle(path: Path) -> object:
    with path.open("rb") as handle:
        return pickle.load(handle)


def _read_validation_sample(
    connection: duckdb.DuckDBPyConnection,
    validation_path: Path,
    feature_columns: list[str],
    target_id_column: str,
    sample_per_class: int,
) -> tuple[np.ndarray, np.ndarray]:
    projected = ", ".join([*(_quote_identifier(column) for column in feature_columns), _quote_identifier(target_id_column)])
    hash_inputs = ", ".join([*(_quote_identifier(column) for column in feature_columns), _quote_identifier(target_id_column)])
    rows = connection.execute(
        f"""
WITH ranked AS (
    SELECT
        {projected},
        ROW_NUMBER() OVER (
            PARTITION BY {_quote_identifier(target_id_column)}
            ORDER BY hash({hash_inputs})
        ) AS sampled_rank
    FROM {_relation_sql(validation_path)}
)
SELECT {projected}
FROM ranked
WHERE sampled_rank <= {sample_per_class}
"""
    ).fetchall()
    batch = np.asarray(rows, dtype=np.float64)
    return batch[:, :-1], batch[:, -1].astype(np.int64)


def _write_permutation_importance_csv(
    output_path: Path,
    metrics: list[PermutationImportanceMetric],
) -> None:
    ensure_parent(output_path)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["feature_name", "mean_importance", "std_importance"])
        for metric in metrics:
            writer.writerow([metric.feature_name, metric.mean_importance, metric.std_importance])


def _hash_projection(feature_columns: list[str], target_id_column: str | None) -> str:
    fields = [*(_quote_identifier(column) for column in feature_columns)]
    if target_id_column is not None:
        fields.append(_quote_identifier(target_id_column))
    return f"hash({', '.join(fields)})"


def _compute_leakage_pair_metric(
    connection: duckdb.DuckDBPyConnection,
    left_split: str,
    right_split: str,
    left_path: Path,
    right_path: Path,
    feature_columns: list[str],
    target_id_column: str,
) -> LeakagePairMetric:
    feature_hash_expr = _hash_projection(feature_columns, None)
    row_hash_expr = _hash_projection(feature_columns, target_id_column)
    left_sql = _relation_sql(left_path)
    right_sql = _relation_sql(right_path)

    exact_row_overlap_count = int(
        connection.execute(
            f"""
WITH left_rows AS (
    SELECT DISTINCT {row_hash_expr} AS row_hash
    FROM {left_sql}
),
right_rows AS (
    SELECT DISTINCT {row_hash_expr} AS row_hash
    FROM {right_sql}
)
SELECT COUNT(*)
FROM left_rows
INNER JOIN right_rows USING (row_hash)
"""
        ).fetchone()[0]
    )

    feature_only_overlap_count = int(
        connection.execute(
            f"""
WITH left_rows AS (
    SELECT DISTINCT {feature_hash_expr} AS feature_hash
    FROM {left_sql}
),
right_rows AS (
    SELECT DISTINCT {feature_hash_expr} AS feature_hash
    FROM {right_sql}
)
SELECT COUNT(*)
FROM left_rows
INNER JOIN right_rows USING (feature_hash)
"""
        ).fetchone()[0]
    )

    conflicting_feature_overlap_count = int(
        connection.execute(
            f"""
WITH left_rows AS (
    SELECT DISTINCT
        {feature_hash_expr} AS feature_hash,
        {_quote_identifier(target_id_column)} AS target_id
    FROM {left_sql}
),
right_rows AS (
    SELECT DISTINCT
        {feature_hash_expr} AS feature_hash,
        {_quote_identifier(target_id_column)} AS target_id
    FROM {right_sql}
),
shared_features AS (
    SELECT DISTINCT left_rows.feature_hash
    FROM left_rows
    INNER JOIN right_rows USING (feature_hash)
),
left_targets AS (
    SELECT feature_hash, MIN(target_id) AS min_target_id, MAX(target_id) AS max_target_id
    FROM left_rows
    GROUP BY feature_hash
),
right_targets AS (
    SELECT feature_hash, MIN(target_id) AS min_target_id, MAX(target_id) AS max_target_id
    FROM right_rows
    GROUP BY feature_hash
)
SELECT COUNT(*)
FROM shared_features
INNER JOIN left_targets USING (feature_hash)
INNER JOIN right_targets USING (feature_hash)
WHERE left_targets.min_target_id <> right_targets.min_target_id
   OR left_targets.max_target_id <> right_targets.max_target_id
"""
        ).fetchone()[0]
    )

    return LeakagePairMetric(
        left_split=left_split,
        right_split=right_split,
        exact_row_overlap_count=exact_row_overlap_count,
        feature_only_overlap_count=feature_only_overlap_count,
        conflicting_feature_overlap_count=conflicting_feature_overlap_count,
    )


def _write_leakage_csv(output_path: Path, metrics: list[LeakagePairMetric]) -> None:
    ensure_parent(output_path)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "left_split",
                "right_split",
                "exact_row_overlap_count",
                "feature_only_overlap_count",
                "conflicting_feature_overlap_count",
            ]
        )
        for metric in metrics:
            writer.writerow(
                [
                    metric.left_split,
                    metric.right_split,
                    metric.exact_row_overlap_count,
                    metric.feature_only_overlap_count,
                    metric.conflicting_feature_overlap_count,
                ]
            )


def _write_v1_diagnostics_report(
    project_root: Path,
    result: V1DiagnosticsResult,
    generated_at: str,
) -> Path:
    report_path = project_root / "reports" / "model_training" / "v1_diagnostics_report.md"
    ensure_parent(report_path)
    lines = [
        "# V1 Diagnostics Report",
        "",
        f"Generated at: {generated_at}",
        "",
        "## Summary",
        "",
        f"- Model name: `{result.model_name}`",
        f"- Model path: `{result.model_path}`",
        f"- Validation sample rows: {result.validation_sample_rows}",
        f"- Permutation repeats: {result.permutation_repeats}",
        f"- Primary scoring: `{result.primary_scoring}`",
        f"- Permutation importance path: `{result.permutation_importance_path}`",
        f"- Leakage check path: `{result.leakage_check_path}`",
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
    lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run_v1_diagnostics(project_root: Path | None = None) -> int:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    v1_manifest_path = root / "metadata" / "manifests" / "v1_tree_baseline_manifest.json"
    v1_manifest = __import__("json").load(v1_manifest_path.open("r", encoding="utf-8"))
    v1_result = v1_manifest["result"]

    model_path = Path(v1_result["model_path"])
    model_payload = _load_pickle(model_path)
    classifier = model_payload["classifier"]
    feature_columns = list(model_payload["feature_columns"])
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
        x_validation, y_validation = _read_validation_sample(
            connection=connection,
            validation_path=validation_path,
            feature_columns=feature_columns,
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

    permutation_path = root / "reports" / "model_training" / "v1_permutation_importance.csv"
    leakage_path = root / "reports" / "model_training" / "v1_split_leakage_checks.csv"
    _write_permutation_importance_csv(permutation_path, permutation_metrics)
    _write_leakage_csv(leakage_path, leakage_pair_metrics)

    result = V1DiagnosticsResult(
        model_name=str(v1_result["model_name"]),
        model_path=str(model_path),
        validation_sample_rows=int(y_validation.shape[0]),
        permutation_repeats=permutation_repeats,
        primary_scoring=primary_scoring,
        permutation_importance_path=str(permutation_path),
        leakage_check_path=str(leakage_path),
        top_permutation_importances=permutation_metrics[:10],
        leakage_pair_metrics=leakage_pair_metrics,
    )

    report_path = _write_v1_diagnostics_report(root, result, generated_at)
    manifest_path = root / "metadata" / "manifests" / "v1_diagnostics_manifest.json"
    write_json(
        manifest_path,
        {
            "generated_at": generated_at,
            "report_path": str(report_path),
            "result": {
                **{
                    key: value
                    for key, value in asdict(result).items()
                    if key not in {"top_permutation_importances", "leakage_pair_metrics"}
                },
                "top_permutation_importances": [asdict(metric) for metric in result.top_permutation_importances],
                "leakage_pair_metrics": [asdict(metric) for metric in result.leakage_pair_metrics],
            },
        },
    )
    return 0
