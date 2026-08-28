# railways_V.2 真实请求运行故事

> 本文用一个“最典型的业务请求”完整讲述系统如何从输入跑到输出。
> 案例选择：`POST /api/aiops/metrics`（原始监测指标入口），因为它是当前系统中最能体现“ML 威胁检测 + Agent 诊断 + Workflow 处置”价值的真实链路。
> 事实依据：当前源码（`app/`）与 `docs/PROJECT_SOURCE_CODE_WALKTHROUGH.md`。所有调用关系均来自源码，不是文档推测。

## 1. 故事：一列车的通信指标出现异常

假设现在是某个值班场景：运维人员发现列车 `T001` 与信号设备 `S001` 之间的通信指标异常，丢包率明显上升、延迟升高。值班员把原始监测指标直接发送给系统：

```json
{
  "train_id": "T001",
  "signal_id": "S001",
  "timestamp": "2026-08-15T10:00:00",
  "metrics": {
    "speed": 120.0,
    "packet_loss": 0.35,
    "latency": 250.0,
    "distance": 1200.0,
    "renewal_interval": 6000.0,
    "burstiness": 0.8,
    "signal_status": "RED",
    "overlap_status": "NORMAL"
  }
}
```

> 说明：以上 JSON 是符合接口字段的示例数据；其中的攻击结论（例如“模型预测 Jamming”）只作为故事演示，不代表真实预测结果。

系统收到后会返回一条 SSE 事件流，事件会一路从“事件创建 → 分级 → ML 检测 → 诊断 → 计划 → 执行 → 验证 → 解决”推进。下面是这个请求的完整运行故事。

## 2. 逐步故事

### ① 发生了什么

列车 T001 与信号设备 S001 的通信质量下降，`packet_loss=0.35`、`latency=250ms`，可能构成网络安全威胁（DoS、Jamming 或 Replay Attack）。

### ② 系统接收到什么

客户端调用 `POST /api/aiops/metrics?session_id=...`，body 是上面的原始指标 JSON。

- 输入：原始指标 dict（当前 API 接收普通 `dict`，没有强制 Pydantic schema）。
- 谁处理：`app/api/aiops.py` 的 `process_metrics_stream`。
- 做什么：把请求包装为 `EventSourceResponse`，并把 payload 交给 `AIOpsService.process_metrics`。
- 输出：一个异步 SSE 生成器，后续所有阶段事件都从这里返回。
- 为什么进入下一步：服务层需要把“HTTP 请求”转成“内部事件”。

### ③ 服务层把指标变成事件

`AIOpsService.process_metrics` 把原始 payload 组装成 `raw_event`：

```text
metrics_snapshot = payload（完整指标）
train_id / signal_id = payload 中的字段
description = 自动生成的指标事件描述
```

然后调用 `process_incident(raw_event, source=IncidentSource.STSRS, session_id=...)`，并把 `thread_id` 设为 `thread-{session_id}`。

- 输入：原始指标 payload。
- 谁处理：`AIOpsService.process_metrics`。
- 做什么：构造 `raw_event`，进入统一事件入口。
- 输出：`process_incident` 的异步生成器。
- 为什么进入下一步：`process_incident` 负责调用 `IncidentRouter.route`，即主工作流。

### ④ 主工作流开始：归一化、去重、分级

`IncidentRouter.route` 是 AIOps 主流水线：

1. `EventNormalizer.normalize(raw_event, source)` 生成 `Incident`（事件实体，携带 `metrics_snapshot`）。
2. 因为这是带 `metrics` 的实时指标（source=STSRS），按 `_should_process_single_metric` 逻辑绕过缓冲去重，直接继续。
3. `SeverityEngine.evaluate(incident)` 根据指标影响给出严重级别（如 P1/P2/P3）。

- 输入：`raw_event` + source + thread_id。
- 谁处理：`IncidentRouter.route` + `app/events/*`。
- 做什么：把原始数据归一化为内部事件模型，并完成初步分级。
- 输出：`INCIDENT_CREATED`、`STATE_CHANGED` 等 SSE 事件；一个已分级的 `Incident`。
- 为什么进入下一步：事件需要先回答“这是什么级别的威胁”，再进入 ML 检测。

