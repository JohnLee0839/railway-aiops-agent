from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from stsrs_data_engineering.config import DATASET_SPECS, ensure_parent, load_pipeline_config, write_json


@dataclass
class DatasetKeyAuditResult:
    dataset_name: str
    source_name: str
    staging_path: str
    key_columns: list[str]
    row_count: int
    null_key_row_count: int
    null_key_counts_by_column: dict[str, int]
    non_null_key_row_count: int
    distinct_key_count: int
    distinct_non_null_key_count: int
    duplicate_key_group_count: int
    duplicate_key_row_count: int
    max_duplicate_key_occurrence_count: int
    key_unique: bool
    key_audit_passed: bool
    failure_reasons: list[str]
    null_key_sample_path: str
    null_key_sample_rows: int
    duplicate_key_sample_path: str
    duplicate_key_sample_rows: int


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _quote_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _relation_sql(staging_path: Path) -> str:
    return f"read_parquet({_quote_literal(staging_path.resolve().as_posix())})"


def _build_any_null_predicate(key_columns: list[str]) -> str:
    return " OR ".join(f"{_quote_identifier(column)} IS NULL" for column in key_columns)


def _build_all_non_null_predicate(key_columns: list[str]) -> str:
    return " AND ".join(f"{_quote_identifier(column)} IS NOT NULL" for column in key_columns)


def _build_key_projection(key_columns: list[str]) -> str:
    return ", ".join(_quote_identifier(column) for column in key_columns)


def _build_key_order_clause(key_columns: list[str], *, nulls_first: bool) -> str:
    nulls_sql = "NULLS FIRST" if nulls_first else "NULLS LAST"
    return ", ".join(f"{_quote_identifier(column)} {nulls_sql}" for column in key_columns)


def _build_metric_query(relation_sql: str, key_columns: list[str]) -> str:
    any_null_predicate = _build_any_null_predicate(key_columns)
    all_non_null_predicate = _build_all_non_null_predicate(key_columns)
    key_projection = _build_key_projection(key_columns)
    null_count_items = ",\n        ".join(
        (
            "SUM(CASE "
            f"WHEN {_quote_identifier(column)} IS NULL THEN 1 ELSE 0 END"
            f") AS {_quote_identifier(f'{column}__null_rows')}"
        )
        for column in key_columns
    )

    return f"""
WITH base AS (
    SELECT *
    FROM {relation_sql}
),
null_metrics AS (
    SELECT
        COUNT(*) AS row_count,
        SUM(CASE WHEN {any_null_predicate} THEN 1 ELSE 0 END) AS null_key_row_count,
        {null_count_items}
    FROM base
),
non_null_groups AS (
    SELECT
        {key_projection},
        COUNT(*) AS occurrence_count
    FROM base
    WHERE {all_non_null_predicate}
    GROUP BY {key_projection}
),
duplicate_metrics AS (
    SELECT
        COUNT(*) AS distinct_non_null_key_count,
        SUM(CASE WHEN occurrence_count > 1 THEN 1 ELSE 0 END) AS duplicate_key_group_count,
        SUM(CASE WHEN occurrence_count > 1 THEN occurrence_count ELSE 0 END) AS duplicate_key_row_count,
        COALESCE(MAX(CASE WHEN occurrence_count > 1 THEN occurrence_count END), 0) AS max_duplicate_key_occurrence_count
    FROM non_null_groups
),
all_groups AS (
    SELECT
        {key_projection}
    FROM base
    GROUP BY {key_projection}
)
SELECT
    null_metrics.row_count,
    null_metrics.null_key_row_count,
    {", ".join(f"null_metrics.{_quote_identifier(f'{column}__null_rows')}" for column in key_columns)},
    null_metrics.row_count - null_metrics.null_key_row_count AS non_null_key_row_count,
    (SELECT COUNT(*) FROM all_groups) AS distinct_key_count,
    duplicate_metrics.distinct_non_null_key_count,
    duplicate_metrics.duplicate_key_group_count,
    duplicate_metrics.duplicate_key_row_count,
    duplicate_metrics.max_duplicate_key_occurrence_count
FROM null_metrics
CROSS JOIN duplicate_metrics
"""


