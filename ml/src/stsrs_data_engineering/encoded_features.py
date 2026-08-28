from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from stsrs_data_engineering.config import ensure_parent, load_pipeline_config, write_json


@dataclass
class EncodedFeatureMetric:
    feature_name: str
    null_count: int
    null_ratio: float


@dataclass
class EncodedSplitSummary:
    split_name: str
    input_path: str
    output_path: str
    row_count: int
    target_id_null_count: int
    encoded_feature_metrics: list[EncodedFeatureMetric]


@dataclass
class EncodedFeatureResult:
    target_column: str
    target_id_column: str
    target_mapping: dict[str, int]
    numeric_feature_columns: list[str]
    categorical_feature_columns: list[str]
    encoded_feature_columns: list[str]
    audit_passed: bool
    failure_reasons: list[str]
    split_summaries: list[EncodedSplitSummary]


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _quote_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _relation_sql(path: Path) -> str:
    return f"read_parquet({_quote_literal(path.resolve().as_posix())})"


def _build_target_id_case_expr(target_column: str, target_mapping: dict[str, int]) -> str:
    when_clauses = [
        f"WHEN {_quote_identifier(target_column)} = {_quote_literal(label)} THEN {label_id}"
        for label, label_id in target_mapping.items()
    ]
    return "CASE " + " ".join(when_clauses) + " ELSE NULL END"


def _build_encoded_projection(
    numeric_feature_columns: list[str],
    categorical_feature_specs: list[dict[str, Any]],
    target_column: str,
    target_id_column: str,
    target_mapping: dict[str, int],
) -> tuple[list[str], list[str]]:
    projection_items: list[str] = []
    encoded_feature_columns: list[str] = []

    for column_name in numeric_feature_columns:
        projection_items.append(_quote_identifier(column_name))
        encoded_feature_columns.append(column_name)

    for column in categorical_feature_specs:
        column_name = str(column["name"])
        allowed_values = list(column["allowed_values"])
        for allowed_value in allowed_values:
            encoded_name = f"{column_name}__is_{allowed_value}"
            projection_items.append(
                f"CASE WHEN {_quote_identifier(column_name)} = {_quote_literal(str(allowed_value))} "
                f"THEN 1 ELSE 0 END AS {_quote_identifier(encoded_name)}"
            )
            encoded_feature_columns.append(encoded_name)

    projection_items.append(_quote_identifier(target_column))
    projection_items.append(
        f"{_build_target_id_case_expr(target_column, target_mapping)} AS {_quote_identifier(target_id_column)}"
    )
    return projection_items, encoded_feature_columns


def _write_encoded_dataset(
    connection: duckdb.DuckDBPyConnection,
    input_relation_sql: str,
    projection_items: list[str],
    output_path: Path,
) -> int:
    ensure_parent(output_path)
    projection = ", ".join(projection_items)
    connection.execute(
        f"COPY (SELECT {projection} FROM {input_relation_sql}) "
        f"TO {_quote_literal(output_path.resolve().as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM read_parquet({_quote_literal(output_path.resolve().as_posix())})"
        ).fetchone()[0]
    )


def _build_metric_query(
    relation_sql: str,
    encoded_feature_columns: list[str],
    target_id_column: str,
) -> str:
    select_items = [
        "COUNT(*) AS row_count",
        f"SUM(CASE WHEN {_quote_identifier(target_id_column)} IS NULL THEN 1 ELSE 0 END) AS target_id_null_count",
    ]
    for column_name in encoded_feature_columns:
        select_items.append(
            f"SUM(CASE WHEN {_quote_identifier(column_name)} IS NULL THEN 1 ELSE 0 END) AS {_quote_identifier(f'{column_name}__null_count')}"
        )
    return f"SELECT\n    {',\n    '.join(select_items)}\nFROM {relation_sql}"


def _compute_encoded_feature_metrics(
    metric_row: dict[str, Any],
    encoded_feature_columns: list[str],
    row_count: int,
) -> list[EncodedFeatureMetric]:
    metrics: list[EncodedFeatureMetric] = []
    for column_name in encoded_feature_columns:
        null_count = int(metric_row[f"{column_name}__null_count"])
        null_ratio = 0.0 if row_count == 0 else null_count / row_count
        metrics.append(
            EncodedFeatureMetric(
                feature_name=column_name,
                null_count=null_count,
                null_ratio=null_ratio,
            )
        )
    return metrics


def _evaluate_encoded_features(
    split_summaries: list[EncodedSplitSummary],
) -> list[str]:
    failures: list[str] = []
    for summary in split_summaries:
        if summary.target_id_null_count > 0:
            failures.append(
                f"Split {summary.split_name} has {summary.target_id_null_count} null target IDs."
            )
        for metric in summary.encoded_feature_metrics:
            if metric.null_count > 0:
                failures.append(
                    f"Split {summary.split_name} encoded feature {metric.feature_name} has {metric.null_count} null values."
                )
    return failures


