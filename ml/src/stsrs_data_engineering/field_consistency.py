from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from stsrs_data_engineering.config import DATASET_SPECS, ensure_parent, load_pipeline_config, write_json


@dataclass
class ColumnConsistencyMetric:
    column_name: str
    logical_type: str
    match_count: int
    mismatch_count: int
    match_rate: float


@dataclass
class FieldConsistencyAuditResult:
    left_dataset_name: str
    right_dataset_name: str
    left_staging_path: str
    right_staging_path: str
    trusted_keys_path: str
    key_columns: list[str]
    compared_columns: list[str]
    excluded_columns: list[str]
    trusted_row_count: int
    conflict_row_count: int
    conflict_row_ratio: float
    default_match_rate_min: float
    audit_passed: bool
    failure_reasons: list[str]
    column_metrics: list[ColumnConsistencyMetric]
    conflict_sample_path: str
    conflict_sample_rows: int


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _quote_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _relation_sql(path: Path) -> str:
    return f"read_parquet({_quote_literal(path.resolve().as_posix())})"


def _build_key_projection(key_columns: list[str], alias: str) -> str:
    return ", ".join(f"{alias}.{_quote_identifier(column)}" for column in key_columns)


def _build_key_order_clause(key_columns: list[str], alias: str = "trusted_keys") -> str:
    return ", ".join(f"{alias}.{_quote_identifier(column)} NULLS LAST" for column in key_columns)


def _build_match_predicate(
    column: dict[str, Any],
    *,
    left_alias: str,
    right_alias: str,
    absolute_tolerance: float,
    relative_tolerance: float,
) -> str:
    column_name = column["name"]
    left_value = f"{left_alias}.{_quote_identifier(column_name)}"
    right_value = f"{right_alias}.{_quote_identifier(column_name)}"
    logical_type = column["logical_type"]

    if logical_type == "numeric":
        return (
            "("
            f"({left_value} IS NULL AND {right_value} IS NULL) "
            f"OR ({left_value} IS NOT NULL AND {right_value} IS NOT NULL AND "
            f"ABS({left_value} - {right_value}) <= {absolute_tolerance} + "
            f"{relative_tolerance} * GREATEST(ABS({left_value}), ABS({right_value})))"
            ")"
        )

    return f"({left_value} IS NOT DISTINCT FROM {right_value})"


def _build_trusted_join_sql(
    trusted_keys_sql: str,
    left_relation_sql: str,
    right_relation_sql: str,
    key_columns: list[str],
) -> str:
    trusted_key_projection = _build_key_projection(key_columns, "trusted_keys")
    key_projection = ", ".join(_quote_identifier(column) for column in key_columns)
    return f"""
SELECT
    {trusted_key_projection}
FROM {trusted_keys_sql} AS trusted_keys
INNER JOIN {left_relation_sql} AS left_rows USING ({key_projection})
INNER JOIN {right_relation_sql} AS right_rows USING ({key_projection})
"""


def _build_metric_query(
    trusted_keys_sql: str,
    left_relation_sql: str,
    right_relation_sql: str,
    key_columns: list[str],
    comparable_columns: list[dict[str, Any]],
    absolute_tolerance: float,
    relative_tolerance: float,
) -> str:
    key_projection = ", ".join(_quote_identifier(column) for column in key_columns)
    select_items = ["COUNT(*) AS trusted_row_count"]
    conflict_predicates: list[str] = []

    for column in comparable_columns:
        match_predicate = _build_match_predicate(
            column,
            left_alias="left_rows",
            right_alias="right_rows",
            absolute_tolerance=absolute_tolerance,
            relative_tolerance=relative_tolerance,
        )
        column_name = column["name"]
        select_items.append(
            f"SUM(CASE WHEN {match_predicate} THEN 1 ELSE 0 END) AS {_quote_identifier(f'{column_name}__match_count')}"
        )
        select_items.append(
            f"SUM(CASE WHEN NOT {match_predicate} THEN 1 ELSE 0 END) AS {_quote_identifier(f'{column_name}__mismatch_count')}"
        )
        conflict_predicates.append(f"NOT {match_predicate}")

    conflict_predicate = " OR ".join(conflict_predicates) if conflict_predicates else "FALSE"
    select_items.append(
        f"SUM(CASE WHEN {conflict_predicate} THEN 1 ELSE 0 END) AS conflict_row_count"
    )

    select_clause = ",\n    ".join(select_items)
    return f"""
SELECT
    {select_clause}
FROM {trusted_keys_sql} AS trusted_keys
INNER JOIN {left_relation_sql} AS left_rows USING ({key_projection})
INNER JOIN {right_relation_sql} AS right_rows USING ({key_projection})
"""


