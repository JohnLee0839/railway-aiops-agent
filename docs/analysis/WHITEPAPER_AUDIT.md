# 《PROJECT_SOURCE_CODE_WALKTHROUGH.md》二次源码审计报告

> 审计对象：`docs/PROJECT_SOURCE_CODE_WALKTHROUGH.md`（1515 行）
> 事实来源：当前 `railways_V.2` 仓库源码、`.env`、测试、`D:/STUDY/ZL` 模型产物与 manifest
> 审计时间：2026-08-14

## 1. 审计结论摘要

白皮书的总体架构判断基本成立：AIOps 主流水线与 RAG 对话链路分离、ML 检测节点位于 Severity 与 Triage 之间、PRP 作为失败恢复引擎、MCP 只服务对话 Agent 与 PRP，这些核心结论均有源码依据。

但审计发现若干会**改变白皮书核心生命周期结论**的问题，其中最严重的是：

1. 真实 `Replanner` 会在 `decide()` 内先做状态迁移，随后 `_common_pipeline` 又对同一目标状态重复迁移，导致成功、补偿、升级、失败分支都会在真实链路中抛 `ValueError`。现有 Router E2E 测试用 `StubReplanner` 掩盖了该问题。
2. 恢复成功与 Safety Control 都尝试把已处于严格终态（FAILED/ESCALATED）的 Incident 再迁移到 RESOLVED/ESCALATED，按状态机必然失败并被静默吞掉，白皮书 7.8 的“恢复成功→RESOLVED”“状态转 ESCALATED”不成立。
3. `recovery_attempt` 从未递增，`MAX_RECOVERY_ATTEMPTS=3` 的检查实际不可达；白皮书 7.8/9.4 描述的“最多 3 次恢复尝试”与源码行为不符。
4. `.env` 实际把 CLS 配置为 `sse` + `http://localhost:3000/sse`，白皮书 8.1/8.5 却把 CLS 画成 `streamable-http:8003`，并错误引用 `.env:L16-L22`。
5. 归一化阶段并非“所有来源 attack_type=UNKNOWN、severity=P4”：Prometheus 基础设施告警会映射 `CPU_HIGH` 等类型，Manual 来源也允许显式 attack_type。

## 2. 严重问题

### 2.1 真实 Replanner 路径必然二次状态迁移并抛异常（P0）

**白皮书表述**：14.1 第⑦ Step E、2.3、12 部分把 `Replanner.decide` 画成真实主链路，并声称可正常到达 `RESOLVED / FAILED / ESCALATED`。

**源码事实**：

- `app/core/incident_router.py:L327-L342`：先调用 `Replanner.decide()`，再在 `RESOLVE` 分支对 `record` 执行 `EXECUTING → VERIFIED`。
- `app/agents/replanner.py:L79-L111`：`decide()` 内部已经对同一 `record` 执行了 `verification.next_state` 的迁移；`SUCCESS` 时 `next_state=VERIFIED`。
- `app/core/state_machine.py:L69-L76`：`VERIFIED → VERIFIED` 不是合法迁移。
- 因此真实成功路径会在 `_common_pipeline` 的第二个 `transition(record, VERIFIED)` 处抛出 `ValueError`；`COMPENSATE/ESCALATE/FAIL` 分支同样存在二次迁移问题。

**实测复现**：使用真实 `Replanner`、其余 Agent 用 Stub 跑完整 `route()`，在 `verification_finished` 后抛：

```text
ValueError: 非法状态迁移: VERIFIED → VERIFIED
```

**测试盲区**：`tests/test_zl_attack_detector.py:L310-L316` 的 `StubReplanner` 直接返回 `ReplanAction.RESOLVE`，不执行 `state_machine.transition`，所以现有“Router 端到端到 RESOLVED”测试没有覆盖真实 Replanner。

**影响**：白皮书 14.1 的“真实 Request → Response 生命周期”在实际默认配置下不会走到 `RESOLVED`；这是白皮书最重要的核心章节错误，也是源码需要修复的阻断性问题。

### 2.2 恢复成功/安全控制无法改变严格终态（P1）

**白皮书表述**：7.8 称“恢复成功 → 事件转 RESOLVED”，Safety Control “状态转 ESCALATED”。

**源码事实**：

