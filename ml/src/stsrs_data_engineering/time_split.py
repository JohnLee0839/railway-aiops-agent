from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import floor
from pathlib import Path

import duckdb

from stsrs_data_engineering.config import ensure_parent, load_pipeline_config, write_json


@dataclass
class SplitSummary:
    split_name: str
    row_count: int
    row_ratio: float
    min_timestamp: str | None
    max_timestamp: str | None
    output_path: str


@dataclass
class TimeSplitResult:
    input_dataset_path: str
    target_column: str
    sort_by: list[str]
    total_input_rows: int
    purged_row_count: int
    purge_gap_enabled: bool
    purge_gap_seconds: int
    train_boundary_timestamp: str | None
    validation_boundary_timestamp: str | None
    split_summaries: list[SplitSummary]


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _quote_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _relation_sql(path: Path) -> str:
    return f"read_parquet({_quote_literal(path.resolve().as_posix())})"


def _build_order_clause(sort_by: list[str]) -> str:
    return ", ".join(_quote_identifier(column) for column in sort_by)


def _build_interval_sql(seconds: int) -> str:
    return f"INTERVAL {seconds} SECOND"


def _write_split_dataset(
    connection: duckdb.DuckDBPyConnection,
    split_assignment_sql: str,
    split_name: str,
    output_path: Path,
    order_clause: str,
) -> None:
    ensure_parent(output_path)
    sql = f"""
COPY (
    SELECT *
    FROM ({split_assignment_sql})
    WHERE SplitName = {_quote_literal(split_name)}
    ORDER BY {order_clause}
) TO {_quote_literal(output_path.resolve().as_posix())}
(FORMAT PARQUET, COMPRESSION ZSTD)
"""
    connection.execute(sql)


