from __future__ import annotations

import argparse
from pathlib import Path

from stsrs_data_engineering.alignment_audit import run_alignment_audit
from stsrs_data_engineering.baseline_training import run_baseline_training
from stsrs_data_engineering.encoded_features import run_encoded_feature_generation
from stsrs_data_engineering.field_consistency import run_field_consistency_audit
from stsrs_data_engineering.feature_engineering import run_feature_engineering
from stsrs_data_engineering.key_audit import run_key_audit
from stsrs_data_engineering.label_quality import run_label_quality_audit
from stsrs_data_engineering.schema_validation import run_schema_to_staging
from stsrs_data_engineering.time_split import run_time_split
from stsrs_data_engineering.v1_ablation import run_v1_ablation
from stsrs_data_engineering.v1_diagnostics import run_v1_diagnostics
from stsrs_data_engineering.v1_tree_baseline import run_v1_tree_baseline
from stsrs_data_engineering.v2_diagnostics import run_v2_diagnostics
from stsrs_data_engineering.v2_explainability import run_v2_explainability
from stsrs_data_engineering.v2_generalization import run_v2_generalization
from stsrs_data_engineering.v2_compact_tree import run_v2_compact_tree


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stsrs-data",
        description="Run STSRS data engineering pipeline stages.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    schema_parser = subparsers.add_parser(
        "schema-to-staging",
        help="Validate the raw schema and write staging Parquet files.",
    )
    schema_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Override the project root. Defaults to the current repository root.",
    )

    key_audit_parser = subparsers.add_parser(
        "key-audit",
        help="Validate candidate key quality on staging Parquet files.",
    )
    key_audit_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Override the project root. Defaults to the current repository root.",
    )

    alignment_audit_parser = subparsers.add_parser(
        "alignment-audit",
        help="Audit cross-dataset key alignment on staging Parquet files.",
    )
    alignment_audit_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Override the project root. Defaults to the current repository root.",
    )

    field_consistency_parser = subparsers.add_parser(
        "field-consistency-audit",
        help="Audit field-level consistency on trusted 1:1 aligned rows.",
    )
    field_consistency_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Override the project root. Defaults to the current repository root.",
    )

    label_quality_parser = subparsers.add_parser(
        "label-quality-audit",
        help="Build the trusted labeled dataset and audit target label quality.",
    )
    label_quality_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Override the project root. Defaults to the current repository root.",
    )

    time_split_parser = subparsers.add_parser(
        "time-split",
        help="Construct time-based train/validation/test datasets with a purge gap.",
    )
    time_split_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Override the project root. Defaults to the current repository root.",
    )

    feature_parser = subparsers.add_parser(
        "feature-engineering",
        help="Build canonical training-ready feature datasets and feature manifest.",
    )
    feature_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Override the project root. Defaults to the current repository root.",
    )

    encoded_feature_parser = subparsers.add_parser(
        "encoded-features",
        help="Build encoded model-input datasets from canonical features.",
    )
    encoded_feature_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Override the project root. Defaults to the current repository root.",
    )

    baseline_train_parser = subparsers.add_parser(
        "baseline-train",
        help="Train and evaluate a reproducible baseline classifier on encoded features.",
    )
    baseline_train_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Override the project root. Defaults to the current repository root.",
    )

    v1_tree_parser = subparsers.add_parser(
        "v1-tree-baseline",
        help="Train and evaluate the preserved V1 tree-based baseline iteration.",
    )
    v1_tree_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Override the project root. Defaults to the current repository root.",
    )

    v1_diagnostics_parser = subparsers.add_parser(
        "v1-diagnostics",
        help="Run post-training diagnostics for the preserved V1 tree baseline.",
    )
    v1_diagnostics_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Override the project root. Defaults to the current repository root.",
    )

    v1_ablation_parser = subparsers.add_parser(
        "v1-ablation",
        help="Run V1 feature ablation experiments against the preserved tree baseline.",
    )
    v1_ablation_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Override the project root. Defaults to the current repository root.",
    )

    v2_compact_tree_parser = subparsers.add_parser(
        "v2-compact-tree",
        help="Train and evaluate the compact top-3 V2 tree iteration.",
    )
    v2_compact_tree_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Override the project root. Defaults to the current repository root.",
    )

    v2_diagnostics_parser = subparsers.add_parser(
        "v2-diagnostics",
        help="Run post-training diagnostics for the V2 compact tree iteration.",
    )
    v2_diagnostics_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Override the project root. Defaults to the current repository root.",
    )

    v2_explainability_parser = subparsers.add_parser(
        "v2-explainability",
        help="Run SHAP explainability for the V2 compact tree iteration.",
    )
    v2_explainability_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Override the project root. Defaults to the current repository root.",
    )

    v2_generalization_parser = subparsers.add_parser(
        "v2-generalization",
        help="Run temporal and retraining stability validation for V2.",
    )
    v2_generalization_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Override the project root. Defaults to the current repository root.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "schema-to-staging":
        return run_schema_to_staging(project_root=args.project_root)
    if args.command == "key-audit":
        return run_key_audit(project_root=args.project_root)
    if args.command == "alignment-audit":
        return run_alignment_audit(project_root=args.project_root)
    if args.command == "field-consistency-audit":
        return run_field_consistency_audit(project_root=args.project_root)
    if args.command == "label-quality-audit":
        return run_label_quality_audit(project_root=args.project_root)
    if args.command == "time-split":
        return run_time_split(project_root=args.project_root)
    if args.command == "feature-engineering":
        return run_feature_engineering(project_root=args.project_root)
    if args.command == "encoded-features":
        return run_encoded_feature_generation(project_root=args.project_root)
    if args.command == "baseline-train":
        return run_baseline_training(project_root=args.project_root)
    if args.command == "v1-tree-baseline":
        return run_v1_tree_baseline(project_root=args.project_root)
    if args.command == "v1-diagnostics":
        return run_v1_diagnostics(project_root=args.project_root)
    if args.command == "v1-ablation":
        return run_v1_ablation(project_root=args.project_root)
    if args.command == "v2-compact-tree":
        return run_v2_compact_tree(project_root=args.project_root)
    if args.command == "v2-diagnostics":
        return run_v2_diagnostics(project_root=args.project_root)
    if args.command == "v2-explainability":
        return run_v2_explainability(project_root=args.project_root)
    if args.command == "v2-generalization":
        return run_v2_generalization(project_root=args.project_root)

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
