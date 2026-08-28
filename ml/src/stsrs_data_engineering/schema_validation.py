from __future__ import annotations

import csv
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from stsrs_data_engineering.config import DATASET_SPECS, ensure_parent, load_pipeline_config, write_json


@dataclass
class ColumnMetric:
    name: str
    logical_type: str
    nullable: bool
    non_null_rows: int
    parse_success_rows: int
    parse_success_rate: float
    unexpected_value_rows: int | None


@dataclass
class DatasetValidationResult:
    dataset_name: str
    source_name: str
    raw_path: str
    staging_path: str
    file_size_bytes: int
    expected_header: list[str]
    actual_header: list[str]
    header_matches: bool
    row_count: int
    schema_passed: bool
    staging_written: bool
    failure_reasons: list[str]
    column_metrics: list[ColumnMetric]


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _quote_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _duckdb_type_name(physical_type: str) -> str:
    normalized = physical_type.strip().lower()
    mapping = {
        "float64": "DOUBLE",
        "float32": "FLOAT",
        "int64": "BIGINT",
        "int32": "INTEGER",
        "int16": "SMALLINT",
        "int8": "TINYINT",
        "string": "VARCHAR",
    }
    return mapping.get(normalized, physical_type.upper())


def _dataset_relation_sql(raw_path: Path, delimiter: str, header: bool) -> str:
    path_literal = _quote_literal(raw_path.resolve().as_posix())
    header_literal = "true" if header else "false"
    return (
        "read_csv_auto("
        f"{path_literal}, "
        f"delim={_quote_literal(delimiter)}, "
        f"header={header_literal}, "
        "all_varchar=true, "
        "sample_size=-1, "
        "ignore_errors=false"
        ")"
    )


def _read_header(raw_path: Path, delimiter: str) -> list[str]:
    with raw_path.open("r", encoding="utf-8-sig", newline="") as handle:
        first_row = next(csv.reader([handle.readline()], delimiter=delimiter))
    return first_row


def _build_parse_success_expr(column: dict[str, Any], timestamp_format: str) -> str:
    name = column["name"]
    identifier = _quote_identifier(name)
    normalized = f"NULLIF(TRIM({identifier}), '')"
    logical_type = column["logical_type"]
    physical_type = _duckdb_type_name(str(column["physical_type"]))

    if logical_type == "timestamp":
        return (
            f"SUM(CASE WHEN try_strptime({normalized}, {_quote_literal(timestamp_format)}) "
            "IS NOT NULL THEN 1 ELSE 0 END)"
        )
    if logical_type == "numeric":
        return f"SUM(CASE WHEN try_cast({normalized} AS {physical_type}) IS NOT NULL THEN 1 ELSE 0 END)"
    return f"SUM(CASE WHEN {normalized} IS NOT NULL THEN 1 ELSE 0 END)"


def _build_unexpected_value_expr(column: dict[str, Any]) -> str | None:
    allowed_values = column.get("allowed_values")
    if not allowed_values:
        return None

    name = column["name"]
    identifier = _quote_identifier(name)
    normalized = f"NULLIF(TRIM({identifier}), '')"
    allowed_sql = ", ".join(_quote_literal(value) for value in allowed_values)
    return (
        "SUM(CASE "
        f"WHEN {normalized} IS NOT NULL AND {normalized} NOT IN ({allowed_sql}) "
        "THEN 1 ELSE 0 END)"
    )


def _build_typed_select_list(columns: list[dict[str, Any]], timestamp_format: str, source_name: str) -> str:
    projected_columns: list[str] = []
    for column in columns:
        name = column["name"]
        identifier = _quote_identifier(name)
        alias = _quote_identifier(name)
        normalized = f"NULLIF(TRIM({identifier}), '')"
        logical_type = column["logical_type"]
        physical_type = _duckdb_type_name(str(column["physical_type"]))

        if logical_type == "timestamp":
            projected_columns.append(
                f"try_strptime({normalized}, {_quote_literal(timestamp_format)}) AS {alias}"
            )
        elif logical_type == "numeric":
            projected_columns.append(f"try_cast({normalized} AS {physical_type}) AS {alias}")
        else:
            projected_columns.append(f"{normalized} AS {alias}")

    projected_columns.append(f"{_quote_literal(source_name)} AS \"SourceDataset\"")
    return ",\n  ".join(projected_columns)