### ⑤ ML 检测：谁攻击了我

`IncidentRouter` 通过 `create_attack_detector()` 创建检测器（配置 `ml_attack_detector_backend="zl"`，默认 ZL 后端）。当 `incident.metrics_snapshot` 存在时：

1. `_build_metric_record(incident)` 从快照重建 `RailMetricRecord`。
2. `attack_detector.predict(record)` 调用 `ZLAttackDetector`：
   - 加载 ZL V2 pickle（HistGradientBoostingClassifier）；
   - `ZLFeatureAdapter` 提取特征，顺序固定为 `Distance, PacketLoss, Latency`；
   - 调用 `predict_proba()` 得到各类别概率；
   - 映射标签：`Normal -> UNKNOWN`、`DoS -> DoS`、`Jamming -> Jamming`、`ReplayAttack -> Replay Attack`；
   - 输出 `AttackPrediction`（attack_type、confidence、probabilities、detector_backend、fallback_used 等）。
3. 预测结果写入 `incident.attack_prediction`，并发出 `INCIDENT_TRIAGED` SSE。

- 输入：`RailMetricRecord`。
- 谁处理：`ZLAttackDetector.predict`（ML 推理）。
- 做什么：先回答 “What happened?”（攻击类型判断）。
- 输出：`AttackPrediction`，进入后续 LLM 诊断。
- 为什么进入下一步：`TriageAgent` 需要把 ML 预测解释成“影响、原因、怎么办”。

### ⑥ 进入通用处置流水线（Agent + Workflow）

`IncidentRouter._common_pipeline` 按状态机推进：

```text
NEW → TRIAGED → PLANNED → EXECUTING → VERIFIED → RESOLVED
```

- Step A（TRIAGED）：`TriageAgent.triage(incident)` 分析指标异常模式、查询 TopologyKB / CaseKB（RAG 检索），把 ML 预测 + 异常模式 + 知识库上下文注入 Prompt，调用 LLM（qwen-max）生成 `TriageResult`（attack_type、root_cause、severity、confidence、evidence）。LLM 失败时使用 `_fallback_triage` 规则诊断。
- Step B（PLANNED）：`RunbookAgent.generate_plan(incident, triage_result)` 查询 CaseKB / RunbookKB / TopologyKB，调用 LLM 生成 `RunbookPlan`（步骤列表、是否需审批）。LLM 失败时使用 `_fallback_plan`。
- Step C（EXECUTING）：`ActionOrchestrator.execute_plan` 逐个执行 Mock 动作（如 `switch_backup_link`、`restart_gateway`），高风险动作走 `ApprovalGate`（Mock 模式自动批准），单动作超时 15 秒并支持重试。
- Step D：`Verifier.verify` 汇总执行结果，判断 SUCCESS / RETRY / COMPENSATE / ESCALATE。
- Step E：`Replanner.decide` 进入决策循环（最多 3 轮）：
  - `RESOLVE` → VERIFIED → RESOLVED；
  - `RETRY` → 重新执行计划；
  - `COMPENSATE` → 执行补偿（rollback）动作并验证；
  - `ESCALATE` → 升级人工；
  - `FAIL` → FAILED。

- 输入：`Incident`（含 `attack_prediction`）。
- 谁处理：`TriageAgent` / `RunbookAgent`（LLM + RAG）、`ActionOrchestrator`（工具）、`Verifier` / `Replanner`（确定性决策）。
- 做什么：把“检测结果”变成“可执行处置闭环”。
- 输出：一系列审计/SSE 事件（triaged、plan_generated、action_executed、verification_finished、incident_resolved 等）。
- 为什么进入下一步：处置完成后需要持久化并返回最终结果。

### ⑦ 最终输出

`IncidentRouter` 最后 yield `complete` 事件，携带：

```text
final_state（如 RESOLVED）
workflow_failed（false/true）
failure_context（仅失败时）
triage / plan / execution_results / verification
event_sequence（审计序号）
```

