from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from stsrs_data_engineering.config import DATASET_SPECS, ensure_parent, load_pipeline_config, write_json


@dataclass
class AmbiguousPatternMetric:
    left_row_count: int
    right_row_count: int
    key_count: int


@dataclass
class AlignmentAuditResult:
    left_dataset_name: str
    right_dataset_name: str
    left_staging_path: str
    right_staging_path: str
    key_columns: list[str]
    total_distinct_key_count: int
    left_distinct_key_count: int
    right_distinct_key_count: int
    overlapping_key_count: int
    trusted_key_count: int
    left_only_key_count: int
    right_only_key_count: int
    ambiguous_key_count: int
    trusted_key_ratio: float
    left_only_ratio: float
    right_only_ratio: float
    ambiguous_ratio: float
    trusted_key_requires_left_count: int
    trusted_key_requires_right_count: int
    alignment_passed: bool
    failure_reasons: list[str]
    ambiguous_patterns: list[AmbiguousPatternMetric]
    trusted_keys_path: str
    trusted_keys_rows: int
    left_only_sample_path: str
    left_only_sample_rows: int
    right_only_sample_path: str
    right_only_sample_rows: int
    ambiguous_sample_path: str
    ambiguous_sample_rows: int


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _quote_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _relation_sql(path: Path) -> str:
    return f"read_parquet({_quote_literal(path.resolve().as_posix())})"


def _build_key_projection(key_columns: list[str], table_alias: str | None = None) -> str:
    prefix = "" if table_alias is None else f"{table_alias}."
    return ", ".join(f"{prefix}{_quote_identifier(column)}" for column in key_columns)


def _build_key_order_clause(key_columns: list[str]) -> str:
    return ", ".join(f"{_quote_identifier(column)} NULLS LAST" for column in key_columns)


def _build_joined_key_counts_sql(
    left_relation_sql: str,
    right_relation_sql: str,
    key_columns: list[str],
) -> str:
    key_projection = _build_key_projection(key_columns)
    left_projection = _build_key_projection(key_columns, table_alias="left_keys")
    right_projection = _build_key_projection(key_columns, table_alias="right_keys")
    coalesced_projection = ",\n        ".join(
        (
            f"COALESCE(left_keys.{_quote_identifier(column)}, "
            f"right_keys.{_quote_identifier(column)}) AS {_quote_identifier(column)}"
        )
        for column in key_columns
    )
    return f"""
WITH left_keys AS (
    SELECT
        {key_projection},
        COUNT(*) AS left_row_count
    FROM {left_relation_sql}
    GROUP BY {key_projection}
),
right_keys AS (
    SELECT
        {key_projection},
        COUNT(*) AS right_row_count
    FROM {right_relation_sql}
    GROUP BY {key_projection}
)
SELECT
    {coalesced_projection},
    left_keys.left_row_count,
    right_keys.right_row_count
FROM left_keys
FULL OUTER JOIN right_keys USING ({key_projection})
"""


def _build_metric_query(
    left_relation_sql: str,
    right_relation_sql: str,
    key_columns: list[str],
    trusted_left_count: int,
    trusted_right_count: int,
) -> str:
    joined_sql = _build_joined_key_counts_sql(left_relation_sql, right_relation_sql, key_columns)
    return f"""
WITH joined_keys AS (
    {joined_sql}
)
SELECT
    COUNT(*) AS total_distinct_key_count,
    SUM(CASE WHEN left_row_count IS NOT NULL THEN 1 ELSE 0 END) AS left_distinct_key_count,
    SUM(CASE WHEN right_row_count IS NOT NULL THEN 1 ELSE 0 END) AS right_distinct_key_count,
    SUM(CASE WHEN left_row_count IS NOT NULL AND right_row_count IS NOT NULL THEN 1 ELSE 0 END) AS overlapping_key_count,
    SUM(
        CASE
            WHEN left_row_count = {trusted_left_count} AND right_row_count = {trusted_right_count}
            THEN 1 ELSE 0
        END
    ) AS trusted_key_count,
    SUM(CASE WHEN left_row_count IS NOT NULL AND right_row_count IS NULL THEN 1 ELSE 0 END) AS left_only_key_count,
    SUM(CASE WHEN left_row_count IS NULL AND right_row_count IS NOT NULL THEN 1 ELSE 0 END) AS right_only_key_count,
    SUM(
        CASE
            WHEN left_row_count IS NOT NULL
             AND right_row_count IS NOT NULL
             AND NOT (
                left_row_count = {trusted_left_count}
                AND right_row_count = {trusted_right_count}
             )
            THEN 1 ELSE 0
        END
    ) AS ambiguous_key_count
FROM joined_keys
"""