def _build_metric_query(
    relation_sql: str,
    columns: list[dict[str, Any]],
    timestamp_format: str,
) -> str:
    select_items = ["COUNT(*) AS row_count"]
    for column in columns:
        name = column["name"]
        identifier = _quote_identifier(name)
        normalized = f"NULLIF(TRIM({identifier}), '')"
        select_items.append(
            f"SUM(CASE WHEN {normalized} IS NOT NULL THEN 1 ELSE 0 END) AS {_quote_identifier(f'{name}__non_null')}"
        )
        select_items.append(
            f"{_build_parse_success_expr(column, timestamp_format)} AS {_quote_identifier(f'{name}__parse_success')}"
        )
        unexpected_expr = _build_unexpected_value_expr(column)
        if unexpected_expr is not None:
            select_items.append(
                f"{unexpected_expr} AS {_quote_identifier(f'{name}__unexpected')}"
            )

    select_clause = ",\n  ".join(select_items)
    return f"SELECT\n  {select_clause}\nFROM {relation_sql}"


def _compute_column_metrics(
    metrics_row: dict[str, Any],
    columns: list[dict[str, Any]],
) -> list[ColumnMetric]:
    row_count = int(metrics_row["row_count"])
    results: list[ColumnMetric] = []
    for column in columns:
        name = column["name"]
        non_null = int(metrics_row[f"{name}__non_null"])
        parse_success = int(metrics_row[f"{name}__parse_success"])
        unexpected_key = f"{name}__unexpected"
        unexpected = int(metrics_row[unexpected_key]) if unexpected_key in metrics_row else None
        success_rate = 1.0 if row_count == 0 else parse_success / row_count
        results.append(
            ColumnMetric(
                name=name,
                logical_type=column["logical_type"],
                nullable=bool(column["nullable"]),
                non_null_rows=non_null,
                parse_success_rows=parse_success,
                parse_success_rate=success_rate,
                unexpected_value_rows=unexpected,
            )
        )
    return results