def _write_null_key_sample(
    connection: duckdb.DuckDBPyConnection,
    relation_sql: str,
    key_columns: list[str],
    sample_path: Path,
    sample_limit: int,
) -> int:
    ensure_parent(sample_path)
    any_null_predicate = _build_any_null_predicate(key_columns)
    order_clause = _build_key_order_clause(key_columns, nulls_first=True)
    sql = f"""
COPY (
    SELECT *
    FROM {relation_sql}
    WHERE {any_null_predicate}
    ORDER BY {order_clause}
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


def _write_duplicate_key_sample(
    connection: duckdb.DuckDBPyConnection,
    relation_sql: str,
    key_columns: list[str],
    sample_path: Path,
    sample_limit: int,
) -> int:
    ensure_parent(sample_path)
    key_projection = _build_key_projection(key_columns)
    order_clause = _build_key_order_clause(key_columns, nulls_first=False)
    sql = f"""
COPY (
    WITH duplicate_keys AS (
        SELECT
            {key_projection},
            COUNT(*) AS DuplicateKeyCount
        FROM {relation_sql}
        WHERE {_build_all_non_null_predicate(key_columns)}
        GROUP BY {key_projection}
        HAVING COUNT(*) > 1
    )
    SELECT
        duplicate_keys.DuplicateKeyCount,
        base.*
    FROM {relation_sql} AS base
    INNER JOIN duplicate_keys USING ({key_projection})
    ORDER BY duplicate_keys.DuplicateKeyCount DESC, {order_clause}
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


def _evaluate_key_result(
    null_key_row_count: int,
    duplicate_key_row_count: int,
    thresholds: dict[str, object],
) -> list[str]:
    failures: list[str] = []
    null_key_rows_max = int(thresholds["null_key_rows_max"])
    duplicate_key_rows_max = int(thresholds["duplicate_key_rows_max"])

    if null_key_row_count > null_key_rows_max:
        failures.append(
            f"Null-key row count {null_key_row_count} exceeds configured maximum {null_key_rows_max}."
        )
    if duplicate_key_row_count > duplicate_key_rows_max:
        failures.append(
            "Duplicate-key row count "
            f"{duplicate_key_row_count} exceeds configured maximum {duplicate_key_rows_max}."
        )
    return failures


def _write_key_audit_report(
    project_root: Path,
    results: list[DatasetKeyAuditResult],
    generated_at: str,
) -> Path:
    report_path = project_root / "reports" / "data_validation" / "key_audit_report.md"
    ensure_parent(report_path)

    lines = [
        "# Key Audit Report",
        "",
        f"Generated at: {generated_at}",
        "",
    ]

    for result in results:
        status = "PASS" if result.key_audit_passed else "FAIL"
        lines.extend(
            [
                f"## {result.dataset_name}",
                "",
                f"- Status: {status}",
                f"- Staging path: `{result.staging_path}`",
                f"- Candidate key: `{tuple(result.key_columns)}`",
                f"- Row count: {result.row_count}",
                f"- Distinct key count: {result.distinct_key_count}",
                f"- Distinct non-null key count: {result.distinct_non_null_key_count}",
                f"- Non-null key row count: {result.non_null_key_row_count}",
                f"- Null key row count: {result.null_key_row_count}",
                f"- Duplicate key row count: {result.duplicate_key_row_count}",
                f"- Duplicate key group count: {result.duplicate_key_group_count}",
                f"- Max duplicate key occurrence count: {result.max_duplicate_key_occurrence_count}",
                f"- Candidate key is unique: {result.key_unique}",
                f"- Null key sample path: `{result.null_key_sample_path}` ({result.null_key_sample_rows} rows)",
                f"- Duplicate key sample path: `{result.duplicate_key_sample_path}` ({result.duplicate_key_sample_rows} rows)",
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
                "### Null Key Statistics",
                "",
                "| Metric | Value |",
                "| --- | ---: |",
                f"| Rows with any null key component | {result.null_key_row_count} |",
            ]
        )
        for column_name, null_count in result.null_key_counts_by_column.items():
            lines.append(f"| Rows with `{column_name}` null | {null_count} |")
        lines.append("")

        lines.extend(
            [
                "### Duplicate Key Statistics",
                "",
                "| Metric | Value |",
                "| --- | ---: |",
                f"| Rows participating in duplicate non-null keys | {result.duplicate_key_row_count} |",
                f"| Duplicate non-null key groups | {result.duplicate_key_group_count} |",
                f"| Maximum rows for one duplicate key | {result.max_duplicate_key_occurrence_count} |",
                "",
            ]
        )

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run_key_audit(project_root: Path | None = None) -> int:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    config = load_pipeline_config(root)
    schema = config["schema"]
    quality_thresholds = config["quality_thresholds"]

    key_definition = schema["key_definition"]
    key_columns = list(key_definition["columns"])
    key_thresholds = quality_thresholds["key_validation"]
    sample_limit = 100

    generated_at = datetime.now(timezone.utc).isoformat()
    results: list[DatasetKeyAuditResult] = []

    connection = duckdb.connect(database=":memory:")
    try:
        for dataset in DATASET_SPECS:
            relation_sql = _relation_sql(dataset.staging_path)
            metric_query = _build_metric_query(relation_sql, key_columns)
            cursor = connection.execute(metric_query)
            metric_values = cursor.fetchone()
            metric_columns = [description[0] for description in cursor.description]
            metric_row = dict(zip(metric_columns, metric_values, strict=True))

            null_key_counts_by_column = {
                column: int(metric_row[f"{column}__null_rows"])
                for column in key_columns
            }
            row_count = int(metric_row["row_count"])
            null_key_row_count = int(metric_row["null_key_row_count"])
            non_null_key_row_count = int(metric_row["non_null_key_row_count"])
            distinct_key_count = int(metric_row["distinct_key_count"])
            distinct_non_null_key_count = int(metric_row["distinct_non_null_key_count"])
            duplicate_key_group_count = int(metric_row["duplicate_key_group_count"])
            duplicate_key_row_count = int(metric_row["duplicate_key_row_count"])
            max_duplicate_key_occurrence_count = int(metric_row["max_duplicate_key_occurrence_count"])

            null_sample_path = (
                root
                / "data"
                / "validated"
                / "keys"
                / f"{dataset.name}_null_key_samples.parquet"
            )
            duplicate_sample_path = (
                root
                / "data"
                / "validated"
                / "keys"
                / f"{dataset.name}_duplicate_key_samples.parquet"
            )

            null_sample_rows = _write_null_key_sample(
                connection=connection,
                relation_sql=relation_sql,
                key_columns=key_columns,
                sample_path=null_sample_path,
                sample_limit=sample_limit,
            )
            duplicate_sample_rows = _write_duplicate_key_sample(
                connection=connection,
                relation_sql=relation_sql,
                key_columns=key_columns,
                sample_path=duplicate_sample_path,
                sample_limit=sample_limit,
            )

            failures = _evaluate_key_result(
                null_key_row_count=null_key_row_count,
                duplicate_key_row_count=duplicate_key_row_count,
                thresholds=key_thresholds,
            )
            key_unique = null_key_row_count == 0 and duplicate_key_row_count == 0
            results.append(
                DatasetKeyAuditResult(
                    dataset_name=dataset.name,
                    source_name=dataset.source_name,
                    staging_path=str(dataset.staging_path),
                    key_columns=key_columns,
                    row_count=row_count,
                    null_key_row_count=null_key_row_count,
                    null_key_counts_by_column=null_key_counts_by_column,
                    non_null_key_row_count=non_null_key_row_count,
                    distinct_key_count=distinct_key_count,
                    distinct_non_null_key_count=distinct_non_null_key_count,
                    duplicate_key_group_count=duplicate_key_group_count,
                    duplicate_key_row_count=duplicate_key_row_count,
                    max_duplicate_key_occurrence_count=max_duplicate_key_occurrence_count,
                    key_unique=key_unique,
                    key_audit_passed=not failures,
                    failure_reasons=failures,
                    null_key_sample_path=str(null_sample_path),
                    null_key_sample_rows=null_sample_rows,
                    duplicate_key_sample_path=str(duplicate_sample_path),
                    duplicate_key_sample_rows=duplicate_sample_rows,
                )
            )
    finally:
        connection.close()

    report_path = _write_key_audit_report(root, results, generated_at)
    manifest_path = root / "metadata" / "manifests" / "key_audit_manifest.json"
    write_json(
        manifest_path,
        {
            "generated_at": generated_at,
            "report_path": str(report_path),
            "key_columns": key_columns,
            "thresholds": key_thresholds,
            "sample_limit": sample_limit,
            "results": [asdict(result) for result in results],
        },
    )

    return 0 if all(result.key_audit_passed for result in results) else 1
