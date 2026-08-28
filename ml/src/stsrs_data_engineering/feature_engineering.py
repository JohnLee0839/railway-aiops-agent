from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from stsrs_data_engineering.config import ensure_parent, load_pipeline_config, write_json


@dataclass
class FeatureMetric:
    feature_name: str
    logical_type: str
    null_count: int
    null_ratio: float


@dataclass
class SplitFeatureSummary:
    split_name: str
    input_path: str
    output_path: str
    row_count: int
    target_null_count: int
    target_distribution: dict[str, int]
    feature_metrics: list[FeatureMetric]


@dataclass
class FeatureEngineeringResult:
    target_column: str
    feature_columns: list[str]
    categorical_feature_columns: list[str]
    numeric_feature_columns: list[str]
    excluded_columns: list[str]
    null_feature_ratio_warn_max: float
    target_column_required: bool
    manifest_required: bool
    audit_passed: bool
    failure_reasons: list[str]
    split_summaries: list[SplitFeatureSummary]


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _quote_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _relation_sql(path: Path) -> str:
    return f"read_parquet({_quote_literal(path.resolve().as_posix())})"


def _select_feature_columns(
    schema_columns: list[dict[str, Any]],
    target_column: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    feature_columns: list[dict[str, Any]] = []
    excluded_columns: list[str] = []

    for column in schema_columns:
        column_name = str(column["name"])
        role = str(column["role"])
        if role == "feature":
            feature_columns.append(column)
        else:
            excluded_columns.append(column_name)

    excluded_columns.extend(
        [
            target_column,
            "AttackInfo_raw",
            "RenewalInterval_control_center",
            "RenewalInterval_train",
            "SplitName",
        ]
    )
    return feature_columns, sorted(set(excluded_columns))


def _write_canonical_dataset(
    connection: duckdb.DuckDBPyConnection,
    input_relation_sql: str,
    feature_columns: list[str],
    target_column: str,
    output_path: Path,
) -> int:
    ensure_parent(output_path)
    projection = ", ".join([*(_quote_identifier(name) for name in feature_columns), _quote_identifier(target_column)])
    connection.execute(
        f"COPY (SELECT {projection} FROM {input_relation_sql}) "
        f"TO {_quote_literal(output_path.resolve().as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM read_parquet({_quote_literal(output_path.resolve().as_posix())})"
        ).fetchone()[0]
    )


def _build_feature_metric_query(input_relation_sql: str, feature_columns: list[dict[str, Any]], target_column: str) -> str:
    select_items = ["COUNT(*) AS row_count"]
    select_items.append(
        f"SUM(CASE WHEN {_quote_identifier(target_column)} IS NULL THEN 1 ELSE 0 END) AS target_null_count"
    )
    for column in feature_columns:
        name = str(column["name"])
        select_items.append(
            f"SUM(CASE WHEN {_quote_identifier(name)} IS NULL THEN 1 ELSE 0 END) AS {_quote_identifier(f'{name}__null_count')}"
        )
    select_clause = ",\n    ".join(select_items)
    return f"SELECT\n    {select_clause}\nFROM {input_relation_sql}"


def _compute_feature_metrics(
    metric_row: dict[str, Any],
    feature_columns: list[dict[str, Any]],
    row_count: int,
) -> list[FeatureMetric]:
    metrics: list[FeatureMetric] = []
    for column in feature_columns:
        name = str(column["name"])
        null_count = int(metric_row[f"{name}__null_count"])
        null_ratio = 0.0 if row_count == 0 else null_count / row_count
        metrics.append(
            FeatureMetric(
                feature_name=name,
                logical_type=str(column["logical_type"]),
                null_count=null_count,
                null_ratio=null_ratio,
            )
        )
    return metrics


def _read_target_distribution(
    connection: duckdb.DuckDBPyConnection,
    input_relation_sql: str,
    target_column: str,
) -> dict[str, int]:
    rows = connection.execute(
        f"""
SELECT {_quote_identifier(target_column)} AS label, COUNT(*) AS row_count
FROM {input_relation_sql}
GROUP BY {_quote_identifier(target_column)}
ORDER BY row_count DESC, label
"""
    ).fetchall()
    return {str(label): int(count) for label, count in rows}


def _evaluate_feature_quality(
    split_summaries: list[SplitFeatureSummary],
    null_feature_ratio_warn_max: float,
    target_column_required: bool,
) -> list[str]:
    failures: list[str] = []
    for summary in split_summaries:
        if target_column_required and summary.target_null_count > 0:
            failures.append(
                f"Split {summary.split_name} has {summary.target_null_count} null target rows."
            )
        for metric in summary.feature_metrics:
            if metric.null_ratio > null_feature_ratio_warn_max:
                failures.append(
                    f"Split {summary.split_name} feature {metric.feature_name} null ratio "
                    f"{metric.null_ratio:.6f} exceeds configured maximum {null_feature_ratio_warn_max:.6f}."
                )
    return failures