- 进入恢复的 `failure_context` 只在 `final_state in (FAILED, ESCALATED)` 时生成（`app/core/incident_router.py:L466-L488`）。
- `app/core/state_machine.py:L77-L92`：`FAILED` 与 `ESCALATED` 的合法目标集合均为空。
- `app/services/aiops_service.py:L151-L167`：恢复成功后调用 `transition(record, RESOLVED)`，异常被 `except ValueError: pass` 吞掉，事件仍停留在 FAILED/ESCALATED。
- `app/services/aiops_service.py:L399-L412`：Safety Control 尝试 `transition(record, ESCALATED)`，同样被吞掉。

**结论**：当前状态机不支持“失败终态 → 恢复成功 → RESOLVED”，也不支持“FAILED → ESCALATED”。白皮书把这些写成事实，实际是源码注释/意图，不是可执行行为。

### 2.3 `recovery_attempt` 从未递增，恢复次数限制不可达（P1）

**白皮书表述**：7.8/9.4 称 PRP 最多 `MAX_RECOVERY_ATTEMPTS=3`，超限由 replanner 强制返回 `recovery_failed`。

**源码事实**：

- `app/services/aiops_service.py:L257-L266`：`initial_state["recovery_attempt"]=0`。
- `app/agent/aiops/replanner.py:L133-L155`：只读取 `recovery_attempt` 并检查 `>= MAX_RECOVERY_ATTEMPTS`，从未自增。
- `app/agent/aiops/state.py:L37` 注释声称“由 Replanner 递增”，但全仓 grep 无 `recovery_attempt +=` 或等价赋值。
- `app/services/aiops_service.py:L464-L480` 的 `should_continue` 中同样检查 `recovery_attempt >= MAX_RECOVERY_ATTEMPTS`，实际永远为假。

**结论**：当前每次失败只执行一次 `_execute_recovery()`，图中“3 次恢复尝试”的限制不可达；白皮书把未实现的机制描述为已实现行为。

### 2.4 MCP 实际生效配置与白皮书架构图不一致（P1）

**白皮书表述**：2.5、8.1、8.5 将 CLS 描述为 `streamable-http`、`http://localhost:8003/mcp`；8.1 称 `.env` 覆盖发生在 `.env:L16-L22`。

**源码事实**：

- `app/config.py:L47-L50` 默认值确实是 `streamable-http`、`http://localhost:8003/mcp`。
- `.env:L29-L30` 实际覆盖为 `MCP_CLS_TRANSPORT=sse`、`MCP_CLS_URL=http://localhost:3000/sse`；`.env` 的 MCP 段实际在 L26-L33，不是 L16-L22。
- `app/agent/mcp_client.py:L105-L109` 在导入时读取 `config.mcp_servers`，实测 `config.mcp_servers` 输出为：
  ```text
  {'cls': {'transport': 'sse', 'url': 'http://localhost:3000/sse'},
   'monitor': {'transport': 'streamable-http', 'url': 'http://localhost:8004/mcp'}}
  ```
- Makefile `start-cls` 启动的是 8003 的 FastMCP server，但当前应用客户端实际连接 3000 的 SSE 端点，二者并不对应。

**结论**：白皮书应区分“config 默认值”与“当前 `.env` 生效值”，并明确 Makefile 8003 server 与 CLS 3000 SSE 客户端配置不匹配。Monitor 的 8004/streamable-http 描述正确。

## 3. 事实与引用错误

### 3.1 归一化阶段并非全源 UNKNOWN/P4

白皮书 2.3、7.1 称归一化阶段 `attack_type=UNKNOWN`、`severity=P4`。

实际：

- `app/events/event_normalizer.py:L203-L259`：`_normalize_prometheus` 使用 `PROMETHEUS_ALERT_MAP` 映射 `CPU_HIGH`、`MEMORY_HIGH` 等，并通过 `_prometheus_severity` 映射 `critical→P1`、`warning→P3`。
- `app/events/event_normalizer.py:L339-L377`：`_normalize_manual` 允许从输入解析 attack_type。
- `app/events/event_normalizer.py:L543-L549`：`_prometheus_severity` 返回非 P4。

应改为：`normalize_metric()` 路径保持 UNKNOWN/P4；Prometheus 基础设施告警与 Manual 来源存在例外。

### 3.2 `SSEEventType` 是 20 种，不是 19 种

白皮书 10.1 写“19 种”，但 `app/models/incident.py:L89-L118` 包含 `INCIDENT_CREATED...COMPLETE` 共 20 个成员。该枚举区间引用 `L89-L118` 本身正确，数量写错。