def _write_encoded_feature_report(
    project_root: Path,
    result: EncodedFeatureResult,
    generated_at: str,
) -> Path:
    report_path = project_root / "reports" / "data_validation" / "encoded_feature_report.md"
    ensure_parent(report_path)
    status = "PASS" if result.audit_passed else "FAIL"

    lines = [
        "# Encoded Feature Report",
        "",
        f"Generated at: {generated_at}",
        "",
        "## Summary",
        "",
        f"- Status: {status}",
        f"- Target column: `{result.target_column}`",
        f"- Target ID column: `{result.target_id_column}`",
        f"- Target mapping: `{result.target_mapping}`",
        f"- Numeric feature columns: `{result.numeric_feature_columns}`",
        f"- Categorical feature columns: `{result.categorical_feature_columns}`",
        f"- Encoded feature columns: `{result.encoded_feature_columns}`",
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
                f"- Target ID null count: {summary.target_id_null_count}",
                "",
                "### Encoded Feature Null Metrics",
                "",
                "| Feature | Null Count | Null Ratio |",
                "| --- | ---: | ---: |",
            ]
        )
        for metric in summary.encoded_feature_metrics:
            lines.append(
                f"| {metric.feature_name} | {metric.null_count} | {metric.null_ratio:.6f} |"
            )
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run_encoded_feature_generation(project_root: Path | None = None) -> int:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    config = load_pipeline_config(root)
    schema = config["schema"]
    labels = config["labels"]
    feature_manifest = load_pipeline_config(root)["quality_thresholds"]  # maintain config load symmetry
    _ = feature_manifest

    target_column = str(labels["target_column"])
    target_id_column = f"{target_column}Id"
    target_mapping = {
        label: index for index, label in enumerate(list(labels["allowed_targets"]))
    }

    schema_columns = list(schema["columns"])
    numeric_feature_columns = [
        str(column["name"])
        for column in schema_columns
        if str(column["role"]) == "feature" and str(column["logical_type"]) == "numeric"
    ]
    categorical_feature_specs = [
        column
        for column in schema_columns
        if str(column["role"]) == "feature" and str(column["logical_type"]) == "categorical"
    ]
    categorical_feature_columns = [str(column["name"]) for column in categorical_feature_specs]

    projection_items, encoded_feature_columns = _build_encoded_projection(
        numeric_feature_columns=numeric_feature_columns,
        categorical_feature_specs=categorical_feature_specs,
        target_column=target_column,
        target_id_column=target_id_column,
        target_mapping=target_mapping,
    )

    input_paths = {
        "train": root / "data" / "features" / "canonical" / "train.parquet",
        "validation": root / "data" / "features" / "canonical" / "validation.parquet",
        "test": root / "data" / "features" / "canonical" / "test.parquet",
    }
    output_paths = {
        split_name: root / "data" / "features" / "encoded" / f"{split_name}.parquet"
        for split_name in input_paths
    }

    generated_at = datetime.now(timezone.utc).isoformat()
    split_summaries: list[EncodedSplitSummary] = []

    connection = duckdb.connect(database=":memory:")
    try:
        for split_name, input_path in input_paths.items():
            input_relation_sql = _relation_sql(input_path)
            row_count = _write_encoded_dataset(
                connection=connection,
                input_relation_sql=input_relation_sql,
                projection_items=projection_items,
                output_path=output_paths[split_name],
            )
            encoded_relation_sql = _relation_sql(output_paths[split_name])
            metric_query = _build_metric_query(
                relation_sql=encoded_relation_sql,
                encoded_feature_columns=encoded_feature_columns,
                target_id_column=target_id_column,
            )
            cursor = connection.execute(metric_query)
            metric_values = cursor.fetchone()
            metric_columns = [description[0] for description in cursor.description]
            metric_row = dict(zip(metric_columns, metric_values, strict=True))
            target_id_null_count = int(metric_row["target_id_null_count"])
            encoded_feature_metrics = _compute_encoded_feature_metrics(
                metric_row=metric_row,
                encoded_feature_columns=encoded_feature_columns,
                row_count=row_count,
            )
            split_summaries.append(
                EncodedSplitSummary(
                    split_name=split_name,
                    input_path=str(input_path),
                    output_path=str(output_paths[split_name]),
                    row_count=row_count,
                    target_id_null_count=target_id_null_count,
                    encoded_feature_metrics=encoded_feature_metrics,
                )
            )
    finally:
        connection.close()

    failure_reasons = _evaluate_encoded_features(split_summaries)
    result = EncodedFeatureResult(
        target_column=target_column,
        target_id_column=target_id_column,
        target_mapping=target_mapping,
        numeric_feature_columns=numeric_feature_columns,
        categorical_feature_columns=categorical_feature_columns,
        encoded_feature_columns=encoded_feature_columns,
        audit_passed=not failure_reasons,
        failure_reasons=failure_reasons,
        split_summaries=split_summaries,
    )

    report_path = _write_encoded_feature_report(root, result, generated_at)
    manifest_path = root / "metadata" / "manifests" / "encoded_feature_manifest.json"
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
                            if key != "encoded_feature_metrics"
                        },
                        "encoded_feature_metrics": [
                            asdict(metric) for metric in summary.encoded_feature_metrics
                        ],
                    }
                    for summary in result.split_summaries
                ],
            },
        },
    )

    return 0 if result.audit_passed else 1
