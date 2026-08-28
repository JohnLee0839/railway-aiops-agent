# 第二册：Multi-Agent 架构深度面试

> 基于 RailOps Agent 源码 — 6 个独立 Agent + 新/旧双链路架构

---

## Q1：为什么 RailOps 将 Agent 拆分为 TriageAgent / RunbookAgent / ActionOrchestrator / Verifier / Replanner 五个独立 Agent？

### 标准答案

**源码体现：** `app/agents/` 目录下 5 个独立文件：

| Agent | 文件 | 职责 | 输入 | 输出 | 是否调 LLM |
|-------|------|------|------|------|-----------|
| TriageAgent | `triage_agent.py:48` | 攻击分析+影响范围 | `Incident` | `TriageResult` | ✅ |
| RunbookAgent | `runbook_agent.py:76` | 处置计划生成 | `Incident` + `TriageResult` | `RunbookPlan` | ✅ |
| ActionOrchestrator | `action_orchestrator.py:173` | 执行动作 | `Incident` + `RunbookPlan` | `List[MockActionResult]` | ❌ |
| Verifier | `verifier.py:25` | 验证结果 | `Incident` + `plan` + `results` | `VerificationResult` | ❌ |
| Replanner | `replanner.py:40` | 路由决策 | `VerificationResult` | `ReplanAction` | ❌ |

**核心原因：**
这是 **单一职责原则(SRP)** 在 Multi-Agent 架构中的工程化落地。Agent 之间存在明确的**决策边界**：
- LLM Agent（TriageAgent, RunbookAgent）：处理"需要理解"的任务
- 规则 Agent（ActionOrchestrator, Verifier, Replanner）：处理"需要可靠"的任务

### 追问 #1：为什么 Verifier 不调 LLM？用规则验证是否太粗糙？

**答案：** `app/agents/verifier.py:65-125` 中 Verifier 的判断逻辑：
```python
success_count = sum(1 for r in results if r.success)
failure_count = len(results) - success_count

if success_count == len(results):
    return ActionStatus.SUCCESS
elif escalated:
    return ActionStatus.ESCALATE
elif retry_cycle < self.max_retry_cycles:
    return ActionStatus.RETRY
elif failure_count > 0:
    return ActionStatus.COMPENSATE
```

**设计原因：**
1. **确定性**：`success_count == len(results)` 是布尔表达式，100% 确定。LLM 可能对"是否足够好"给出不同判断。
2. **审计合规**：验证结果需要在审计中可复现。LLM 的非确定性会破坏审计回溯。
3. **延迟**：LLM 调用增加 1-3 秒延迟，在故障处理链路中不必要。

### 追问 #2：为什么 Replanner 也不调 LLM？

**答案：** `app/agents/replanner.py:113-136`
```python
def _map_status(self, verification: VerificationResult) -> ReplanAction:
    if status == ActionStatus.SUCCESS:
        return ReplanAction.RESOLVE
    elif status == ActionStatus.RETRY:
        self.retry_count += 1
        if self.retry_count > self.max_retries:
            return ReplanAction.ESCALATE
        return ReplanAction.RETRY
    ...
```

Replanner 是**纯映射函数**——直接将 `ActionStatus` 映射为 `ReplanAction`。如果让 LLM 来做这个决策，可能会：
- 因为"觉得还能再试试"而忽略重试上限
- 因为"过于谨慎"而不必要地升级
- 产生无法解释的决策（黑盒）

### 追问 #3：这和旧链路的 Planner → Executor → Replanner（三节点）有什么本质区别？

**答案：**

| 维度 | 旧链路（3节点） | 新链路（5 Agent） |
|------|----------------|-------------------|
| 职责边界 | 模糊（Planner 兼做检索+计划） | 清晰（每个 Agent 一个职责） |
| LLM 使用 | 全部节点调 LLM | 仅分诊和计划调 LLM |
| 输出格式 | `str` (plan steps 是字符串列表) | `Pydantic Schema`（TriageResult, RunbookPlan...） |
| 补偿机制 | 无 | 完整（compensate + rollback） |
| 审批机制 | 无 | ApprovalGate |
| 状态机 | 无（靠 should_continue） | 9 状态 + 合法迁移表 |

