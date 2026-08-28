# Data Engineering Config Baseline

This project now has four baseline configuration files:

- `configs/schema/stsrs_schema.yaml`
- `configs/labels/attack_label_mapping.yaml`
- `configs/split_policy/time_split.yaml`
- `configs/quality_thresholds/data_quality.yaml`

Their roles are:

- `schema`: defines the raw input contract, key columns, column types, and source-specific semantics.
- `labels`: defines how `AttackInfo` is normalized and mapped to `AttackLabel`.
- `split_policy`: defines the time-based train/validation/test split with a purge gap.
- `quality_thresholds`: defines the validation gates that must pass before merge and training.

Immediate next step:

1. Read the two raw `.txt` files using the schema contract.
2. Validate header, column order, timestamp parsing, and numeric parsing.
3. Write staging Parquet outputs.
4. Generate the first schema validation report.
