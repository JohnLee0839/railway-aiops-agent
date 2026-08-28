from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from stsrs_data_engineering.model_service import build_model_service, prediction_result_to_dict


def _fetch_validation_sample(project_root: Path) -> dict[str, object]:
    input_path = (project_root / "data" / "serving" / "validation" / "validation.parquet").resolve().as_posix()
    rows = duckdb.connect(database=":memory:").execute(
        f"""
SELECT
    Speed,
    Distance,
    Location,
    SignalStatus,
    OverlapStatus,
    OverlapCount,
    PacketLoss,
    Latency,
    Burstiness
FROM read_parquet('{input_path}')
LIMIT 1
"""
    ).fetchall()
    if not rows:
        raise ValueError("No validation sample rows found for ModelService smoke test.")
    row = rows[0]
    return {
        "Speed": row[0],
        "Distance": row[1],
        "Location": row[2],
        "SignalStatus": row[3],
        "OverlapStatus": row[4],
        "OverlapCount": row[5],
        "PacketLoss": row[6],
        "Latency": row[7],
        "Burstiness": row[8],
    }


def main() -> int:
    service = build_model_service(PROJECT_ROOT)
    sample_input = _fetch_validation_sample(PROJECT_ROOT)
    results = {
        model_version: prediction_result_to_dict(
            service.predict(
                raw_input=sample_input,
                model_version=model_version,
                metadata={"source": "smoke_test"},
            )
        )
        for model_version in service.list_versions()
    }
    print(json.dumps(results, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
