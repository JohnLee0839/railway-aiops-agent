from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from stsrs_data_engineering.config import DATASET_SPECS, ensure_parent, load_pipeline_config, write_json


@dataclass
class LabelDistributionMetric:
    label: str
    row_count: int
    row_ratio: float


@dataclass
class LabelQualityAuditResult:
    left_dataset_name: str
    right_dataset_name: str
    trusted_keys_path: str
    merged_dataset_path: str
    unknown_label_rows_path: str
    source_column: str
    raw_backup_column: str
    target_column: str
    key_columns: list[str]
    allowed_targets: list[str]
    trusted_row_count: int
    merged_row_count: int
    unknown_label_row_count: int
    unknown_label_ratio: float
    audit_passed: bool
    failure_reasons: list[str]
    label_distribution: list[LabelDistributionMetric]


def _quote_identifier(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _quote_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _relation_sql(path: Path) -> str:
    return f"read_parquet({_quote_literal(path.resolve().as_posix())})"


def _build_normalized_label_source_expr(source_expr: str, label_config: dict[str, object]) -> str:
    normalization = label_config["normalization"]
    expr = source_expr

    if normalization.get("trim_whitespace", False):
        expr = f"TRIM({expr})"
    if normalization.get("collapse_internal_spaces", False):
        expr = f"regexp_replace({expr}, {_quote_literal(r'\\s+')}, ' ', 'g')"

    delimiter = normalization.get("delimiter")
    if delimiter:
        escaped_delimiter = delimiter.replace("\\", "\\\\")
        expr = (
            f"regexp_replace({expr}, "
            f"{_quote_literal(r'\\s*' + escaped_delimiter + r'\\s*')}, "
            f"{_quote_literal(str(delimiter))}, 'g')"
        )

    case_policy = normalization.get("case_policy", "preserve")
    if case_policy == "lower":
        expr = f"LOWER({expr})"
    elif case_policy == "upper":
        expr = f"UPPER({expr})"

    return expr


def _build_label_case_expr(normalized_expr: str, label_config: dict[str, object]) -> str:
    mappings: dict[str, str] = label_config["mappings"]
    when_clauses = [
        f"WHEN {normalized_expr} = {_quote_literal(source_label)} THEN {_quote_literal(target_label)}"
        for source_label, target_label in mappings.items()
    ]
    return "CASE " + " ".join(when_clauses) + " ELSE NULL END"


def _build_trusted_base_sql(
    trusted_keys_sql: str,
    left_relation_sql: str,
    right_relation_sql: str,
    key_columns: list[str],
    schema_columns: list[dict[str, object]],
    label_config: dict[str, object],
) -> str:
    source_column = str(label_config["source_column"])
    raw_backup_column = str(label_config["raw_backup_column"])
    source_expr = f"left_rows.{_quote_identifier(source_column)}"
    normalized_source_expr = _build_normalized_label_source_expr(source_expr, label_config)
    target_expr = _build_label_case_expr(normalized_source_expr, label_config)

    projected_columns: list[str] = [
        f"trusted_keys.{_quote_identifier(column)}" for column in key_columns
    ]

    for column in schema_columns:
        column_name = str(column["name"])
        if column_name in key_columns or column_name == source_column or column_name == "RenewalInterval":
            continue
        projected_columns.append(f"left_rows.{_quote_identifier(column_name)}")

    projected_columns.extend(
        [
            f"left_rows.{_quote_identifier('RenewalInterval')} AS {_quote_identifier('RenewalInterval_control_center')}",
            f"right_rows.{_quote_identifier('RenewalInterval')} AS {_quote_identifier('RenewalInterval_train')}",
            f"{source_expr} AS {_quote_identifier(raw_backup_column)}",
            f"{normalized_source_expr} AS {_quote_identifier(source_column)}",
            f"{target_expr} AS {_quote_identifier(str(label_config['target_column']))}",
        ]
    )

    key_projection = ", ".join(_quote_identifier(column) for column in key_columns)
    return f"""
SELECT
    {', '.join(projected_columns)}
FROM {trusted_keys_sql} AS trusted_keys
INNER JOIN {left_relation_sql} AS left_rows USING ({key_projection})
INNER JOIN {right_relation_sql} AS right_rows USING ({key_projection})
"""


def _write_dataset_from_query(
    connection: duckdb.DuckDBPyConnection,
    query_sql: str,
    output_path: Path,
) -> int:
    ensure_parent(output_path)
    connection.execute(
        f"COPY ({query_sql}) TO {_quote_literal(output_path.resolve().as_posix())} "
        "(FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM read_parquet({_quote_literal(output_path.resolve().as_posix())})"
        ).fetchone()[0]
    )


