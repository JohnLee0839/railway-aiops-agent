from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
    raise RuntimeError(
        "PyYAML is required to read the STSRS config files. "
        "Run `uv sync` or `uv pip install pyyaml` in the project root."
    ) from exc


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    source_name: str
    raw_path: Path
    staging_path: Path


DATASET_SPECS = (
    DatasetSpec(
        name="control_center",
        source_name="ControlCenter",
        raw_path=PROJECT_ROOT / "STSRS-Control Center.txt",
        staging_path=PROJECT_ROOT / "data" / "staging" / "control_center.parquet",
    ),
    DatasetSpec(
        name="train",
        source_name="Train",
        raw_path=PROJECT_ROOT / "STSRS-Train.txt",
        staging_path=PROJECT_ROOT / "data" / "staging" / "train.parquet",
    ),
)


def load_yaml_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping in config file: {path}")
    return data


def load_pipeline_config(project_root: Path | None = None) -> dict[str, Any]:
    root = project_root or PROJECT_ROOT
    return {
        "schema": load_yaml_file(root / "configs" / "schema" / "stsrs_schema.yaml"),
        "labels": load_yaml_file(root / "configs" / "labels" / "attack_label_mapping.yaml"),
        "split_policy": load_yaml_file(root / "configs" / "split_policy" / "time_split.yaml"),
        "quality_thresholds": load_yaml_file(
            root / "configs" / "quality_thresholds" / "data_quality.yaml"
        ),
    }


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True)