def _compute_column_metrics(
    metric_row: dict[str, Any],
    comparable_columns: list[dict[str, Any]],
    trusted_row_count: int,
) -> list[ColumnConsistencyMetric]:
    results: list[ColumnConsistencyMetric] = []
    for column in comparable_columns:
        column_name = column["name"]
        match_count = int(metric_row[f"{column_name}__match_count"])
        mismatch_count = int(metric_row[f"{column_name}__mismatch_count"])
        match_rate = 1.0 if trusted_row_count == 0 else match_count / trusted_row_count
        results.append(
            ColumnConsistencyMetric(
                column_name=column_name,
                logical_type=column["logical_type"],
                match_count=match_count,
                mismatch_count=mismatch_count,
                match_rate=match_rate,
            )
        )
    return results


def _evaluate_field_consistency(
    column_metrics: list[ColumnConsistencyMetric],
    default_match_rate_min: float,
) -> list[str]:
    failures: list[str] = []
    for metric in column_metrics:
        if metric.match_rate < default_match_rate_min:
            failures.append(
                f"Column {metric.column_name} match rate {metric.match_rate:.6f} "
                f"is below configured minimum {default_match_rate_min:.6f}."
            )
    return failures


def _build_conflict_columns_expression(
    comparable_columns: list[dict[str, Any]],
    absolute_tolerance: float,
    relative_tolerance: float,
) -> tuple[str, str]:
    conflict_count_terms: list[str] = []
    conflict_label_terms: list[str] = []

    for column in comparable_columns:
        match_predicate = _build_match_predicate(
            column,
            left_alias="left_rows",
            right_alias="right_rows",
            absolute_tolerance=absolute_tolerance,
            relative_tolerance=relative_tolerance,
        )
        column_name = column["name"]
        conflict_count_terms.append(f"CASE WHEN NOT {match_predicate} THEN 1 ELSE 0 END")
        conflict_label_terms.append(
            f"CASE WHEN NOT {match_predicate} THEN {_quote_literal(column_name)} ELSE NULL END"
        )

    conflict_count_expr = " + ".join(conflict_count_terms) if conflict_count_terms else "0"
    conflict_columns_expr = f"list_filter([{', '.join(conflict_label_terms)}], value -> value IS NOT NULL)"
    return conflict_count_expr, conflict_columns_expr


