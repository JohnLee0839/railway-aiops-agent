"""
事件驱动 AIOps 模型定义
Incident, State, Event, KnowledgeBase 等核心 Pydantic 模型
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field, computed_field
import uuid


# ============================================================
# 枚举定义
# ============================================================

class IncidentSource(str, Enum):
    """事件来源"""
    PROMETHEUS = "prometheus"
    MCP = "mcp"
    STSRS = "stsrs"
    MANUAL = "manual"


class AttackType(str, Enum):
    """攻击/故障类型"""
    DOS = "DoS"
    JAMMING = "Jamming"
    REPLAY_ATTACK = "Replay Attack"
    SPOOFING = "Spoofing"
    MALWARE = "Malware"
    UNAUTHORIZED_ACCESS = "Unauthorized Access"
    SIGNAL_INTERFERENCE = "Signal Interference"
    CPU_HIGH = "CPU High Usage"
    MEMORY_HIGH = "Memory High Usage"
    DISK_HIGH = "Disk High Usage"
    SERVICE_UNAVAILABLE = "Service Unavailable"
    SLOW_RESPONSE = "Slow Response"
    NETWORK_PARTITION = "Network Partition"
    UNKNOWN = "Unknown"


class Severity(str, Enum):
    """
    严重级别（Metric-driven: 基于指标影响程度评估）

    P1: 影响列车运行安全 (SignalStatus=RED, OverlapStatus=ABNORMAL, Speed异常)
    P2: 影响信号通信 (PacketLoss>50%, Latency>200ms, RenewalInterval异常)
    P3: 性能下降 (PacketLoss>10%, Latency>100ms, Burstiness异常)
    P4: 轻微异常/未知
    """
    P1 = "P1"  # 紧急 - 影响列车运行安全
    P2 = "P2"  # 高 - 影响信号通信
    P3 = "P3"  # 中 - 性能下降
    P4 = "P4"  # 低 - 轻微异常/正常/信息
    UNKNOWN = "UNKNOWN"  # 尚未评估


class IncidentState(str, Enum):
    """事件处理状态机"""
    NEW = "NEW"
    TRIAGED = "TRIAGED"
    PLANNED = "PLANNED"
    EXECUTING = "EXECUTING"
    VERIFIED = "VERIFIED"
    RESOLVED = "RESOLVED"          # 非严格终态 — 可被 COMPENSATING 打断
    COMPENSATING = "COMPENSATING"
    FAILED = "FAILED"              # 严格终态
    ESCALATED = "ESCALATED"        # 严格终态


class ActionStatus(str, Enum):
    """动作执行状态"""
    SUCCESS = "SUCCESS"
    RETRY = "RETRY"
    COMPENSATE = "COMPENSATE"
    ESCALATE = "ESCALATE"
    FAILED = "FAILED"


class ApprovalStatus(str, Enum):
    """审批状态"""
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    TIMEOUT = "TIMEOUT"


class SSEEventType(str, Enum):
    """SSE 事件类型（完整版）"""
    # 生命周期
    INCIDENT_CREATED = "incident_created"
    INCIDENT_DEDUPLICATED = "incident_deduplicated"
    INCIDENT_TRIAGED = "incident_triaged"
    PLAN_GENERATED = "plan_generated"
    # 审批
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_TIMEOUT = "approval_timeout"
    APPROVAL_REQUESTED = "approval_requested"
    # 执行
    ACTION_EXECUTED = "action_executed"
    RETRY_SCHEDULED = "retry_scheduled"
    # 补偿
    COMPENSATION_STARTED = "compensation_started"
    COMPENSATION_ACTION_EXECUTED = "compensation_action_executed"
    COMPENSATION_COMPLETED = "compensation_completed"
    # 验证
    VERIFICATION_FINISHED = "verification_finished"
    # 终态
    INCIDENT_RESOLVED = "incident_resolved"
    INCIDENT_FAILED = "incident_failed"
    INCIDENT_ESCALATED = "incident_escalated"
    # 状态迁移（每次 transition 必发）
    STATE_CHANGED = "state_changed"
    # 流程结束
    COMPLETE = "complete"


class ApprovalAction(str, Enum):
    """需要审批的高风险动作"""
    STOP_TRAIN = "STOP_TRAIN"
    BLOCK_SECTION = "BLOCK_SECTION"
    EMERGENCY_SHUTDOWN = "EMERGENCY_SHUTDOWN"
    RESTART_GATEWAY = "restart_gateway"
    BLOCK_SUSPICIOUS_SOURCE = "block_suspicious_source"


class MockActionName(str, Enum):
    """当前可由 ActionOrchestrator 执行的 Mock 工具名称。"""
    SWITCH_BACKUP_LINK = "switch_backup_link"
    RESTART_GATEWAY = "restart_gateway"
    BLOCK_SUSPICIOUS_SOURCE = "block_suspicious_source"
    NOTIFY_DISPATCHER = "notify_dispatcher"
    GENERATE_TICKET = "generate_ticket"
    VERIFY_NETWORK_HEALTH = "verify_network_health"
    ROLLBACK_SWITCH_BACKUP_LINK = "rollback_switch_backup_link"
    ROLLBACK_BLOCK_SUSPICIOUS_SOURCE = "rollback_block_suspicious_source"


# ============================================================
# 核心事件模型
# ============================================================

class IncidentMetadata(BaseModel):
    """事件元数据"""
    source_ip: Optional[str] = None
    target_ip: Optional[str] = None
    port: Optional[int] = None
    protocol: Optional[str] = None
    train_id: Optional[str] = None
    signal_id: Optional[str] = None
    control_center: Optional[str] = None
    raw_alert: Optional[Dict[str, Any]] = None
    extra: Dict[str, Any] = Field(default_factory=dict)


class Incident(BaseModel):
    """
    统一事件对象。

    Metric-driven AIOps 设计:
    - attack_type 默认 UNKNOWN，由 TriageAgent 诊断后赋值
    - severity 默认 P4，由 SeverityEngine 初步评估 + TriageAgent 确认
    - metrics_snapshot 承载原始监测指标（RailMetricRecord 的序列化形式）
    """
    incident_id: str = Field(
        default_factory=lambda: f"INC-{uuid.uuid4().hex[:8].upper()}"
    )
    source: IncidentSource = IncidentSource.MANUAL
    attack_type: AttackType = AttackType.UNKNOWN
    severity: Severity = Severity.P4
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: IncidentMetadata = Field(default_factory=IncidentMetadata)
    trace_id: str = Field(
        default_factory=lambda: f"trace-{uuid.uuid4().hex[:12]}"
    )

    # ---- 显式去重字段（不藏在 metadata 中） ----
    # 来源 IP（从 metadata.source_ip 复制 / 由 Normalizer 统一设置）
    source_ip: Optional[str] = None
    # 事件特征签名（由 Normalizer 根据 train_id + signal_id + metrics hash 生成）
    event_signature: Optional[str] = None
    # 去重键（由 Normalizer 统一生成，Deduplicator 用作核心合并键）
    dedup_key: Optional[str] = None

    # ---- Metric-driven AIOps 新增字段 ----
    # 原始监测指标快照（来自 STSRS Adapter / Prometheus Simulator）
    metrics_snapshot: Optional[Dict[str, Any]] = Field(
        default=None,
        description="原始监测指标快照: {packet_loss: 0.35, latency: 250, ...}"
    )
    # 监督学习攻击检测模型预测结果
    attack_prediction: Optional[Dict[str, Any]] = Field(
        default=None,
        description="AttackDetector 预测结果: {attack_type, confidence, probabilities, model_version}"
    )

    # 去重相关
    duplicate_count: int = 1
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    # 原始数据
    raw_payload: Optional[Dict[str, Any]] = None
    description: Optional[str] = None

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


# ============================================================
# 状态机相关模型
# ============================================================

class StateTransition(BaseModel):
    """状态迁移记录"""
    from_state: IncidentState
    to_state: IncidentState
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    reason: str = ""
    triggered_by: str = ""  # agent name
    metadata: Dict[str, Any] = Field(default_factory=dict)


class IncidentRecord(BaseModel):
    """事件存储记录"""
    incident_id: str
    thread_id: str  # LangGraph thread_id
    state: IncidentState = IncidentState.NEW
    incident: Incident
    # 处理结果
    triage_result: Optional[Dict[str, Any]] = None
    plan: Optional[List[str]] = None
    # 完整计划用于 durable store 重新构建可执行的 RunbookPlan；plan 保留给
    # 现有展示/API 调用方使用。
    runbook_plan: Optional["RunbookPlan"] = None
    execution_results: List[Dict[str, Any]] = Field(default_factory=list)
    verification_result: Optional[Dict[str, Any]] = None
    # 状态历史
    state_history: List[StateTransition] = Field(default_factory=list)
    # 时间戳
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    resolved_at: Optional[datetime] = None


# ============================================================
# Triage / Runbook / 计划模型
# ============================================================

class TriageResult(BaseModel):
    """
    TriageAgent 诊断输出。

    Metric-driven AIOps:
    - attack_type 是 TriageAgent 的诊断结论（不是输入！）
    - severity 是 TriageAgent 基于指标影响 + 知识库的综合评估
    - confidence 是基于证据充分程度的置信度
    """
    root_cause: str = Field(description="根因分析（如: 'Jamming attack causing signal communication failure'）")
    attack_type: Optional[str] = Field(
        default=None,
        description="TriageAgent 诊断出的攻击/故障类型（如 'Jamming', 'DoS', 'Replay Attack', 'UNKNOWN'）"
    )
    severity: Severity = Field(description="严重级别（P1-P4 或 UNKNOWN）")
    impact_scope: List[str] = Field(
        default_factory=list,
        description="影响范围（如 Train-1H66）"
    )
    upstream_assets: List[str] = Field(
        default_factory=list,
        description="上游资产"
    )
    downstream_assets: List[str] = Field(
        default_factory=list,
        description="下游资产"
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="诊断置信度"
    )
    evidence: List[str] = Field(
        default_factory=list,
        description="诊断证据列表（如: 'Packet loss pattern matches Jamming signature'）"
    )


class RawIncidentRequest(BaseModel):
    """
    统一事件入口格式。

    示例:
    {
      "source": "STSRS",
      "raw_event": { "attack_code": "STSRS-1003", ... }
    }
    """
    source: IncidentSource = IncidentSource.MANUAL
    raw_event: Dict[str, Any] = Field(default_factory=dict)


class ActionInstruction(BaseModel):
    """一条已规范化、可直接传给工具注册表的动作指令。"""
    # These IDs name the logical plan step and action. They are deliberately
    # independent from a plan's display order and are persisted with the plan.
    step_id: str = Field(default_factory=lambda: f"step-{uuid.uuid4().hex}")
    action_id: str = Field(default_factory=lambda: f"action-{uuid.uuid4().hex}")
    idempotency_key: Optional[str] = None
    action: MockActionName = Field(description="工具注册表中的动作名称")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="工具调用参数")
    description: str = Field(default="", description="面向操作者和审计的说明")


class RunbookPlan(BaseModel):
    """RunbookAgent 输出（含决策可解释性）"""
    plan_id: str = Field(
        default_factory=lambda: f"plan-{uuid.uuid4().hex[:8]}"
    )
    plan_revision: int = Field(
        default=1,
        ge=1,
        description="同一逻辑计划的持久化版本；Phase 2 首版固定为 1",
    )
    actions: List[ActionInstruction] = Field(
        min_length=1,
        description="按执行顺序排列的结构化动作指令",
    )
    source_kb: str = Field(
        default="CaseKB",
        description="知识来源: CaseKB / RunbookKB / TopologyKB"
    )
    # ---- 决策可解释性 ----
    case_id: Optional[str] = Field(
        default=None,
        description="引用的 CaseKB 案例 ID（如 CASE-1024）"
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="计划置信度"
    )
    reasoning: str = Field(
        default="",
        description="决策推理过程（如: 历史案例相似度 92%）"
    )
    # ----
    affected_assets: List[str] = Field(default_factory=list)
    estimated_duration_minutes: int = 30
    requires_approval: bool = False
    approval_actions: List[ApprovalAction] = Field(default_factory=list)
    rollback_actions: List[ActionInstruction] = Field(
        default_factory=list,
        description="按执行顺序排列的结构化回滚动作指令",
    )

    def bind_execution_identities(self, incident_id: str) -> None:
        """Bind incident-scoped idempotency keys once without changing logical IDs."""
        for instruction in [*self.actions, *self.rollback_actions]:
            if not instruction.step_id:
                instruction.step_id = f"step-{uuid.uuid4().hex}"
            if not instruction.action_id:
                instruction.action_id = f"action-{uuid.uuid4().hex}"
            if instruction.idempotency_key:
                continue
            material = "\x1f".join(
                (
                    incident_id,
                    self.plan_id,
                    str(self.plan_revision),
                    instruction.step_id,
                    instruction.action_id,
                )
            )
            instruction.idempotency_key = f"idem-{hashlib.sha256(material.encode()).hexdigest()}"

    @computed_field(return_type=List[str])
    @property
    def steps(self) -> List[str]:
        """兼容展示和审计：执行输入始终以 actions 为准。"""
        return [instruction.description or instruction.action.value for instruction in self.actions]

    @computed_field(return_type=List[str])
    @property
    def rollback_steps(self) -> List[str]:
        """兼容展示和审计：执行输入始终以 rollback_actions 为准。"""
        return [
            instruction.description or instruction.action.value
            for instruction in self.rollback_actions
        ]


# ============================================================
# 动作 / 执行模型
# ============================================================

class MockActionResult(BaseModel):
    """Mock 动作执行结果"""
    action_name: str
    success: bool
    message: str = ""
    duration_ms: float = 0.0
    error_type: Optional[str] = None  # "timeout" / "failure" / "exception" / "escalated" / "circuit_open"
    # 下游执行结局：not_sent / response_lost / unknown / failed / success
    outcome: Optional[str] = None
    retry_count: int = 0
    retry_exhausted: bool = False
    retryable: Optional[bool] = None
    side_effect_possible: bool = False
    side_effect_confirmed: bool = False
    target: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    """审批请求"""
    request_id: str = Field(
        default_factory=lambda: f"approval-{uuid.uuid4().hex[:8]}"
    )
    incident_id: str
    trace_id: str = ""           # 绑定追踪 ID
    thread_id: str = ""          # 绑定线程 ID
    action: ApprovalAction
    reason: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    timeout_minutes: int = 10
    status: ApprovalStatus = ApprovalStatus.PENDING
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    # 超时后自动转入的状态
    escalated_at: Optional[datetime] = None


class ExecutionOutcome(str, Enum):
    """副作用动作的执行结局，和升级/重试处置解耦。"""
    SUCCESS = "success"
    FAILED = "failed"
    NOT_SENT = "not_sent"
    RESPONSE_LOST = "response_lost"
    UNKNOWN = "unknown"


class ActionJournalStatus(str, Enum):
    """Durable lifecycle state for a logical action, distinct from its outcome."""
    STARTED = "STARTED"
    TERMINAL = "TERMINAL"


class ExternalStateStatus(str, Enum):
    """Read-only observation of the external system during recovery."""
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class ReconciliationDecision(str, Enum):
    """Recovery decision derived from an external-state observation."""
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    UNCERTAIN = "uncertain"
    NOT_RECONCILABLE = "not_reconcilable"


class WorkflowCursorStatus(str, Enum):
    """Durable position state for an existing RunbookPlan revision."""
    NOT_STARTED = "not_started"
    EXECUTING = "executing"
    WAITING_RECOVERY = "waiting_recovery"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class ExternalStateObservation(BaseModel):
    """Evidence returned by a read-only external-state probe."""
    status: ExternalStateStatus
    observed_at: datetime = Field(default_factory=datetime.utcnow)
    source: str
    target: Optional[str] = None
    evidence: Dict[str, Any] = Field(default_factory=dict)
    reason: Optional[str] = None


class RecoveryCandidate(BaseModel):
    """Durable non-terminal action context used by Phase 4 recovery."""
    incident_id: str
    plan_id: str
    plan_revision: int
    step_id: str
    action_id: str
    idempotency_key: str
    action_name: str
    target: Optional[str] = None
    journal_status: ActionJournalStatus
    latest_attempt_no: Optional[int] = None
    latest_attempt_status: Optional[ActionJournalStatus] = None
    latest_outcome: Optional[ExecutionOutcome] = None
    side_effect_possible: bool = False
    retry_exhausted: bool = False
    request_metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowCursor(BaseModel):
    incident_id: str
    plan_id: str
    plan_revision: int
    current_step_id: Optional[str] = None
    current_action_id: Optional[str] = None
    cursor_status: WorkflowCursorStatus
    updated_at: datetime


class RecoveryLease(BaseModel):
    action_id: str
    owner_id: str
    lease_token: str
    acquired_at: datetime
    expires_at: datetime


class ReconciliationResult(BaseModel):
    decision: ReconciliationDecision
    observation: ExternalStateObservation


class ActionAttemptRecord(BaseModel):
    attempt_id: str
    action_id: str
    attempt_no: int
    status: ActionJournalStatus
    started_at: datetime
    finished_at: Optional[datetime] = None
    outcome: Optional[ExecutionOutcome] = None
    success: Optional[bool] = None
    retryable: Optional[bool] = None
    retry_exhausted: bool = False
    side_effect_possible: bool = False
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    request_metadata: Dict[str, Any] = Field(default_factory=dict)
    response_metadata: Dict[str, Any] = Field(default_factory=dict)


class ActionJournalRecord(BaseModel):
    action_id: str
    incident_id: str
    plan_id: str
    plan_revision: int
    step_id: str
    idempotency_key: str
    action_name: str
    target: Optional[str] = None
    journal_status: ActionJournalStatus
    outcome: Optional[ExecutionOutcome] = None
    current_attempt: int = 0
    retryable: Optional[bool] = None
    retry_exhausted: bool = False
    side_effect_possible: bool = False
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    request_metadata: Dict[str, Any] = Field(default_factory=dict)
    response_metadata: Dict[str, Any] = Field(default_factory=dict)


class RecoveryResult(BaseModel):
    action_id: str
    decision: ReconciliationDecision
    observation: ExternalStateObservation
    action_executed: bool = False
    journal: ActionJournalRecord


class ExecutionResult(BaseModel):
    """
    TimeoutManager 执行结果（可序列化）。

    替代原来的 dataclass，确保审计可记录。
    """
    action_name: Optional[str] = None
    target: Optional[str] = None
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None  # 错误消息（不可序列化异常，存字符串）
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    retry_count: int = 0
    total_duration_ms: float = 0.0
    escalated: bool = False
    final_action: str = ""  # "success" / "escalate" / "timeout" / "circuit_open"
    outcome: ExecutionOutcome = ExecutionOutcome.UNKNOWN
    retry_exhausted: bool = False
    retryable: bool = False
    side_effect_possible: bool = False
    side_effect_confirmed: bool = False


class StateVerificationResult(BaseModel):
    """一次只读状态探针的观测结果。"""
    verified: bool
    target: Optional[str] = None
    expected_state: Optional[Any] = None
    observed_state: Optional[Any] = None
    verifier_name: Optional[str] = None
    checked_at: datetime = Field(default_factory=datetime.utcnow)
    evidence: List[str] = Field(default_factory=list)
    error_type: Optional[str] = None


# ============================================================
# 去重窗口条目（Pydantic 可序列化版本）
# ============================================================

class DedupWindowEntry(BaseModel):
    """
    去重窗口条目（Pydantic 模型）。

    用于去重窗口的可序列化存储和测试验证。
    """
    incident_id: str
    dedup_key: str
    attack_type: str = ""
    source_ip: Optional[str] = None
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    count: int = 1


# ============================================================
# 验证模型
# ============================================================

class VerificationResult(BaseModel):
    """Verifier 输出"""
    incident_id: str
    action_status: ActionStatus
    reason: str = ""
    evidence: List[str] = Field(default_factory=list)
    retry_recommended: bool = False
    compensation_needed: bool = False
    compensation_actions: List[str] = Field(default_factory=list)
    next_state: IncidentState = IncidentState.RESOLVED


# ============================================================
# 审计模型
# ============================================================

class AuditEntry(BaseModel):
    """审计日志条目"""
    audit_id: str = Field(
        default_factory=lambda: f"audit-{uuid.uuid4().hex[:12]}"
    )
    trace_id: str
    incident_id: str
    thread_id: str
    event_sequence: int
    event_type: SSEEventType
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    actor: str = ""  # agent / user / system
    action: str = ""
    detail: Dict[str, Any] = Field(default_factory=dict)
    # 便于检索
    state_from: Optional[IncidentState] = None
    state_to: Optional[IncidentState] = None
    # 当前状态（冗余但便于查询）
    current_state: Optional[IncidentState] = None


# ============================================================
# SSE 事件模型
# ============================================================

class SSEPayload(BaseModel):
    """SSE 事件 Payload"""
    trace_id: str
    incident_id: str
    thread_id: str
    event_sequence: int
    event_type: SSEEventType
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    state: Optional[IncidentState] = None  # 当前业务状态
    data: Dict[str, Any] = Field(default_factory=dict)
    message: str = ""

    def to_sse_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "incident_id": self.incident_id,
            "thread_id": self.thread_id,
            "event_sequence": self.event_sequence,
            "type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            "state": self.state.value if self.state else None,
            "data": self.data,
            "message": self.message,
        }


# ============================================================
# 知识库检索请求
# ============================================================

class KBQueryRequest(BaseModel):
    """知识库查询请求"""
    query: str
    kb_type: str  # "CaseKB" / "RunbookKB" / "TopologyKB"
    top_k: int = 5
    filters: Dict[str, Any] = Field(default_factory=dict)


class KBQueryResult(BaseModel):
    """知识库查询结果"""
    kb_type: str
    query: str
    documents: List[str] = Field(default_factory=list)
    scores: List[float] = Field(default_factory=list)
    metadata_list: List[Dict[str, Any]] = Field(default_factory=list)


# ============================================================
# 超时 / 熔断配置
# ============================================================

class RetryPolicy(BaseModel):
    """重试策略"""
    max_retries: int = Field(default=3, ge=0)
    base_delay_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 60.0
    retryable_exceptions: List[str] = Field(
        default_factory=lambda: ["TimeoutError", "ConnectionError", "MockFailure"]
    )
    retryable_status_codes: List[int] = Field(
        default_factory=lambda: [408, 429, 500, 502, 503, 504]
    )
    non_retryable_exceptions: List[str] = Field(
        default_factory=lambda: [
            "PermissionDenied",
            "PermissionDeniedError",
            "InvalidParameter",
            "InvalidParameterError",
            "BadRequest",
            "BadRequestError",
            "ValidationError",
        ]
    )
    non_retryable_status_codes: List[int] = Field(
        default_factory=lambda: [400, 401, 403, 404, 405, 409, 422]
    )


class CircuitBreakerConfig(BaseModel):
    """熔断器配置"""
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0
    half_open_max_requests: int = 3


class TimeoutConfig(BaseModel):
    """超时配置"""
    default_timeout_seconds: float = 30.0
    approval_timeout_minutes: int = 10
    tool_timeout_seconds: float = 15.0
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)


# ============================================================
# AIOps Fusion: FailureContext & RecoveryState
# ============================================================

class FailureContext(BaseModel):
    """
    当 Event Driven Workflow 失败时，传递给 Legacy PRP Recovery Engine 的上下文。

    不包含原始事件的全部细节，只包含 PRP Planner 需要的关键信息。
    """
    incident_id: str = Field(description="关联的事件 ID")
    thread_id: str = Field(description="LangGraph thread ID")
    trace_id: str = Field(description="全链路追踪 ID")

    # ---- 失败原因 ----
    failure_reason: str = Field(
        default="",
        description="失败原因描述（如: Action 执行失败 / Runbook 匹配失败 / 置信度不足）"
    )
    failure_category: str = Field(
        default="unknown",
        description="失败类别: action_failed / metric_unrecovered / incident_cleared_failed / "
                    "mcp_tool_failed / timeout / confidence_low / runbook_match_failed / unknown"
    )

    # ---- 已执行信息 ----
    executed_actions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="已执行的 Mock 动作结果列表"
    )
    triage_result: Optional[Dict[str, Any]] = Field(
        default=None,
        description="TriageAgent 分诊结果（含根因、影响范围）"
    )
    plan_steps: List[str] = Field(
        default_factory=list,
        description="已生成的处置计划步骤"
    )

    # ---- 当前状态 ----
    current_state: str = Field(
        default="FAILED",
        description="当前 IncidentState"
    )
    retry_cycles: int = Field(
        default=0,
        description="已执行的重试轮次"
    )

    # ---- 额外上下文 ----
    prometheus_metrics: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Prometheus 指标快照（如有）"
    )
    history: List[str] = Field(
        default_factory=list,
        description="处理历史摘要"
    )
    extra: Dict[str, Any] = Field(
        default_factory=dict,
        description="额外上下文信息"
    )

    def to_planner_input(self) -> str:
        """将 FailureContext 格式化为 Planner 可理解的文本输入"""
        parts = [
            f"## 故障恢复任务",
            f"事件 ID: {self.incident_id}",
            f"失败原因: {self.failure_reason}",
            f"失败类别: {self.failure_category}",
            f"当前状态: {self.current_state}",
            f"已重试轮次: {self.retry_cycles}",
        ]

        if self.triage_result:
            triage = self.triage_result
            parts.append(f"\n## 分诊结果")
            parts.append(f"根因: {triage.get('root_cause', '未知')}")
            parts.append(f"严重级别: {triage.get('severity', '未知')}")
            parts.append(f"置信度: {triage.get('confidence', 0)}")
            impact = triage.get('impact_scope', [])
            if impact:
                parts.append(f"影响范围: {', '.join(impact)}")

        if self.plan_steps:
            parts.append(f"\n## 已执行的处置计划")
            for i, step in enumerate(self.plan_steps, 1):
                parts.append(f"  {i}. {step}")

        if self.executed_actions:
            parts.append(f"\n## 已执行的行动结果")
            for action in self.executed_actions:
                action_name = action.get('action_name', 'unknown')
                success = action.get('success', False)
                message = action.get('message', '')
                parts.append(f"  - {action_name}: {'OK' if success else 'FAIL'} {message}")

        if self.history:
            parts.append(f"\n## 处理历史")
            for h in self.history:
                parts.append(f"  - {h}")

        parts.append(
            f"\n请根据以上信息，制定恢复计划。"
            f"注意：常规处置手段已失败，需要探索新的恢复策略。"
        )
        return "\n".join(parts)


class RecoveryState(str, Enum):
    """Recovery Engine 状态"""
    IDLE = "IDLE"                    # 未触发恢复
    RECOVERY_STARTED = "RECOVERY_STARTED"      # 恢复已启动
    PLANNING = "PLANNING"            # Planner 制定恢复计划
    EXECUTING = "EXECUTING"          # Executor 执行恢复步骤
    VERIFYING = "VERIFYING"          # Verifier 验证恢复结果
    REPLANNING = "REPLANNING"        # Replanner 重新调整
    RECOVERY_SUCCESS = "RECOVERY_SUCCESS"     # 恢复成功
    RECOVERY_FAILED = "RECOVERY_FAILED"      # 恢复失败（触发 Safety Control）
    SAFETY_ROLLBACK = "SAFETY_ROLLBACK"      # 安全回滚中
    SAFETY_ESCALATION = "SAFETY_ESCALATION"  # 安全升级中


# Recovery Engine 配置常量
MAX_RECOVERY_ATTEMPTS = 3