def _evaluate_schema_result(
    columns: list[dict[str, Any]],
    column_metrics: list[ColumnMetric],
    quality_thresholds: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    schema_thresholds = quality_thresholds["schema_validation"]

    for column, metric in zip(columns, column_metrics, strict=True):
        if not column["nullable"] and metric.non_null_rows != metric.parse_success_rows:
            failures.append(
                f"Column {metric.name} contains null or blank values after normalization."
            )

        if metric.logical_type == "timestamp":
            min_rate = float(schema_thresholds["timestamp_parse_success_rate_min"])
        elif metric.logical_type == "numeric":
            min_rate = float(schema_thresholds["numeric_parse_success_rate_min"])
        else:
            min_rate = 1.0

        if metric.parse_success_rate < min_rate:
            failures.append(
                f"Column {metric.name} parse success rate {metric.parse_success_rate:.6f} "
                f"is below the configured minimum {min_rate:.6f}."
            )

        if metric.unexpected_value_rows:
            failures.append(
                f"Column {metric.name} contains {metric.unexpected_value_rows} unexpected categorical values."
            )

    return failures


def _write_staging_parquet(
    connection: duckdb.DuckDBPyConnection,
    relation_sql: str,
    columns: list[dict[str, Any]],
    timestamp_format: str,
    source_name: str,
    staging_path: Path,
) -> None:
    ensure_parent(staging_path)
    select_list = _build_typed_select_list(columns, timestamp_format, source_name)
    sql = (
        "COPY (\n"
        f"SELECT\n  {select_list}\n"
        f"FROM {relation_sql}\n"
        f") TO {_quote_literal(staging_path.resolve().as_posix())} "
        "(FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    connection.execute(sql)


def _format_float(value: float) -> str:
    if math.isnan(value):
        return "nan"
    return f"{value:.6f}"


def _write_schema_report(
    project_root: Path,
    results: list[DatasetValidationResult],
    generated_at: str,
) -> Path:
    report_path = project_root / "reports" / "data_validation" / "schema_report.md"
    ensure_parent(report_path)

    lines = [
        "# Schema Validation Report",
        "",
        f"Generated at: {generated_at}",
        "",
    ]

    for result in results:
        status = "PASS" if result.schema_passed else "FAIL"
        lines.extend(
            [
                f"## {result.dataset_name}",
                "",
                f"- Status: {status}",
                f"- Raw path: `{result.raw_path}`",
                f"- Staging path: `{result.staging_path}`",
                f"- File size (bytes): {result.file_size_bytes}",
                f"- Row count: {result.row_count}",
                f"- Header matches expected schema: {result.header_matches}",
                f"- Staging written: {result.staging_written}",
                "",
            ]
        )

        if result.failure_reasons:
            lines.append("### Failure Reasons")
            lines.append("")
            for reason in result.failure_reasons:
                lines.append(f"- {reason}")
            lines.append("")

        lines.extend(
            [
                "### Column Metrics",
                "",
                "| Column | Logical Type | Nullable | Non-null Rows | Parse Success Rows | Parse Success Rate | Unexpected Values |",
                "| --- | --- | --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for metric in result.column_metrics:
            unexpected = "" if metric.unexpected_value_rows is None else str(metric.unexpected_value_rows)
            lines.append(
                f"| {metric.name} | {metric.logical_type} | {metric.nullable} | "
                f"{metric.non_null_rows} | {metric.parse_success_rows} | "
                f"{_format_float(metric.parse_success_rate)} | {unexpected} |"
            )
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run_schema_to_staging(project_root: Path | None = None) -> int:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    config = load_pipeline_config(root)
    schema = config["schema"]
    quality_thresholds = config["quality_thresholds"]

    delimiter = schema["file_format"]["delimiter"]
    expected_header = [column["name"] for column in schema["columns"]]
    timestamp_format = schema["timestamp"]["format"]

    generated_at = datetime.now(timezone.utc).isoformat()
    results: list[DatasetValidationResult] = []

    connection = duckdb.connect(database=":memory:")
    try:
        for dataset in DATASET_SPECS:
            header = _read_header(dataset.raw_path, delimiter)
            header_matches = header == expected_header
            failures: list[str] = []
            if not header_matches:
                failures.append("Header does not match the configured schema exactly.")

            relation_sql = _dataset_relation_sql(dataset.raw_path, delimiter, header=True)
            row_count = 0
            column_metrics: list[ColumnMetric] = []
            staging_written = False

            if header_matches:
                metric_query = _build_metric_query(relation_sql, schema["columns"], timestamp_format)
                cursor = connection.execute(metric_query)
                metric_values = cursor.fetchone()
                metric_columns = [description[0] for description in cursor.description]
                metric_row = dict(zip(metric_columns, metric_values, strict=True))
                row_count = int(metric_row["row_count"])
                column_metrics = _compute_column_metrics(metric_row, schema["columns"])
                failures.extend(
                    _evaluate_schema_result(
                        schema["columns"],
                        column_metrics,
                        quality_thresholds,
                    )
                )
                _write_staging_parquet(
                    connection=connection,
                    relation_sql=relation_sql,
                    columns=schema["columns"],
                    timestamp_format=timestamp_format,
                    source_name=dataset.source_name,
                    staging_path=dataset.staging_path,
                )
                staging_written = True

            results.append(
                DatasetValidationResult(
                    dataset_name=dataset.name,
                    source_name=dataset.source_name,
                    raw_path=str(dataset.raw_path),
                    staging_path=str(dataset.staging_path),
                    file_size_bytes=dataset.raw_path.stat().st_size,
                    expected_header=expected_header,
                    actual_header=header,
                    header_matches=header_matches,
                    row_count=row_count,
                    schema_passed=not failures,
                    staging_written=staging_written,
                    failure_reasons=failures,
                    column_metrics=column_metrics,
                )
            )
    finally:
        connection.close()

    report_path = _write_schema_report(root, results, generated_at)
    manifest_path = root / "metadata" / "manifests" / "schema_validation_manifest.json"
    write_json(
        manifest_path,
        {
            "generated_at": generated_at,
            "report_path": str(report_path),
            "results": [
                {
                    **{
                        key: value
                        for key, value in asdict(result).items()
                        if key != "column_metrics"
                    },
                    "column_metrics": [asdict(metric) for metric in result.column_metrics],
                }
                for result in results
            ],
        },
    )

    return 0 if all(result.schema_passed for result in results) else 1