def _write_feature_quality_report(
    project_root: Path,
    result: FeatureEngineeringResult,
    generated_at: str,
) -> Path:
    report_path = project_root / "reports" / "data_validation" / "feature_quality_report.md"
    ensure_parent(report_path)
    status = "PASS" if result.audit_passed else "FAIL"

    lines = [
        "# Feature Quality Report",
        "",
        f"Generated at: {generated_at}",
        "",
        "## Summary",
        "",
        f"- Status: {status}",
        f"- Target column: `{result.target_column}`",
        f"- Feature columns: `{result.feature_columns}`",
        f"- Numeric feature columns: `{result.numeric_feature_columns}`",
        f"- Categorical feature columns: `{result.categorical_feature_columns}`",
        f"- Excluded columns: `{result.excluded_columns}`",
        f"- Null feature ratio maximum: {result.null_feature_ratio_warn_max:.6f}",
        f"- Target column required: {result.target_column_required}",
        f"- Feature manifest required: {result.manifest_required}",
        "",
    ]

    if result.failure_reasons:
        lines.extend(["### Failure Reasons", ""])
        for reason in result.failure_reasons:
            lines.append(f"- {reason}")
        lines.append("")

    for summary in result.split_summaries:
        lines.extend(
            [
                f"## {summary.split_name}",
                "",
                f"- Input path: `{summary.input_path}`",
                f"- Output path: `{summary.output_path}`",
                f"- Row count: {summary.row_count}",
                f"- Target null count: {summary.target_null_count}",
                f"- Target distribution: `{summary.target_distribution}`",
                "",
                "### Feature Null Metrics",
                "",
                "| Feature | Logical Type | Null Count | Null Ratio |",
                "| --- | --- | ---: | ---: |",
            ]
        )
        for metric in summary.feature_metrics:
            lines.append(
                f"| {metric.feature_name} | {metric.logical_type} | {metric.null_count} | {metric.null_ratio:.6f} |"
            )
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run_feature_engineering(project_root: Path | None = None) -> int:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    config = load_pipeline_config(root)
    schema = config["schema"]
    split_policy = config["split_policy"]
    quality_thresholds = config["quality_thresholds"]

    target_column = str(split_policy["target_column"])
    feature_quality = quality_thresholds["feature_quality"]
    null_feature_ratio_warn_max = float(feature_quality["null_feature_ratio_warn_max"])
    target_column_required = bool(feature_quality["target_column_required"])
    manifest_required = bool(feature_quality["feature_manifest_required"])

    feature_columns_config, excluded_columns = _select_feature_columns(
        schema_columns=list(schema["columns"]),
        target_column=target_column,
    )
    feature_columns = [str(column["name"]) for column in feature_columns_config]
    numeric_feature_columns = [
        str(column["name"]) for column in feature_columns_config if str(column["logical_type"]) == "numeric"
    ]
    categorical_feature_columns = [
        str(column["name"]) for column in feature_columns_config if str(column["logical_type"]) == "categorical"
    ]

    input_paths = {
        "train": root / "data" / "serving" / "train" / "train.parquet",
        "validation": root / "data" / "serving" / "validation" / "validation.parquet",
        "test": root / "data" / "serving" / "test" / "test.parquet",
    }
    output_paths = {
        split_name: root / "data" / "features" / "canonical" / f"{split_name}.parquet"
        for split_name in input_paths
    }

    generated_at = datetime.now(timezone.utc).isoformat()
    split_summaries: list[SplitFeatureSummary] = []

    connection = duckdb.connect(database=":memory:")
    try:
        for split_name, input_path in input_paths.items():
            input_relation_sql = _relation_sql(input_path)
            row_count = _write_canonical_dataset(
                connection=connection,
                input_relation_sql=input_relation_sql,
                feature_columns=feature_columns,
                target_column=target_column,
                output_path=output_paths[split_name],
            )

            metric_query = _build_feature_metric_query(
                input_relation_sql=input_relation_sql,
                feature_columns=feature_columns_config,
                target_column=target_column,
            )
            cursor = connection.execute(metric_query)
            metric_values = cursor.fetchone()
            metric_columns = [description[0] for description in cursor.description]
            metric_row = dict(zip(metric_columns, metric_values, strict=True))
            target_null_count = int(metric_row["target_null_count"])
            feature_metrics = _compute_feature_metrics(
                metric_row=metric_row,
                feature_columns=feature_columns_config,
                row_count=row_count,
            )
            target_distribution = _read_target_distribution(
                connection=connection,
                input_relation_sql=input_relation_sql,
                target_column=target_column,
            )

            split_summaries.append(
                SplitFeatureSummary(
                    split_name=split_name,
                    input_path=str(input_path),
                    output_path=str(output_paths[split_name]),
                    row_count=row_count,
                    target_null_count=target_null_count,
                    target_distribution=target_distribution,
                    feature_metrics=feature_metrics,
                )
            )
    finally:
        connection.close()

    failure_reasons = _evaluate_feature_quality(
        split_summaries=split_summaries,
        null_feature_ratio_warn_max=null_feature_ratio_warn_max,
        target_column_required=target_column_required,
    )

    result = FeatureEngineeringResult(
        target_column=target_column,
        feature_columns=feature_columns,
        categorical_feature_columns=categorical_feature_columns,
        numeric_feature_columns=numeric_feature_columns,
        excluded_columns=excluded_columns,
        null_feature_ratio_warn_max=null_feature_ratio_warn_max,
        target_column_required=target_column_required,
        manifest_required=manifest_required,
        audit_passed=not failure_reasons,
        failure_reasons=failure_reasons,
        split_summaries=split_summaries,
    )

    report_path = _write_feature_quality_report(root, result, generated_at)
    manifest_path = root / "metadata" / "manifests" / "feature_manifest.json"
    write_json(
        manifest_path,
        {
            "generated_at": generated_at,
            "report_path": str(report_path),
            "result": {
                **{
                    key: value
                    for key, value in asdict(result).items()
                    if key != "split_summaries"
                },
                "split_summaries": [
                    {
                        **{
                            key: value
                            for key, value in asdict(summary).items()
                            if key != "feature_metrics"
                        },
                        "feature_metrics": [asdict(metric) for metric in summary.feature_metrics],
                    }
                    for summary in result.split_summaries
                ],
            },
        },
    )

    return 0 if result.audit_passed else 1