### 3.3 `incident.py` 的 Pydantic 模型是 20 个，不是“25+”

白皮书 3.3 写“9 个枚举 + 25+ Pydantic 模型”。实测 `app/models/incident.py`：

- 枚举 9 个：`IncidentSource/AttackType/Severity/IncidentState/ActionStatus/ApprovalStatus/SSEEventType/ApprovalAction/RecoveryState`。
- Pydantic 模型 20 个：从 `IncidentMetadata` 到 `FailureContext`（`app/models/incident.py:L132-L621`）。

“25+”无源码依据。

### 3.4 `with_structured_output` 与 Prompt 数量是 5，不是 4

白皮书 11.3 写“4 处”并同时列出 5 个输出类型（TriageResult/RunbookPlan/Plan/Response/Act），前后自相矛盾。

实测：

- `with_structured_output` 共 5 处：
  `triage_agent.py:L105`、`runbook_agent.py:L91`、`planner.py:L137`、`replanner.py:L204`、`replanner.py:L291`。
- `ChatPromptTemplate.from_messages` 共 5 处：
  `triage_agent.py:L28`、`runbook_agent.py:L36`、`planner.py:L28`、`replanner.py:L41`、`replanner.py:L92`（`response_prompt`）。

### 3.5 附录 B 测试用例分布写错

白皮书附录 B 写 `test_zl_attack_detector.py（11 例）`、`test_stsrs_fusion.py（9 例）`。

`pytest --collect-only` 实测：

- `tests/test_attack_detector.py`: 11
- `tests/test_stsrs_fusion.py`: 10
- `tests/test_zl_attack_detector.py`: 10
- 总计 31，总数字正确；单文件分布写错。

### 3.6 “唯一 Pydantic 请求模型”与“AIOps 响应统一为 SSE”表述过宽

- `RawIncidentRequest`（`app/models/incident.py:L276-L288`）是真实存在的 Pydantic 请求模型，并在 `app/api/aiops.py:L72-L80` 被用来校验统一格式。白皮书 3.3 说“唯一带 Pydantic 校验的请求模型”不准确，应限定为“`app/models/request.py` 仅有 ChatRequest/ClearRequest”。
- `GET /api/aiops/incidents|incidents/{id}|timeline|stats` 返回普通 JSON（`app/api/aiops.py:L292-L394`），所以“AIOps 响应统一为 SSE”只适用于三个 POST 入口，不适用于全部 AIOps 端点。

### 3.7 15.1 “API 层全部无业务逻辑”与 3.3 自相矛盾

- `app/api/file.py:L26-L103` 执行扩展名/大小校验、覆盖旧文件、保存文件。
- `app/api/aiops.py:L71-L95` 执行请求格式解析。
- 白皮书 3.3 自己写明 AIOps 端点“格式解析在端点内手写”，因此 15.1 的“API 层只做 SSE/JSON 包装、全部无业务逻辑”应弱化为“业务编排不放在 API 层”。

### 3.8 16.1-4 对 `approval_timeout_minutes` 的引用容易误读

`app/events/timeout_manager.py:L294` 使用的是 `TimeoutConfig.approval_timeout_minutes`（`app/models/incident.py:L509`），不是 `app/config.py:L71` 的 `Settings.approval_timeout_minutes`。后者确实无引用，但白皮书原文把 `timeout_manager.py:L294` 与“app/config 配置”并列，读者容易误以为该配置项生效。

### 3.9 首次推理耗时

白皮书 5.7.5 实测 `inference_ms=5148.8`；本次审计同机实测首次推理约 `7045ms`。该值依赖机器负载与 sklearn 导入，属于环境相关数字，不构成事实错误，但建议标注为“本机单次实测”而非固定性能指标。

## 4. 已核实无误的重要结论

以下核心结论经源码/实测核对成立：