### 追问 #4：Agent 之间如何传递上下文？上下文丢失怎么办？

**答案：** 三种传递方式：

1. **Pydantic Model 传递**（结构化数据）：
```python
# incident_router.py:171
triage_result = await self.triage_agent.triage(incident)
# incident_router.py:201
plan = await self.runbook_agent.generate_plan(incident, triage_result)
```

2. **IncidentStore 共享**（跨 Agent 状态）：
```python
# incident_store.py:36-62
record = incident_store.create(incident, thread_id)
record.triage_result = triage_result.model_dump()
incident_store.update(record)
```

3. **AuditStore 关联**（全链路追踪）：
```python
# audit_store.py:47-126
# 每次决策和动作都写入审计，trace_id 串联全链路
```

### 追问 #5：如果 TriageAgent 分析出错导致后续全部错误，怎么办？

**答案：** 当前设计中没有 "Triage 验证" 机制。这是一个已知的架构风险。优化方案：
1. 引入 **Confidence 阈值**：`TriageResult.confidence` (`incident.py:226-231`) 低于 0.5 时转人工审核
2. 引入 **双 Triage 交叉验证**：两个独立的 TriageAgent 分诊，结果不一致则升级
3. 引入 **Time-travel 回滚**：如果后续发现分诊错误，通过 `COMPENSATING` 状态回退到 `TRIAGED`

---

## Q2：为什么 ActionOrchestrator 需要 ApprovalGate？

### 标准答案

```python
# app/agents/action_orchestrator.py:39-170
class ApprovalGate:
    HIGH_RISK = {STOP_TRAIN, BLOCK_SECTION, EMERGENCY_SHUTDOWN}

    def requires_approval(self, action_name: str) -> bool:
        return action_name in {"STOP_TRAIN", "BLOCK_SECTION", "EMERGENCY_SHUTDOWN"}
```

**核心原因：**
在铁路信号系统中，停止列车、封锁区段、紧急关停是**不可逆的高风险操作**。Agent 可以建议这些操作，但**不能自主执行**。这是典型的 **Human-in-the-Loop (HITL)** 设计。

### 追问 #1：审批超时后怎么处理？

**答案：** `app/agents/action_orchestrator.py:141-163`
```python
def timeout(self, request_id: str) -> ApprovalRequest:
    req.status = ApprovalStatus.TIMEOUT
    req.escalated_at = datetime.utcnow()
    # 超时自动转 ESCALATE
```
超时 = 自动升级。默认 10 分钟超时 (`config.approval_timeout_minutes = 10`)。这避免了审批人不在线时系统卡死。

### 追问 #2：当前 Mock 模式直接 auto-approve，生产环境怎么改？

**答案：** `app/agents/action_orchestrator.py:238-242`
```python
# 模拟审批等待（真实环境通过 SSE 回调）
# 此处跳过审批等待，直接执行（Mock 模式下默认批准）
logger.info(f"[ActionOrchestrator] Mock 模式: 自动批准 {action_name}")
self.approval_gate.approve(approval_req.request_id)
```
生产环境改为：
1. 发送 SSE 事件 `APPROVAL_REQUIRED` 给前端
2. 前端展示审批 UI
3. 运维人员点击 Approve/Deny → 回调 API → `approval_gate.approve(request_id)`
4. SSE 推送结果继续执行

### 追问 #3：为什么 ApprovalGate 是 ActionOrchestrator 的内部类，而非独立模块？

**答案：** 因为审批是 "执行流程的一个环节"，而非独立的业务逻辑。它紧密耦合于 ActionOrchestrator 的执行上下文。但作为 Trade-off，这使得 ApprovalGate 难以被其他模块复用。

---

