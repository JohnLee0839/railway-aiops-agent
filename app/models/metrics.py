"""Metric-driven AIOps data models."""

from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid

from pydantic import BaseModel, Field


class RailMetrics(BaseModel):
    """Core rail signal monitoring metrics."""

    speed: Optional[float] = Field(default=None, description="Train speed in km/h")
    distance: Optional[float] = Field(default=None, description="Distance to signal equipment in meters")
    location: Optional[str] = Field(default=None, description="Train location")

    signal_status: Optional[str] = Field(default=None, description="Signal status")
    overlap_status: Optional[str] = Field(default=None, description="Overlap/interlock status")
    overlap_count: Optional[int] = Field(default=None, description="Overlap section count")

    packet_loss: Optional[float] = Field(default=None, description="Packet loss")
    latency: Optional[float] = Field(default=None, description="Communication latency in ms")
    renewal_interval: Optional[float] = Field(default=None, description="Signal renewal interval in ms")
    burstiness: Optional[float] = Field(default=None, description="Traffic burstiness")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class RailMetricRecord(BaseModel):
    """Unified metric record produced from rail monitoring inputs."""

    record_id: str = Field(
        default_factory=lambda: f"RMR-{uuid.uuid4().hex[:8].upper()}"
    )
    timestamp: datetime = Field(description="Monitoring timestamp")
    train_id: str = Field(description="Train identifier")
    signal_id: str = Field(description="Signal equipment identifier")
    metrics: RailMetrics = Field(default_factory=RailMetrics, description="Monitoring metrics")
    source_metrics: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Source-specific metrics keyed by source name",
    )
    source_files: List[str] = Field(default_factory=list, description="Source data files")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class PrometheusMetricSnapshot(BaseModel):
    """Prometheus-style metric snapshot accepted by the metric pipeline."""

    snapshot_id: str = Field(
        default_factory=lambda: f"PMS-{uuid.uuid4().hex[:8].upper()}"
    )
    timestamp: datetime = Field(description="Collection timestamp")
    labels: Dict[str, str] = Field(default_factory=dict, description="Prometheus labels")
    metrics: Dict[str, float] = Field(default_factory=dict, description="Prometheus metric values")
    source_record_id: Optional[str] = Field(default=None, description="Related RailMetricRecord ID")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class FusionKey(BaseModel):
    """Multi-source fusion key: timestamp + train_id + signal_id."""

    timestamp: datetime
    train_id: str
    signal_id: str

    def __hash__(self) -> int:
        return hash((self.timestamp, self.train_id, self.signal_id))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FusionKey):
            return False
        return (
            self.timestamp == other.timestamp
            and self.train_id == other.train_id
            and self.signal_id == other.signal_id
        )

    def to_string(self) -> str:
        return f"{self.timestamp.isoformat()}:{self.train_id}:{self.signal_id}"


class MetricAnomaly(BaseModel):
    """Single metric anomaly result."""

    metric_name: str = Field(description="Metric name")
    current_value: float = Field(description="Current value")
    threshold: float = Field(description="Threshold value")
    is_abnormal: bool = Field(description="Whether the metric is abnormal")
    deviation_ratio: float = Field(default=0.0, description="Deviation ratio")
    description: str = Field(default="", description="Anomaly description")


class MetricAnomalyReport(BaseModel):
    """Metric anomaly report."""

    report_id: str = Field(
        default_factory=lambda: f"MAR-{uuid.uuid4().hex[:8].upper()}"
    )
    record_id: str = Field(description="Related RailMetricRecord ID")
    anomalies: List[MetricAnomaly] = Field(default_factory=list)
    overall_severity: str = Field(default="P4", description="Overall metric severity")
    abnormal_metric_names: List[str] = Field(default_factory=list)
    summary: str = Field(default="", description="Anomaly summary")


class FeatureVector(BaseModel):
    """Structured feature vector extracted from RailMetricRecord."""

    packet_loss: float = Field(default=0.0, description="Packet loss")
    latency: float = Field(default=0.0, description="Communication latency in ms")
    burstiness: float = Field(default=0.0, description="Traffic burstiness")

    renewal_interval_difference: Optional[float] = Field(
        default=None,
        description="Source difference for renewal_interval: train - control_center",
    )
    renewal_interval_ratio: Optional[float] = Field(
        default=None,
        description="Source ratio for renewal_interval: train / control_center",
    )

    signal_status: Optional[str] = Field(default=None, description="Signal status")
    overlap_status: Optional[str] = Field(default=None, description="Overlap/interlock status")
    overlap_count: Optional[int] = Field(default=None, description="Overlap section count")

    speed: Optional[float] = Field(default=None, description="Train speed in km/h")
    distance: Optional[float] = Field(default=None, description="Distance to signal equipment in meters")

    train_id: str = Field(default="", description="Train identifier")
    signal_id: str = Field(default="", description="Signal equipment identifier")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class AttackPrediction(BaseModel):
    """Attack detector prediction output."""

    attack_type: str = Field(
        default="UNKNOWN",
        description="Predicted attack type",
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Prediction confidence",
    )
    probabilities: Dict[str, float] = Field(
        default_factory=dict,
        description="Per-class probability distribution",
    )
    model_version: str = Field(
        default="unknown",
        description="Model version identifier",
    )
    detector_backend: str = Field(
        default="unknown",
        description="Detector backend: zl / rule / mock",
    )
    fallback_used: bool = Field(
        default=False,
        description="Whether fallback detector was used",
    )
    fallback_reason: Optional[str] = Field(
        default=None,
        description="Fallback reason code",
    )
    inference_ms: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Inference latency in milliseconds",
    )
    feature_vector: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional feature/audit payload used for prediction",
    )