def _write_conflict_sample(
    connection: duckdb.DuckDBPyConnection,
    trusted_keys_sql: str,
    left_relation_sql: str,
    right_relation_sql: str,
    key_columns: list[str],
    comparable_columns: list[dict[str, Any]],
    absolute_tolerance: float,
    relative_tolerance: float,
    sample_path: Path,
    sample_limit: int,
) -> int:
    ensure_parent(sample_path)
    key_projection = ", ".join(_quote_identifier(column) for column in key_columns)
    order_clause = _build_key_order_clause(key_columns)
    conflict_predicates = [
        f"NOT {_build_match_predicate(column, left_alias='left_rows', right_alias='right_rows', absolute_tolerance=absolute_tolerance, relative_tolerance=relative_tolerance)}"
        for column in comparable_columns
    ]
    conflict_where_clause = " OR ".join(conflict_predicates) if conflict_predicates else "FALSE"
    conflict_count_expr, conflict_columns_expr = _build_conflict_columns_expression(
        comparable_columns,
        absolute_tolerance,
        relative_tolerance,
    )

    projected_fields: list[str] = []
    for column in comparable_columns:
        column_name = column["name"]
        identifier = _quote_identifier(column_name)
        projected_fields.append(f"left_rows.{identifier} AS {_quote_identifier(f'left__{column_name}')}")
        projected_fields.append(f"right_rows.{identifier} AS {_quote_identifier(f'right__{column_name}')}")

    sql = f"""
COPY (
    SELECT
        {', '.join(f'trusted_keys.{_quote_identifier(column)}' for column in key_columns)},
        {conflict_count_expr} AS ConflictColumnCount,
        {conflict_columns_expr} AS ConflictColumns,
        {', '.join(projected_fields)}
    FROM {trusted_keys_sql} AS trusted_keys
    INNER JOIN {left_relation_sql} AS left_rows USING ({key_projection})
    INNER JOIN {right_relation_sql} AS right_rows USING ({key_projection})
    WHERE {conflict_where_clause}
    ORDER BY ConflictColumnCount DESC, {order_clause}
    LIMIT {sample_limit}
) TO {_quote_literal(sample_path.resolve().as_posix())}
(FORMAT PARQUET, COMPRESSION ZSTD)
"""
    connection.execute(sql)
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM read_parquet({_quote_literal(sample_path.resolve().as_posix())})"
        ).fetchone()[0]
    )


