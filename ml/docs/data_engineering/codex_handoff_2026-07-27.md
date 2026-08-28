# STSRS Codex Handoff (2026-07-27)

## Project Root

`D:\STUDY\Security-Threats-in-Smart-Railway-Systems-STSRS--main`

## Current State

This repository has already moved beyond planning and now contains:

- a data-engineering directory layout
- baseline configuration files
- a runnable Python package scaffold
- a first pipeline stage that has been executed successfully

The first completed pipeline stage is:

```text
raw txt -> schema validation -> staging parquet -> schema report
```

## Raw Inputs

- `STSRS-Control Center.txt`
- `STSRS-Train.txt`

Known assumption from exploratory analysis:

- candidate key: `(Timestamp, TrainID, SignalID)`
- `RenewalInterval` has different semantics across sources
  - Control Center: TTL / hop count
  - Train: key renewal interval

Important:

- key uniqueness has **not** been fully validated yet
- cross-dataset one-to-one alignment has **not** been fully validated yet
- merge, label cleaning, feature engineering, and dataset construction are **not** done yet

## Config Files Already Added

- `configs/schema/stsrs_schema.yaml`
- `configs/labels/attack_label_mapping.yaml`
- `configs/split_policy/time_split.yaml`
- `configs/quality_thresholds/data_quality.yaml`

These define:

- raw schema contract
- label mapping
- split policy
- quality gates

## Code Already Added

- `pyproject.toml`
- `src/stsrs_data_engineering/__init__.py`
- `src/stsrs_data_engineering/config.py`
- `src/stsrs_data_engineering/cli.py`
- `src/stsrs_data_engineering/schema_validation.py`
- `scripts/run_schema_validation.py`
- `scripts/bootstrap_env.ps1`

## Outputs Already Generated

- `data/staging/control_center.parquet`
- `data/staging/train.parquet`
- `reports/data_validation/schema_report.md`
- `metadata/manifests/schema_validation_manifest.json`

## Verified Results So Far

The schema-validation stage passed for both datasets:

- row count: `10,000,000` in each file
- header matches expected schema
- timestamp parse success rate: `1.0`
- numeric parse success rate: `1.0`
- allowed-value checks for `SignalStatus` and `OverlapStatus` passed

See:

- `reports/data_validation/schema_report.md`
- `metadata/manifests/schema_validation_manifest.json`

## Environment Notes

The local environment was created with:

- Python `3.14.6`
- `uv`

The current project also contains a local `.venv`, but do **not** rely on copying it to a new machine.

Recommended on a new machine:

1. copy the project directory, but exclude:
   - `.venv`
   - `.uv-cache`
   - `__pycache__`
2. on the new machine, recreate the environment locally

## New Machine Setup

Run this in PowerShell from the project root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_env.ps1
```

If you also want to rerun the first pipeline stage immediately:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_env.ps1 -RunSchemaValidation
```

## Recommended Copy Set

Copy these folders/files to the new computer:

- raw data files:
  - `STSRS-Control Center.txt`
  - `STSRS-Train.txt`
- project/config/code:
  - `pyproject.toml`
  - `configs/`
  - `src/`
  - `scripts/`
  - `docs/data_engineering/`
- generated outputs:
  - `data/staging/`
  - `reports/data_validation/`
  - `metadata/manifests/`

Do not copy:

- `.venv/`
- `.uv-cache/`
- `__pycache__/`

## Next Required Task

The next pipeline stage should be:

```text
Stage 3: Key Audit
```

Use the staging Parquet files as input and validate whether:

- `(Timestamp, TrainID, SignalID)` is truly unique in each dataset
- there are any `null key` rows
- there are any `duplicate key` rows
- the distinct key counts match expectations

Expected new outputs:

- `reports/data_validation/key_audit_report.md`
- `metadata/manifests/key_audit_manifest.json`
- duplicate/null sample files under `data/validated/keys/`

## Do Not Do Yet

Do not do these before key audit and alignment audit are complete:

- dataset merge
- label cleaning
- feature engineering
- train/validation/test construction
- model training

## Prompt For The Next Codex

Use the following prompt verbatim if helpful:

```text
Continue the STSRS data-engineering work in this repository.

Current completed stage:
raw txt -> schema validation -> staging parquet -> schema report

Already generated:
- data/staging/control_center.parquet
- data/staging/train.parquet
- reports/data_validation/schema_report.md
- metadata/manifests/schema_validation_manifest.json

The next required task is Stage 3: Key Audit.

Please use the existing project code/config structure and implement key validation on the staging Parquet files.

Validate whether the candidate key (Timestamp, TrainID, SignalID) is truly unique in each dataset.

Produce:
- key_audit_report
- key_audit_manifest
- null key statistics
- duplicate key statistics
- distinct key counts
- duplicate/null sample outputs

Do not start merge or model training yet.
```