def _write_time_split_report(
    project_root: Path,
    result: TimeSplitResult,
    generated_at: str,
) -> Path:
    report_path = project_root / "reports" / "data_validation" / "time_split_report.md"
    ensure_parent(report_path)

    lines = [
        "# Time Split Report",
        "",
        f"Generated at: {generated_at}",
        "",
        "## Summary",
        "",
        f"- Input dataset path: `{result.input_dataset_path}`",
        f"- Target column: `{result.target_column}`",
        f"- Sort order: `{result.sort_by}`",
        f"- Total input rows: {result.total_input_rows}",
        f"- Purged row count: {result.purged_row_count}",
        f"- Purge gap enabled: {result.purge_gap_enabled}",
        f"- Purge gap seconds: {result.purge_gap_seconds}",
        f"- Train boundary timestamp: `{result.train_boundary_timestamp}`",
        f"- Validation boundary timestamp: `{result.validation_boundary_timestamp}`",
        "",
        "### Split Counts",
        "",
        "| Split | Row Count | Row Ratio | Min Timestamp | Max Timestamp | Output Path |",
        "| --- | ---: | ---: | --- | --- | --- |",
    ]

    for summary in result.split_summaries:
        lines.append(
            f"| {summary.split_name} | {summary.row_count} | {summary.row_ratio:.6f} | "
            f"{summary.min_timestamp or ''} | {summary.max_timestamp or ''} | `{summary.output_path}` |"
        )
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run_time_split(project_root: Path | None = None) -> int:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    config = load_pipeline_config(root)
    split_policy = config["split_policy"]

    input_dataset_path = root / "data" / "validated" / "merged" / "trusted_labeled_dataset.parquet"
    input_relation_sql = _relation_sql(input_dataset_path)
    target_column = str(split_policy["target_column"])
    sort_by = list(split_policy["sort_by"])
    order_clause = _build_order_clause(sort_by)

    train_ratio = float(split_policy["ratios"]["train"])
    validation_ratio = float(split_policy["ratios"]["validation"])

    purge_gap_enabled = bool(split_policy["purge_gap"]["enabled"])
    purge_gap_unit = str(split_policy["purge_gap"]["unit"])
    purge_gap_value = int(split_policy["purge_gap"]["value"])
    if purge_gap_unit != "seconds":
        raise ValueError(f"Unsupported purge gap unit: {purge_gap_unit}")
    purge_gap_seconds = purge_gap_value if purge_gap_enabled else 0
    interval_sql = _build_interval_sql(purge_gap_seconds)

    generated_at = datetime.now(timezone.utc).isoformat()

    connection = duckdb.connect(database=":memory:")
    try:
        total_input_rows = int(connection.execute(f"SELECT COUNT(*) FROM {input_relation_sql}").fetchone()[0])
        if total_input_rows == 0:
            raise ValueError("The trusted labeled dataset is empty; cannot perform time split.")

        train_target_rows = floor(total_input_rows * train_ratio)
        validation_target_rows = floor(total_input_rows * validation_ratio)
        train_boundary_row = min(train_target_rows + 1, total_input_rows)
        validation_boundary_row = min(train_target_rows + validation_target_rows + 1, total_input_rows)

        boundary_row = connection.execute(
            f"""
WITH ordered AS (
    SELECT
        {_quote_identifier('Timestamp')} AS Timestamp,
        ROW_NUMBER() OVER (ORDER BY {order_clause}) AS rn
    FROM {input_relation_sql}
)
SELECT
    MAX(CASE WHEN rn = {train_boundary_row} THEN Timestamp END) AS train_boundary_timestamp,
    MAX(CASE WHEN rn = {validation_boundary_row} THEN Timestamp END) AS validation_boundary_timestamp
FROM ordered
"""
        ).fetchone()
        train_boundary_timestamp = boundary_row[0]
        validation_boundary_timestamp = boundary_row[1]

        if purge_gap_enabled:
            split_name_expr = f"""
CASE
    WHEN {_quote_identifier('Timestamp')} < TIMESTAMP {_quote_literal(str(train_boundary_timestamp))} - {interval_sql}
        THEN 'train'
    WHEN {_quote_identifier('Timestamp')} > TIMESTAMP {_quote_literal(str(train_boundary_timestamp))} + {interval_sql}
         AND {_quote_identifier('Timestamp')} < TIMESTAMP {_quote_literal(str(validation_boundary_timestamp))} - {interval_sql}
        THEN 'validation'
    WHEN {_quote_identifier('Timestamp')} > TIMESTAMP {_quote_literal(str(validation_boundary_timestamp))} + {interval_sql}
        THEN 'test'
    ELSE NULL
END
"""
        else:
            split_name_expr = f"""
CASE
    WHEN ROW_NUMBER() OVER (ORDER BY {order_clause}) <= {train_target_rows} THEN 'train'
    WHEN ROW_NUMBER() OVER (ORDER BY {order_clause}) <= {train_target_rows + validation_target_rows} THEN 'validation'
    ELSE 'test'
END
"""

        split_assignment_sql = f"""
SELECT
    base.*,
    {split_name_expr} AS SplitName
FROM {input_relation_sql} AS base
"""

        output_names = split_policy["output_names"]
        split_output_paths = {
            "train": root / "data" / "serving" / "train" / str(output_names["train"]),
            "validation": root / "data" / "serving" / "validation" / str(output_names["validation"]),
            "test": root / "data" / "serving" / "test" / str(output_names["test"]),
        }
        for split_name, output_path in split_output_paths.items():
            _write_split_dataset(
                connection=connection,
                split_assignment_sql=split_assignment_sql,
                split_name=split_name,
                output_path=output_path,
                order_clause=order_clause,
            )

        split_summary_rows = connection.execute(
            f"""
SELECT
    SplitName,
    COUNT(*) AS row_count,
    MIN({_quote_identifier('Timestamp')}) AS min_timestamp,
    MAX({_quote_identifier('Timestamp')}) AS max_timestamp
FROM ({split_assignment_sql})
WHERE SplitName IS NOT NULL
GROUP BY SplitName
"""
        ).fetchall()
        split_summary_by_name = {
            str(row[0]): {
                "row_count": int(row[1]),
                "min_timestamp": None if row[2] is None else str(row[2]),
                "max_timestamp": None if row[3] is None else str(row[3]),
            }
            for row in split_summary_rows
        }

        assigned_row_count = sum(item["row_count"] for item in split_summary_by_name.values())
        purged_row_count = total_input_rows - assigned_row_count

        split_summaries = [
            SplitSummary(
                split_name=split_name,
                row_count=split_summary_by_name.get(split_name, {}).get("row_count", 0),
                row_ratio=0.0
                if total_input_rows == 0
                else split_summary_by_name.get(split_name, {}).get("row_count", 0) / total_input_rows,
                min_timestamp=split_summary_by_name.get(split_name, {}).get("min_timestamp"),
                max_timestamp=split_summary_by_name.get(split_name, {}).get("max_timestamp"),
                output_path=str(output_path),
            )
            for split_name, output_path in split_output_paths.items()
        ]

        result = TimeSplitResult(
            input_dataset_path=str(input_dataset_path),
            target_column=target_column,
            sort_by=sort_by,
            total_input_rows=total_input_rows,
            purged_row_count=purged_row_count,
            purge_gap_enabled=purge_gap_enabled,
            purge_gap_seconds=purge_gap_seconds,
            train_boundary_timestamp=None if train_boundary_timestamp is None else str(train_boundary_timestamp),
            validation_boundary_timestamp=None if validation_boundary_timestamp is None else str(validation_boundary_timestamp),
            split_summaries=split_summaries,
        )
    finally:
        connection.close()

    report_path = _write_time_split_report(root, result, generated_at)
    manifest_path = root / "metadata" / "manifests" / "time_split_manifest.json"
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
                "split_summaries": [asdict(summary) for summary in result.split_summaries],
            },
        },
    )
    return 0