def _write_field_consistency_report(
    project_root: Path,
    result: FieldConsistencyAuditResult,
    generated_at: str,
) -> Path:
    report_path = project_root / "reports" / "data_validation" / "field_consistency_report.md"
    ensure_parent(report_path)

    status = "PASS" if result.audit_passed else "FAIL"
    lines = [
        "# Field Consistency Report",
        "",
        f"Generated at: {generated_at}",
        "",
        "## Summary",
        "",
        f"- Status: {status}",
        f"- Left dataset: `{result.left_dataset_name}`",
        f"- Right dataset: `{result.right_dataset_name}`",
        f"- Left staging path: `{result.left_staging_path}`",
        f"- Right staging path: `{result.right_staging_path}`",
        f"- Trusted keys path: `{result.trusted_keys_path}`",
        f"- Candidate key: `{tuple(result.key_columns)}`",
        f"- Trusted 1:1 row count: {result.trusted_row_count}",
        f"- Conflict row count: {result.conflict_row_count}",
        f"- Conflict row ratio: {result.conflict_row_ratio:.6f}",
        f"- Compared columns: `{result.compared_columns}`",
        f"- Excluded columns: `{result.excluded_columns}`",
        f"- Default match rate minimum: {result.default_match_rate_min:.6f}",
        f"- Conflict sample path: `{result.conflict_sample_path}` ({result.conflict_sample_rows} rows)",
        "",
    ]

    if result.failure_reasons:
        lines.extend(["### Failure Reasons", ""])
        for reason in result.failure_reasons:
            lines.append(f"- {reason}")
        lines.append("")

    lines.extend(
        [
            "### Column Match Metrics",
            "",
            "| Column | Logical Type | Match Count | Mismatch Count | Match Rate |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    for metric in result.column_metrics:
        lines.append(
            f"| {metric.column_name} | {metric.logical_type} | {metric.match_count} | "
            f"{metric.mismatch_count} | {metric.match_rate:.6f} |"
        )
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run_field_consistency_audit(project_root: Path | None = None) -> int:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    config = load_pipeline_config(root)
    schema = config["schema"]
    quality_thresholds = config["quality_thresholds"]

    key_columns = list(schema["key_definition"]["columns"])
    field_consistency_config = quality_thresholds["field_consistency"]
    default_match_rate_min = float(field_consistency_config["default_match_rate_min"])
    absolute_tolerance = float(field_consistency_config["numeric_comparison"]["absolute_tolerance"])
    relative_tolerance = float(field_consistency_config["numeric_comparison"]["relative_tolerance"])
    excluded_columns = list(field_consistency_config["excluded_from_equality_check"])
    comparable_columns = [
        column
        for column in schema["columns"]
        if column["name"] not in key_columns and column["name"] not in excluded_columns
    ]
    compared_column_names = [column["name"] for column in comparable_columns]

    left_dataset, right_dataset = DATASET_SPECS
    trusted_keys_path = root / "data" / "validated" / "alignment" / "trusted_alignment_keys.parquet"
    trusted_keys_sql = _relation_sql(trusted_keys_path)
    left_relation_sql = _relation_sql(left_dataset.staging_path)
    right_relation_sql = _relation_sql(right_dataset.staging_path)
    sample_limit = 100

    generated_at = datetime.now(timezone.utc).isoformat()

    connection = duckdb.connect(database=":memory:")
    try:
        metric_query = _build_metric_query(
            trusted_keys_sql=trusted_keys_sql,
            left_relation_sql=left_relation_sql,
            right_relation_sql=right_relation_sql,
            key_columns=key_columns,
            comparable_columns=comparable_columns,
            absolute_tolerance=absolute_tolerance,
            relative_tolerance=relative_tolerance,
        )
        cursor = connection.execute(metric_query)
        metric_values = cursor.fetchone()
        metric_columns = [description[0] for description in cursor.description]
        metric_row = dict(zip(metric_columns, metric_values, strict=True))

        trusted_row_count = int(metric_row["trusted_row_count"])
        conflict_row_count = int(metric_row["conflict_row_count"])
        conflict_row_ratio = 0.0 if trusted_row_count == 0 else conflict_row_count / trusted_row_count
        column_metrics = _compute_column_metrics(
            metric_row=metric_row,
            comparable_columns=comparable_columns,
            trusted_row_count=trusted_row_count,
        )

        conflict_sample_path = (
            root / "data" / "validated" / "consistency" / "field_conflict_samples.parquet"
        )
        conflict_sample_rows = _write_conflict_sample(
            connection=connection,
            trusted_keys_sql=trusted_keys_sql,
            left_relation_sql=left_relation_sql,
            right_relation_sql=right_relation_sql,
            key_columns=key_columns,
            comparable_columns=comparable_columns,
            absolute_tolerance=absolute_tolerance,
            relative_tolerance=relative_tolerance,
            sample_path=conflict_sample_path,
            sample_limit=sample_limit,
        )

        failure_reasons = _evaluate_field_consistency(
            column_metrics=column_metrics,
            default_match_rate_min=default_match_rate_min,
        )

        result = FieldConsistencyAuditResult(
            left_dataset_name=left_dataset.name,
            right_dataset_name=right_dataset.name,
            left_staging_path=str(left_dataset.staging_path),
            right_staging_path=str(right_dataset.staging_path),
            trusted_keys_path=str(trusted_keys_path),
            key_columns=key_columns,
            compared_columns=compared_column_names,
            excluded_columns=excluded_columns,
            trusted_row_count=trusted_row_count,
            conflict_row_count=conflict_row_count,
            conflict_row_ratio=conflict_row_ratio,
            default_match_rate_min=default_match_rate_min,
            audit_passed=not failure_reasons,
            failure_reasons=failure_reasons,
            column_metrics=column_metrics,
            conflict_sample_path=str(conflict_sample_path),
            conflict_sample_rows=conflict_sample_rows,
        )
    finally:
        connection.close()

    report_path = _write_field_consistency_report(root, result, generated_at)
    manifest_path = root / "metadata" / "manifests" / "field_consistency_manifest.json"
    write_json(
        manifest_path,
        {
            "generated_at": generated_at,
            "report_path": str(report_path),
            "sample_limit": sample_limit,
            "numeric_tolerance": {
                "absolute_tolerance": absolute_tolerance,
                "relative_tolerance": relative_tolerance,
            },
            "result": {
                **{
                    key: value
                    for key, value in asdict(result).items()
                    if key != "column_metrics"
                },
                "column_metrics": [asdict(metric) for metric in result.column_metrics],
            },
        },
    )

    return 0 if result.audit_passed else 1
