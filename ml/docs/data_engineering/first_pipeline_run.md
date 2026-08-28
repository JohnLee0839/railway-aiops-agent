# First Pipeline Run

The first executable pipeline stage is now:

```text
raw txt -> schema validation -> staging parquet -> schema report
```

Files added for this stage:

- `pyproject.toml`
- `src/stsrs_data_engineering/cli.py`
- `src/stsrs_data_engineering/config.py`
- `src/stsrs_data_engineering/schema_validation.py`
- `scripts/run_schema_validation.py`

Recommended run sequence:

1. Create a project-local virtual environment:
   `uv venv`
2. Install the project dependencies from `pyproject.toml`:
   `uv sync`
3. Run the first pipeline stage:
   `uv run python scripts/run_schema_validation.py`

Expected outputs:

- `data/staging/control_center.parquet`
- `data/staging/train.parquet`
- `reports/data_validation/schema_report.md`
- `metadata/manifests/schema_validation_manifest.json`