def _build_ambiguous_pattern_query(
    left_relation_sql: str,
    right_relation_sql: str,
    key_columns: list[str],
    trusted_left_count: int,
    trusted_right_count: int,
) -> str:
    joined_sql = _build_joined_key_counts_sql(left_relation_sql, right_relation_sql, key_columns)
    return f"""
WITH joined_keys AS (
    {joined_sql}
)
SELECT
    left_row_count,
    right_row_count,
    COUNT(*) AS key_count
FROM joined_keys
WHERE left_row_count IS NOT NULL
  AND right_row_count IS NOT NULL
  AND NOT (
      left_row_count = {trusted_left_count}
      AND right_row_count = {trusted_right_count}
  )
GROUP BY left_row_count, right_row_count
ORDER BY key_count DESC, left_row_count, right_row_count
"""


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _evaluate_alignment_result(
    left_only_ratio: float,
    right_only_ratio: float,
    ambiguous_ratio: float,
    thresholds: dict[str, object],
) -> list[str]:
    failures: list[str] = []
    left_only_warn_max = float(thresholds["left_only_ratio_warn_max"])
    right_only_warn_max = float(thresholds["right_only_ratio_warn_max"])
    ambiguous_warn_max = float(thresholds["ambiguous_ratio_warn_max"])

    if left_only_ratio > left_only_warn_max:
        failures.append(
            f"Left-only key ratio {left_only_ratio:.6f} exceeds configured maximum {left_only_warn_max:.6f}."
        )
    if right_only_ratio > right_only_warn_max:
        failures.append(
            f"Right-only key ratio {right_only_ratio:.6f} exceeds configured maximum {right_only_warn_max:.6f}."
        )
    if ambiguous_ratio > ambiguous_warn_max:
        failures.append(
            f"Ambiguous key ratio {ambiguous_ratio:.6f} exceeds configured maximum {ambiguous_warn_max:.6f}."
        )
    return failures


def _write_trusted_keys_output(
    connection: duckdb.DuckDBPyConnection,
    left_relation_sql: str,
    right_relation_sql: str,
    key_columns: list[str],
    trusted_left_count: int,
    trusted_right_count: int,
    output_path: Path,
) -> int:
    ensure_parent(output_path)
    order_clause = _build_key_order_clause(key_columns)
    joined_sql = _build_joined_key_counts_sql(left_relation_sql, right_relation_sql, key_columns)
    sql = f"""
COPY (
    WITH joined_keys AS (
        {joined_sql}
    )
    SELECT *
    FROM joined_keys
    WHERE left_row_count = {trusted_left_count}
      AND right_row_count = {trusted_right_count}
    ORDER BY {order_clause}
) TO {_quote_literal(output_path.resolve().as_posix())}
(FORMAT PARQUET, COMPRESSION ZSTD)
"""
    connection.execute(sql)
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM read_parquet({_quote_literal(output_path.resolve().as_posix())})"
        ).fetchone()[0]
    )


def _write_key_class_sample(
    connection: duckdb.DuckDBPyConnection,
    left_relation_sql: str,
    right_relation_sql: str,
    key_columns: list[str],
    output_path: Path,
    sample_limit: int,
    where_clause: str,
) -> int:
    ensure_parent(output_path)
    order_clause = _build_key_order_clause(key_columns)
    joined_sql = _build_joined_key_counts_sql(left_relation_sql, right_relation_sql, key_columns)
    sql = f"""
COPY (
    WITH joined_keys AS (
        {joined_sql}
    )
    SELECT *
    FROM joined_keys
    WHERE {where_clause}
    ORDER BY {order_clause}
    LIMIT {sample_limit}
) TO {_quote_literal(output_path.resolve().as_posix())}
(FORMAT PARQUET, COMPRESSION ZSTD)
"""
    connection.execute(sql)
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM read_parquet({_quote_literal(output_path.resolve().as_posix())})"
        ).fetchone()[0]
    )