`AIOpsService.process_incident` 把完整事件流交给 `process_metrics_stream`，后者通过 `EventSourceResponse` 返回给客户端，并在遇到 `complete` / `error` 事件时结束 SSE。

同时，`AuditStore.record` 会写入审计日志并广播给订阅者；客户端也可以调用 `GET /api/aiops/sse/{thread_id}` 订阅同一线程的事件流，或通过事件详情/时间线接口回放。

## 3. 真实调用链（源码级）

```text
POST /api/aiops/metrics
  ↓
app/api/aiops.py
  process_metrics_stream(payload: dict, session_id: str = Query(None))
  ↓
AIOpsService.process_metrics(metrics_payload, session_id)
  ↓ 构造 raw_event（metrics_snapshot=train_id/signal_id/source_files/record_id）
AIOpsService.process_incident(raw_event, source=IncidentSource.STSRS, session_id)
  ↓
IncidentRouter.route(raw_event, source, thread_id)
  ├─ EventNormalizer.normalize(...)              # app/events/event_normalizer.py
  ├─ Deduplicator（实时单指标绕过缓冲去重）        # app/events/deduplicator.py
  ├─ SeverityEngine.evaluate(...)                # app/events/severity_engine.py
  ├─ create_attack_detector()                    # app/ml/attack_detector.py（zl + rule fallback）
  │   └─ ZLAttackDetector.predict(...)           # app/ml/zl_attack_detector.py
  │       └─ ZLFeatureAdapter.to_raw_input()     # 特征: Distance, PacketLoss, Latency
  │       └─ classifier.predict_proba()          # HistGradientBoostingClassifier
  │   └─ AttackPrediction -> incident.attack_prediction
  ├─ incident_store.create(...)                  # app/core/incident_store.py
  ├─ state_machine.transition(NEW)               # app/core/state_machine.py
  └─ _common_pipeline(incident, thread_id, record)
      ├─ TriageAgent.triage(incident)            # app/agents/triage_agent.py
      │   ├─ _analyze_metric_anomalies()
      │   ├─ _query_topology() / _query_casekb_by_prediction()
      │   │   └─ retrieve_knowledge(...)         # app/tools/knowledge_tool.py
      │   │       └─ VectorStoreManager -> Milvus + DashScopeEmbeddings
      │   └─ LLM: TRIAGE_PROMPT | ChatQwen.with_structured_output(TriageResult)
      ├─ RunbookAgent.generate_plan(...)         # app/agents/runbook_agent.py
      │   ├─ _query_casekb() / _query_runbookkb() / _query_topologykb()
      │   └─ LLM: RUNBOOK_PROMPT | ChatQwen.with_structured_output(RunbookPlan)
      ├─ ActionOrchestrator.execute_plan(...)    # app/agents/action_orchestrator.py
      │   ├─ ApprovalGate（高风险动作 Mock 自动批准）
      │   └─ mock_actions（switch_backup_link / restart_gateway / ...）
      ├─ Verifier.verify(...)                    # app/agents/verifier.py
      └─ Replanner.decide(...)                   # app/agents/replanner.py
          ├─ RESOLVE → VERIFIED → RESOLVED
          ├─ RETRY / COMPENSATE / ESCALATE / FAIL
          └─ final complete event + FailureContext（失败时）
  ↓
AuditStore.record(...)                           # app/core/audit_store.py（审计 + SSE 广播）
  ↓
EventSourceResponse 返回 SSE（complete 后结束）
```

## 4. 异常路径

### 4.1 正常路径

指标正常处理 → ML 输出预测 → Triage 诊断 → Runbook 计划 → 动作执行成功 → Verifier 判定 SUCCESS → Replanner 判定 RESOLVE → 状态 `RESOLVED` → SSE `complete`（`workflow_failed=false`）。

### 4.2 ML 模型失败怎么办

检测器有专门的分层：