- 两条子系统路由注册：`app/main.py:L62-L65`。
- AIOps 主链路：`Normalize → Dedup → Severity → AttackDetector → Triage → Runbook → Action → Verify → Replan` 的调用关系与白皮书 2.3/13.1 基本一致（但受 2.1 的真实 Replanner 异常影响）。
- ML 只做推理：`app/ml/attack_detector.py:L1-L17`、`app/ml/zl_attack_detector.py:L1-L6`。
- ZL 模型产物与 manifest：`model_name`、`feature_columns=['Distance','PacketLoss','Latency']`、`target_mapping`、`scaler=None`、`HistGradientBoostingClassifier`、`numpy 2.4.2/sklearn 1.9.0` 均实测一致。
- `ZLFeatureAdapter` 字段映射、缺失抛 `AttackDetectorInputError`、枚举归一化、概率三分支、输出契约校验、`FallbackAttackDetector` 只兜底 LoadError，均与白皮书 5.5-5.9 一致。
- `AttackPrediction` 写入 `Incident.attack_prediction` 并进入 Triage Prompt/CaseKB，调用链成立（`incident_router.py:L164-L184`、`triage_agent.py:L129-L131/L336-L348`）。
- RAG 写入/检索链：upload → index → delete_by_source → splitter → add_documents；`retrieve_knowledge` top_k=3、无 reranker、无 kb_type 过滤，均成立。
- MCP server 工具数：CLS 5、Monitor 2；`mcp_servers/README.md` 声称的工具在源码中不存在。
- 16.1 列出的多数 Confirmed Issue 可复现：`LLMFactory`、`FeatureExtractor`、`trim_messages_middleware`、`should_use_new_link`、`await_approval`、`app/agents/__init__.py` lazy accessor 均无调用者；8 个 AIOps 配置项未被 `Settings` 使用；`static/app.js:L1181` 请求不存在的 `/api/aiops`。
- 16.2 列出的风险项均成立：pickle 安全、首次推理秒级、manifest 旧路径、`Replanner.retry_count` 跨事件累积（`reset_retry_count` 无调用）、`run_in_executor` 无法真正中断、阈值只改 attack_type 不改概率。
- `pytest` 收集 31 项；实测运行退出码 0，并出现 `PydanticDeprecatedSince20` 与 `datetime.utcnow()` 弃用警告，与 11.2/16.1-8 一致。

## 5. 白皮书未覆盖的新发现

除上述 2.1-2.4 外，建议补充：

- 现有 Router E2E 测试全部替换 `replanner` 为 `StubReplanner`，未覆盖真实状态迁移；白皮书附录 B 应注明这一测试盲区。
- `Replanner.decide` 内部调用 `state_machine.transition` 时未传 `trace_id/thread_id`，审计记录 `thread` 为空（`app/agents/replanner.py:L82-L92`）。
- `SeverityEngine` 会把 95（百分数量纲）格式化为 `9500%`，进一步佐证 16.1-6 的量纲问题，但白皮书 5.7.5 的量纲结论仍成立。

## 6. 建议

1. 修复真实 Replanner 的双重迁移：`Replanner.decide` 只做决策与审计，状态迁移统一由 `_common_pipeline` 完成；或删除 `_common_pipeline` 中重复的 `transition`。
2. 为 FAILED/ESCALATED 增加“恢复中/已恢复”的可迁移语义，否则恢复成功与 Safety Control 无法改变终态。
3. 在 PRP 图中真正递增 `recovery_attempt`，或移除不可达的 `MAX_RECOVERY_ATTEMPTS` 分支。
4. 白皮书按“config 默认值”与“当前 `.env` 生效值”两列重写 MCP 配置，并修正 `.env` 行号。
5. 修正归一化章节的表述：`normalize_metric()` 路径才是 UNKNOWN/P4；Prometheus 基础设施告警与 Manual 输入有例外。
6. 修正数量类错误：SSEEventType=20、incident.py Pydantic=20、structured output/Prompt=5、附录 B 单文件用例分布。

## 7. 验证记录

- 模型载荷：`.venv\Scripts\python.exe` 加载 `D:/STUDY/ZL/models/baseline/v2_compact_top3_hist_gradient_boosting.pkl`，输出与白皮书 5.2.1 一致。
- 模型推理：`packet_loss=95.23, latency=354.46, distance=14.75` 输出 `DoS`，`confidence≈0.9999994`，与 5.7.5 一致。
- 测试收集：`pytest tests --collect-only -p no:cacheprovider --no-cov -q` → 31 项。
- 测试运行：`pytest tests --no-cov -p no:cacheprovider -q` 退出码 0；有弃用警告。
- 有效 MCP 配置：`config.mcp_servers` 实测输出见 2.4。
- 真实 Replanner 复现：完整 `route()` 在 `VERIFIED → VERIFIED` 抛 `ValueError`，见 2.1。