def _write_alignment_report(
    project_root: Path,
    result: AlignmentAuditResult,
    generated_at: str,
) -> Path:
    report_path = project_root / "reports" / "data_validation" / "alignment_audit_report.md"
    ensure_parent(report_path)

    status = "PASS" if result.alignment_passed else "FAIL"
    lines = [
        "# Alignment Audit Report",
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
        f"- Candidate key: `{tuple(result.key_columns)}`",
        f"- Trusted match definition: left count = {result.trusted_key_requires_left_count}, right count = {result.trusted_key_requires_right_count}",
        f"- Total distinct keys across both datasets: {result.total_distinct_key_count}",
        f"- Left distinct key count: {result.left_distinct_key_count}",
        f"- Right distinct key count: {result.right_distinct_key_count}",
        f"- Overlapping key count: {result.overlapping_key_count}",
        f"- Trusted 1:1 key count: {result.trusted_key_count}",
        f"- Left-only key count: {result.left_only_key_count}",
        f"- Right-only key count: {result.right_only_key_count}",
        f"- Ambiguous key count: {result.ambiguous_key_count}",
        f"- Trusted key ratio: {result.trusted_key_ratio:.6f}",
        f"- Left-only key ratio: {result.left_only_ratio:.6f}",
        f"- Right-only key ratio: {result.right_only_ratio:.6f}",
        f"- Ambiguous key ratio: {result.ambiguous_ratio:.6f}",
        f"- Trusted keys path: `{result.trusted_keys_path}` ({result.trusted_keys_rows} rows)",
        f"- Left-only sample path: `{result.left_only_sample_path}` ({result.left_only_sample_rows} rows)",
        f"- Right-only sample path: `{result.right_only_sample_path}` ({result.right_only_sample_rows} rows)",
        f"- Ambiguous sample path: `{result.ambiguous_sample_path}` ({result.ambiguous_sample_rows} rows)",
        "",
    ]

    if result.failure_reasons:
        lines.extend(["### Failure Reasons", ""])
        for reason in result.failure_reasons:
            lines.append(f"- {reason}")
        lines.append("")

    lines.extend(
        [
            "### Alignment Class Statistics",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
            f"| Trusted 1:1 keys | {result.trusted_key_count} |",
            f"| Left-only keys | {result.left_only_key_count} |",
            f"| Right-only keys | {result.right_only_key_count} |",
            f"| Ambiguous keys | {result.ambiguous_key_count} |",
            "",
        ]
    )

    lines.extend(
        [
            "### Ambiguous Pattern Breakdown",
            "",
            "| Left Row Count | Right Row Count | Key Count |",
            "| ---: | ---: | ---: |",
        ]
    )
    if result.ambiguous_patterns:
        for pattern in result.ambiguous_patterns:
            lines.append(
                f"| {pattern.left_row_count} | {pattern.right_row_count} | {pattern.key_count} |"
            )
    else:
        lines.append("|  |  | 0 |")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run_alignment_audit(project_root: Path | None = None) -> int:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    config = load_pipeline_config(root)
    schema = config["schema"]
    quality_thresholds = config["quality_thresholds"]

    key_columns = list(schema["key_definition"]["columns"])
    thresholds = quality_thresholds["alignment_validation"]
    trusted_left_count = int(thresholds["trusted_key_requires_left_count"])
    trusted_right_count = int(thresholds["trusted_key_requires_right_count"])
    sample_limit = 100

    left_dataset, right_dataset = DATASET_SPECS
    left_relation_sql = _relation_sql(left_dataset.staging_path)
    right_relation_sql = _relation_sql(right_dataset.staging_path)

    generated_at = datetime.now(timezone.utc).isoformat()

    connection = duckdb.connect(database=":memory:")
    try:
        metric_query = _build_metric_query(
            left_relation_sql=left_relation_sql,
            right_relation_sql=right_relation_sql,
            key_columns=key_columns,
            trusted_left_count=trusted_left_count,
            trusted_right_count=trusted_right_count,
        )
        cursor = connection.execute(metric_query)
        metric_values = cursor.fetchone()
        metric_columns = [description[0] for description in cursor.description]
        metrics = dict(zip(metric_columns, metric_values, strict=True))

        ambiguous_pattern_query = _build_ambiguous_pattern_query(
            left_relation_sql=left_relation_sql,
            right_relation_sql=right_relation_sql,
            key_columns=key_columns,
            trusted_left_count=trusted_left_count,
            trusted_right_count=trusted_right_count,
        )
        ambiguous_patterns = [
            AmbiguousPatternMetric(
                left_row_count=int(row[0]),
                right_row_count=int(row[1]),
                key_count=int(row[2]),
            )
            for row in connection.execute(ambiguous_pattern_query).fetchall()
        ]

        total_distinct_key_count = int(metrics["total_distinct_key_count"])
        left_distinct_key_count = int(metrics["left_distinct_key_count"])
        right_distinct_key_count = int(metrics["right_distinct_key_count"])
        overlapping_key_count = int(metrics["overlapping_key_count"])
        trusted_key_count = int(metrics["trusted_key_count"])
        left_only_key_count = int(metrics["left_only_key_count"])
        right_only_key_count = int(metrics["right_only_key_count"])
        ambiguous_key_count = int(metrics["ambiguous_key_count"])

        trusted_key_ratio = _ratio(trusted_key_count, total_distinct_key_count)
        left_only_ratio = _ratio(left_only_key_count, total_distinct_key_count)
        right_only_ratio = _ratio(right_only_key_count, total_distinct_key_count)
        ambiguous_ratio = _ratio(ambiguous_key_count, total_distinct_key_count)

        trusted_keys_path = (
            root / "data" / "validated" / "alignment" / "trusted_alignment_keys.parquet"
        )
        left_only_sample_path = (
            root / "data" / "validated" / "alignment" / "left_only_key_samples.parquet"
        )
        right_only_sample_path = (
            root / "data" / "validated" / "alignment" / "right_only_key_samples.parquet"
        )
        ambiguous_sample_path = (
            root / "data" / "validated" / "alignment" / "ambiguous_key_samples.parquet"
        )

        trusted_keys_rows = _write_trusted_keys_output(
            connection=connection,
            left_relation_sql=left_relation_sql,
            right_relation_sql=right_relation_sql,
            key_columns=key_columns,
            trusted_left_count=trusted_left_count,
            trusted_right_count=trusted_right_count,
            output_path=trusted_keys_path,
        )
        left_only_sample_rows = _write_key_class_sample(
            connection=connection,
            left_relation_sql=left_relation_sql,
            right_relation_sql=right_relation_sql,
            key_columns=key_columns,
            output_path=left_only_sample_path,
            sample_limit=sample_limit,
            where_clause="left_row_count IS NOT NULL AND right_row_count IS NULL",
        )
        right_only_sample_rows = _write_key_class_sample(
            connection=connection,
            left_relation_sql=left_relation_sql,
            right_relation_sql=right_relation_sql,
            key_columns=key_columns,
            output_path=right_only_sample_path,
            sample_limit=sample_limit,
            where_clause="left_row_count IS NULL AND right_row_count IS NOT NULL",
        )
        ambiguous_sample_rows = _write_key_class_sample(
            connection=connection,
            left_relation_sql=left_relation_sql,
            right_relation_sql=right_relation_sql,
            key_columns=key_columns,
            output_path=ambiguous_sample_path,
            sample_limit=sample_limit,
            where_clause=(
                "left_row_count IS NOT NULL "
                "AND right_row_count IS NOT NULL "
                f"AND NOT (left_row_count = {trusted_left_count} AND right_row_count = {trusted_right_count})"
            ),
        )

        failure_reasons = _evaluate_alignment_result(
            left_only_ratio=left_only_ratio,
            right_only_ratio=right_only_ratio,
            ambiguous_ratio=ambiguous_ratio,
            thresholds=thresholds,
        )

        result = AlignmentAuditResult(
            left_dataset_name=left_dataset.name,
            right_dataset_name=right_dataset.name,
            left_staging_path=str(left_dataset.staging_path),
            right_staging_path=str(right_dataset.staging_path),
            key_columns=key_columns,
            total_distinct_key_count=total_distinct_key_count,
            left_distinct_key_count=left_distinct_key_count,
            right_distinct_key_count=right_distinct_key_count,
            overlapping_key_count=overlapping_key_count,
            trusted_key_count=trusted_key_count,
            left_only_key_count=left_only_key_count,
            right_only_key_count=right_only_key_count,
            ambiguous_key_count=ambiguous_key_count,
            trusted_key_ratio=trusted_key_ratio,
            left_only_ratio=left_only_ratio,
            right_only_ratio=right_only_ratio,
            ambiguous_ratio=ambiguous_ratio,
            trusted_key_requires_left_count=trusted_left_count,
            trusted_key_requires_right_count=trusted_right_count,
            alignment_passed=not failure_reasons,
            failure_reasons=failure_reasons,
            ambiguous_patterns=ambiguous_patterns,
            trusted_keys_path=str(trusted_keys_path),
            trusted_keys_rows=trusted_keys_rows,
            left_only_sample_path=str(left_only_sample_path),
            left_only_sample_rows=left_only_sample_rows,
            right_only_sample_path=str(right_only_sample_path),
            right_only_sample_rows=right_only_sample_rows,
            ambiguous_sample_path=str(ambiguous_sample_path),
            ambiguous_sample_rows=ambiguous_sample_rows,
        )
    finally:
        connection.close()

    report_path = _write_alignment_report(root, result, generated_at)
    manifest_path = root / "metadata" / "manifests" / "alignment_audit_manifest.json"
    write_json(
        manifest_path,
        {
            "generated_at": generated_at,
            "report_path": str(report_path),
            "sample_limit": sample_limit,
            "thresholds": thresholds,
            "result": {
                **{
                    key: value
                    for key, value in asdict(result).items()
                    if key != "ambiguous_patterns"
                },
                "ambiguous_patterns": [asdict(pattern) for pattern in result.ambiguous_patterns],
            },
        },
    )

    return 0 if result.alignment_passed else 1