def _write_unknown_label_report(
    project_root: Path,
    generated_at: str,
    unknown_report_name: str,
    unknown_label_rows_path: str,
    unknown_label_row_count: int,
    unknown_label_ratio: float,
) -> Path:
    report_path = project_root / "reports" / "data_validation" / f"{unknown_report_name}.md"
    ensure_parent(report_path)
    lines = [
        f"# {unknown_report_name.replace('_', ' ').title()}",
        "",
        f"Generated at: {generated_at}",
        "",
        f"- Unknown label row count: {unknown_label_row_count}",
        f"- Unknown label ratio: {unknown_label_ratio:.6f}",
        f"- Unknown label rows path: `{unknown_label_rows_path}`",
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def _evaluate_label_quality(
    unknown_label_ratio: float,
    unknown_label_ratio_max: float,
) -> list[str]:
    failures: list[str] = []
    if unknown_label_ratio > unknown_label_ratio_max:
        failures.append(
            f"Unknown label ratio {unknown_label_ratio:.6f} exceeds configured maximum {unknown_label_ratio_max:.6f}."
        )
    return failures


def _write_label_quality_report(
    project_root: Path,
    result: LabelQualityAuditResult,
    generated_at: str,
) -> Path:
    report_path = project_root / "reports" / "data_validation" / "label_quality_report.md"
    ensure_parent(report_path)
    status = "PASS" if result.audit_passed else "FAIL"

    lines = [
        "# Label Quality Report",
        "",
        f"Generated at: {generated_at}",
        "",
        "## Summary",
        "",
        f"- Status: {status}",
        f"- Left dataset: `{result.left_dataset_name}`",
        f"- Right dataset: `{result.right_dataset_name}`",
        f"- Trusted keys path: `{result.trusted_keys_path}`",
        f"- Merged labeled dataset path: `{result.merged_dataset_path}`",
        f"- Unknown label rows path: `{result.unknown_label_rows_path}`",
        f"- Source label column: `{result.source_column}`",
        f"- Raw backup column: `{result.raw_backup_column}`",
        f"- Target column: `{result.target_column}`",
        f"- Candidate key: `{tuple(result.key_columns)}`",
        f"- Allowed targets: `{result.allowed_targets}`",
        f"- Trusted row count: {result.trusted_row_count}",
        f"- Merged row count: {result.merged_row_count}",
        f"- Unknown label row count: {result.unknown_label_row_count}",
        f"- Unknown label ratio: {result.unknown_label_ratio:.6f}",
        "",
    ]

    if result.failure_reasons:
        lines.extend(["### Failure Reasons", ""])
        for reason in result.failure_reasons:
            lines.append(f"- {reason}")
        lines.append("")

    lines.extend(
        [
            "### Label Distribution",
            "",
            "| Label | Row Count | Row Ratio |",
            "| --- | ---: | ---: |",
        ]
    )
    for metric in result.label_distribution:
        lines.append(f"| {metric.label} | {metric.row_count} | {metric.row_ratio:.6f} |")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run_label_quality_audit(project_root: Path | None = None) -> int:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    config = load_pipeline_config(root)
    schema = config["schema"]
    label_config = config["labels"]
    quality_thresholds = config["quality_thresholds"]

    key_columns = list(schema["key_definition"]["columns"])
    schema_columns = list(schema["columns"])
    source_column = str(label_config["source_column"])
    raw_backup_column = str(label_config["raw_backup_column"])
    target_column = str(label_config["target_column"])
    allowed_targets = list(label_config["allowed_targets"])
    unknown_label_ratio_max = float(quality_thresholds["label_quality"]["unknown_label_ratio_max"])

    left_dataset, right_dataset = DATASET_SPECS
    trusted_keys_path = root / "data" / "validated" / "alignment" / "trusted_alignment_keys.parquet"
    merged_dataset_path = root / "data" / "validated" / "merged" / "trusted_labeled_dataset.parquet"
    unknown_label_rows_path = root / "data" / "validated" / "labels" / "unknown_label_rows.parquet"

    trusted_keys_sql = _relation_sql(trusted_keys_path)
    left_relation_sql = _relation_sql(left_dataset.staging_path)
    right_relation_sql = _relation_sql(right_dataset.staging_path)

    base_sql = _build_trusted_base_sql(
        trusted_keys_sql=trusted_keys_sql,
        left_relation_sql=left_relation_sql,
        right_relation_sql=right_relation_sql,
        key_columns=key_columns,
        schema_columns=schema_columns,
        label_config=label_config,
    )

    generated_at = datetime.now(timezone.utc).isoformat()

    connection = duckdb.connect(database=":memory:")
    try:
        trusted_row_count = int(connection.execute(f"SELECT COUNT(*) FROM ({base_sql})").fetchone()[0])

        merged_row_count = _write_dataset_from_query(
            connection,
            f"SELECT * FROM ({base_sql}) WHERE {_quote_identifier(target_column)} IS NOT NULL",
            merged_dataset_path,
        )
        unknown_label_row_count = _write_dataset_from_query(
            connection,
            f"SELECT * FROM ({base_sql}) WHERE {_quote_identifier(target_column)} IS NULL",
            unknown_label_rows_path,
        )
        unknown_label_ratio = (
            0.0 if trusted_row_count == 0 else unknown_label_row_count / trusted_row_count
        )

        distribution_rows = connection.execute(
            f"""
SELECT
    {_quote_identifier(target_column)} AS label,
    COUNT(*) AS row_count
FROM read_parquet({_quote_literal(merged_dataset_path.resolve().as_posix())})
GROUP BY {_quote_identifier(target_column)}
ORDER BY row_count DESC, label
"""
        ).fetchall()
        label_distribution = [
            LabelDistributionMetric(
                label=str(row[0]),
                row_count=int(row[1]),
                row_ratio=0.0 if merged_row_count == 0 else int(row[1]) / merged_row_count,
            )
            for row in distribution_rows
        ]

        failure_reasons = _evaluate_label_quality(
            unknown_label_ratio=unknown_label_ratio,
            unknown_label_ratio_max=unknown_label_ratio_max,
        )

        result = LabelQualityAuditResult(
            left_dataset_name=left_dataset.name,
            right_dataset_name=right_dataset.name,
            trusted_keys_path=str(trusted_keys_path),
            merged_dataset_path=str(merged_dataset_path),
            unknown_label_rows_path=str(unknown_label_rows_path),
            source_column=source_column,
            raw_backup_column=raw_backup_column,
            target_column=target_column,
            key_columns=key_columns,
            allowed_targets=allowed_targets,
            trusted_row_count=trusted_row_count,
            merged_row_count=merged_row_count,
            unknown_label_row_count=unknown_label_row_count,
            unknown_label_ratio=unknown_label_ratio,
            audit_passed=not failure_reasons,
            failure_reasons=failure_reasons,
            label_distribution=label_distribution,
        )
    finally:
        connection.close()

    label_quality_report_path = _write_label_quality_report(root, result, generated_at)
    unknown_report_path = _write_unknown_label_report(
        project_root=root,
        generated_at=generated_at,
        unknown_report_name=str(label_config["unknown_label_policy"]["report_name"]),
        unknown_label_rows_path=str(unknown_label_rows_path),
        unknown_label_row_count=result.unknown_label_row_count,
        unknown_label_ratio=result.unknown_label_ratio,
    )
    manifest_path = root / "metadata" / "manifests" / "label_quality_manifest.json"
    write_json(
        manifest_path,
        {
            "generated_at": generated_at,
            "report_path": str(label_quality_report_path),
            "unknown_label_report_path": str(unknown_report_path),
            "unknown_label_ratio_max": unknown_label_ratio_max,
            "result": {
                **{
                    key: value
                    for key, value in asdict(result).items()
                    if key != "label_distribution"
                },
                "label_distribution": [asdict(metric) for metric in result.label_distribution],
            },
        },
    )

    return 0 if result.audit_passed else 1
