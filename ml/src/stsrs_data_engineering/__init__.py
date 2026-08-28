"""STSRS data engineering package."""

from stsrs_data_engineering.model_service import (
    ModelService,
    PredictionResult,
    build_model_service,
    prediction_result_to_dict,
)

__all__ = [
    "__version__",
    "ModelService",
    "PredictionResult",
    "build_model_service",
    "prediction_result_to_dict",
]

__version__ = "0.1.0"