## Q3：旧链路的 Planner 和新链路的 TriageAgent + RunbookAgent 有什么设计哲学上的区别？

### 标准答案

**旧链路 Planner** (`app/agent/aiops/planner.py:63`)：
- 一个 LLM 调用完成所有规划（经验检索 + 工具分析 + 计划生成）
- 输出是 `List[str]`（简单字符串列表）
- 无结构化字段验证

**新链路 TriageAgent + RunbookAgent**：
- 两个 LLM 调用，职责分离
- 输出是 `TriageResult` + `RunbookPlan`（Pydantic Schema）
- 包含 `confidence`、`source_kb`、`reasoning` 等可解释性字段

### 追问 #1：为什么新链路的 RunbookPlan 包含 `confidence` 和 `reasoning` 字段？

**答案：** `app/models/incident.py:264-274`
```python
confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="计划置信度")
reasoning: str = Field(default="", description="决策推理过程")
```

这是**可解释 AI (XAI)** 的体现。当事故复盘时，团队需要知道"为什么当时执行了这个计划"，而不仅仅是"执行了什么计划"。

### 追问 #2：RunbookAgent 的 KB 检索优先级为什么是硬编码的？

**答案：** `app/agents/runbook_agent.py:117-127`
```python
# 优先级 1: CaseKB（历史案例）
casekb_context = await self._query_casekb(incident, triage_result)
# 优先级 2: RunbookKB（SOP）
if not casekb_context:
    runbook_context = await self._query_runbookkb(incident, triage_result)
# 优先级 3: TopologyKB（拓扑）
topology_context = await self._query_topologykb(incident, triage_result)
```

如果让 LLM 决定检索优先级，LLM 可能：
- 跳过 CaseKB 直接使用 SOP（因为 SOP 更"通用"）
- 忽略 TopologyKB（因为觉得"不相关"）

但运维领域的最佳实践是：**历史案例 > 通用 SOP > 拓扑推断**。这个优先级应该在代码层硬编码而非交给 LLM。

### 追问 #3：新链路中 TriageAgent 和 RunbookAgent 各自查询了 RAG，是否重复？

**答案：** 两次查询的目的不同：
- TriageAgent: 查询 `TopologyKB`（拓扑影响）+ `CaseKB`（辅助判断）
- RunbookAgent: 查询 `CaseKB`（处置方案）+ `RunbookKB`（SOP）+ `TopologyKB`（影响范围）

存在部分重叠（CaseKB 被查了两次），但查询内容不同。优化方案：将 RunbookAgent 的 CaseKB 查询缓存（因为 TriageAgent 已查过），通过 `IncidentStore` 传递缓存结果。

### 追问 #4：如果 RunbookAgent 返回 `source_kb = "CaseKB"` 但实际没有从 CaseKB 获取到有效信息会怎样？

**答案：** `app/agents/runbook_agent.py:146-148`
```python
if not plan.source_kb:
    plan.source_kb = source_kb or "TopologyKB"
```
有一个补丁逻辑：如果 LLM 没有返回 `source_kb`，则用代码层判断的结果。但这里存在不一致风险——LLM 可能错误标注来源。应该完全由代码层设置 `source_kb`，而非信任 LLM。

---

## Q4：EventNormalizer → Deduplicator → SeverityEngine 为什么设计成独立模块而非 Agent 的一部分？

### 标准答案

```python
# app/events/__init__.py:6-15
from app.events.event_normalizer import EventNormalizer
from app.events.deduplicator import Deduplicator
from app.events.severity_engine import SeverityEngine
from app.events.timeout_manager import TimeoutManager
```

这三个模块在 `IncidentRouter` 中被顺序调用（`app/core/incident_router.py:109-144`），但它们不是 Agent，而是**纯事件处理管道**。原因：
1. **不需要 LLM**：归一化、去重、分级都是确定性逻辑
2. **必须快速**：事件处理需要毫秒级响应，不能有 LLM 延迟
3. **必须可靠**：归一化错误会导致整个链路失败，不能依赖概率性输出