- `FallbackAttackDetector`：当主检测器抛 `AttackDetectorLoadError`（模型文件/依赖加载失败）时，回退到 `RuleBasedAttackDetector`，输出 `detector_backend="rule"`、`fallback_used=true`。
- 输入契约错误（`AttackDetectorInputError`）或推理契约错误（`AttackDetectorInferenceError`）**不允许 fallback**，直接抛出。
- `IncidentRouter` 中检测调用被 `try/except` 包裹：即使检测失败，也记录 warning 后继续流程，由 `TriageAgent` 独立诊断，不阻断处置。

### 4.3 RAG 没有结果怎么办

`retrieve_knowledge` 在没有命中文档或检索异常时返回 `"没有找到相关信息。"` 与空文档列表，不抛异常。`TriageAgent` / `RunbookAgent` 会记录“未命中”，继续使用异常模式、拓扑信息或规则回退生成结果，不会中断事件处置。

### 4.4 Tool 调用失败怎么办

- 知识检索工具：异常被捕获，返回错误文本。
- MCP 工具（对话链路）：`load_mcp_tools_safe` 失败时仅使用本地工具，不阻断对话。
- Mock 动作：`ActionOrchestrator` 通过 `TimeoutManager` 对单动作设 15 秒超时和重试；失败动作由 `Verifier` 判定 RETRY / COMPENSATE / ESCALATE。

### 4.5 Agent / Workflow 某一步失败怎么办

- `TriageAgent` / `RunbookAgent`：LLM 调用失败 → 使用规则回退（`_fallback_triage` / `_fallback_plan`），流程继续。
- Workflow 进入 `FAILED` / `ESCALATED`：`IncidentRouter` 生成 `FailureContext`，`complete` 事件带 `workflow_failed=true`。
- `AIOpsService` 收到失败上下文后启动 PRP 恢复引擎（LangGraph：`planner → executor → replanner`），最多按 `MAX_RECOVERY_ATTEMPTS` 尝试。
- 恢复失败 → `SafetyControl`：执行回滚动作（解除封禁、恢复链路、验证网络健康），并升级人工。
- 最终仍会 yield 一个 `complete` 事件（含 `final_state`、`recovery_attempted`、`recovery_success`）。

### 4.6 失败时如何结束

- 流程异常：`AIOpsService` 捕获后 yield `error` SSE；`process_metrics_stream` 遇到 `error` / `complete` 会结束 SSE。
- 业务失败：状态机进入 `FAILED` / `ESCALATED`，事件流以 `workflow_failed` → `recovery_*`（可选）→ `complete` 结束，事件详情与审计仍可查询回放。

## 5. 本案例中哪些模块没有参与

- MCP：`POST /api/aiops/metrics` 主链路**不使用 MCP**；MCP 客户端只在 RAG 对话 Agent（`RagAgentService`）中加载 CLS / Monitor 工具。
- Prometheus 查询工具：`query_prometheus_alerts` 是对话 Agent 的本地工具，本指标处置链路不调用。
- PRP 恢复图：正常路径不参与；只有 Workflow 失败（`workflow_failed=true`）才启动。

## 6. 事实核对清单

- 真实入口：`POST /api/aiops/metrics`（`app/api/aiops.py`），body 为普通 `dict`。
- 真实服务：`AIOpsService.process_metrics` → `process_incident`。
- 真实主工作流：`IncidentRouter.route` → `_common_pipeline`（Python async generator，非 LangGraph）。
- ML 参与：`ZLAttackDetector.predict`（真实 ZL V2 `predict_proba`），输出 `AttackPrediction`。
- RAG 参与：`TriageAgent` / `RunbookAgent` 调用 `retrieve_knowledge`（Milvus + DashScope Embedding）。
- Agent 参与：`TriageAgent`、`RunbookAgent`（LLM + 规则回退）；`ActionOrchestrator`、`Verifier`、`Replanner` 为确定性处置组件。
- Workflow 参与：事件处置主流水线；失败后进入 PRP 恢复图。
- Tool 参与：知识检索工具、Mock 动作工具。
- MCP：本案例未参与。
- 最终输出：SSE 事件流（`EventSourceResponse`），`complete` 后结束；审计与事件可查询/回放。
- Fallback：ML 加载失败 → rule 检测器；LLM 失败 → 规则回退；RAG 无结果 → 继续流程；恢复失败 → SafetyControl。