### 追问 #1：Deduplicator 的滑动窗口为什么是 10 秒和阈值 3？

**答案：** `app/events/deduplicator.py:38`
```python
def __init__(self, window_seconds: float = 10.0, threshold: int = 3):
```

10 秒窗口 + 阈值 3 意味着：10 秒内收到 3 条相同事件 → 合并为 1 条。这是针对告警风暴（Alert Storm）的防护——一个故障可能在短时间内触发几十条告警。具体数值需要根据实际运维环境调整。

### 追问 #2：去重 Key 的 7 级回退策略是如何设计的？

**答案：** `app/events/deduplicator.py:147-183`
```python
def _make_keys(self, incident: Incident) -> List[str]:
    keys = []
    if incident.dedup_key:           # Key 0: 由EventNormalizer统一生成
        keys.append(incident.dedup_key)
    if incident.event_signature:     # Key 1: attack_type:资产标识
        keys.append(incident.event_signature)
    if incident.source_ip:           # Key 2: IP
        keys.append(f"ip:{incident.source_ip}")
    # ... Key 3-6 逐级回退
    keys.append(f"atk:{incident.attack_type.value}")  # Key 6: 最模糊
```

设计思想：从最精确到最模糊逐级回退。如果 source_ip 为空（如手工输入），仍能基于 attack_type 去重。

### 追问 #3：为什么 SeverityEngine 有列车相关事件的升级规则？

**答案：** `app/events/severity_engine.py:75-82`
```python
if incident.metadata.train_id and severity in (Severity.P3, Severity.P4):
    old = severity
    severity = Severity.P2 if severity == Severity.P3 else Severity.P3
```
这是**领域知识硬编码**——在铁路信号系统中，任何影响列车运行的事件都应该被更高优先级处理。不能依赖 LLM 来判断这个规则。

---

## Q5：新链路和旧链路如何选择？IncidentRouter 的路由规则是什么？

### 标准答案

```python
# app/core/incident_router.py:79-91
def should_use_new_link(self, raw_event, source):
    if source == IncidentSource.STSRS:
        return True           # STSRS 强制走新链路
    if source in (IncidentSource.PROMETHEUS, IncidentSource.MCP):
        return True           # Prometheus/MCP 走新链路
    if source == IncidentSource.MANUAL:
        return bool(raw_event.get("attack_type") or ...)  # 有attack_type走新链路
    return False
```

### 追问 #1：为什么 STSRS 强制走新链路？

**答案：** STSRS (列车信号安全系统) 是最高优先级的事件来源。新链路有完整的 TriageAgent→RunbookAgent→KB 检索→补偿机制，比旧链路的 Plan-Execute-Replan 更适合处理安全关键事件。

### 追问 #2：旧链路未来会被完全移除吗？

**答案：** 当前 `AIOpsService` (`app/services/aiops_service.py:38-55`) 同时持有新链路 `IncidentRouter` 和旧链路 `StateGraph`。旧链路有 "简单输入 → 自动诊断" 的便利性，适合 Demo 和快速测试。完全移除需要等新链路的 `MANUAL` 源支持完善。

---

**Multi-Agent 架构深度面试 — 本章结束**

关键文件索引：
- `app/agents/triage_agent.py` — TriageAgent（分诊）
- `app/agents/runbook_agent.py` — RunbookAgent（计划生成）
- `app/agents/action_orchestrator.py` — ActionOrchestrator（含 ApprovalGate）
- `app/agents/verifier.py` — Verifier（独立验证）
- `app/agents/replanner.py` — Replanner（5态路由）
- `app/events/event_normalizer.py` — 异构事件归一化
- `app/events/deduplicator.py` — 滑动窗口去重
- `app/events/severity_engine.py` — 严重级别引擎
- `app/core/incident_router.py` — AttackType Pipeline 路由
- `app/core/state_machine.py` — 9状态合法迁移表
