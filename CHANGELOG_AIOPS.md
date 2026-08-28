# CHANGELOG — AIOps Fusion 增量改造

> SuperBizAgent 项目 — 将 Legacy Plan-Execute-Replan 转化为 Event Driven Pipeline 的异常恢复能力

---

## AIOPS-FUSION-001

### Modified Files

- `app/models/incident.py`
- `app/models/__init__.py`

### Change Type

新增

### Reason

需要定义 Workflow 失败上下文（FailureContext）和恢复状态机（RecoveryState），
作为 Event Driven Pipeline 和 Legacy PRP Recovery Engine 之间的数据契约。

### Implementation

1. 新增 `FailureContext` (Pydantic BaseModel):
   - `incident_id`, `thread_id`, `trace_id`: 全链路追踪
   - `failure_reason`, `failure_category`: 失败原因和分类
   - `executed_actions`, `triage_result`, `plan_steps`: 已执行信息
   - `current_state`, `retry_cycles`: 当前状态
   - `prometheus_metrics`, `history`, `extra`: 额外上下文
   - `to_planner_input()`: 格式化为 Legacy Planner 可理解的文本

2. 新增 `RecoveryState` (str Enum):
   - `IDLE → RECOVERY_STARTED → PLANNING → EXECUTING → VERIFYING → REPLANNING`
   - 终态: `RECOVERY_SUCCESS`, `RECOVERY_FAILED`
   - Safety: `SAFETY_ROLLBACK`, `SAFETY_ESCALATION`

3. 新增常量 `MAX_RECOVERY_ATTEMPTS = 3`

### Impact

- `app/models/__init__.py`: 导出新模型
- `app/agent/aiops/state.py`: PlanExecuteState 引用 Recovery 相关字段
- `app/agent/aiops/replanner.py`: 使用 MAX_RECOVERY_ATTEMPTS
- `app/core/incident_router.py`: 生成 FailureContext
- `app/services/aiops_service.py`: 消费 FailureContext

### Compatibility

- `POST /api/aiops`: 兼容（旧链路无变化）
- `POST /api/aiops/incident`: 兼容（FailureContext 仅作为 complete 事件的新增字段，不影响下游）

### Rollback Plan

删除 `FailureContext`, `RecoveryState`, `MAX_RECOVERY_ATTEMPTS` 定义，
恢复 `app/models/__init__.py` 的导出列表即可。

---

## AIOPS-FUSION-002

### Modified Files

- `app/agent/aiops/state.py`

### Change Type

修改

### Reason

Legacy PlanExecuteState 需要支持 Recovery Mode 标记，
使 Planner/Executor/Replanner 能区分"用户主动调用"和"Workflow 失败恢复"两种场景。

### Implementation

1. `PlanExecuteState` 从固定字段改为 `total=False`（所有字段可选）
2. 新增字段:
   - `is_recovery: bool` — 是否为恢复模式
   - `recovery_attempt: int` — 恢复尝试次数
   - `failure_context: Optional[Dict]` — 失败上下文
   - `recovery_result: str` — 恢复最终结果

### Impact

- `app/agent/aiops/planner.py`: 读取 is_recovery 标记
- `app/agent/aiops/replanner.py`: 读取/更新 recovery_attempt, 设置 recovery_result
- `app/services/aiops_service.py`: 构建 recovery 模式的初始状态

### Compatibility

- `POST /api/aiops`: 兼容（正常模式不传新字段，按 False/0 处理）
- `POST /api/aiops/incident`: 兼容（旧链路不感知新字段）

### Rollback Plan

移除 4 个新字段，恢复 `PlanExecuteState` 的固定字段定义。

---

## AIOPS-FUSION-003

### Modified Files

- `app/agent/aiops/planner.py`

### Change Type

修改

### Reason

Planner 需要在 Recovery 模式下接受 FailureContext 格式化的输入（而非用户原始任务描述），
同时对恢复模式增加日志标记。

### Implementation

1. 读取 `state.get("is_recovery", False)` 标记
2. Recovery 模式下打印恢复尝试次数日志
3. 输入日志截断至前 200 字符（避免 FailureContext 过长刷屏）

### Impact

- Planner 行为逻辑不变，仅增强日志
- Recovery 模式下 `input` 由 `FailureContext.to_planner_input()` 生成

### Compatibility

完全向后兼容。正常模式行为不变。

### Rollback Plan

移除 `is_recovery` 检查和 Recovery 日志行。

---

## AIOPS-FUSION-004

### Modified Files

- `app/agent/aiops/replanner.py`

### Change Type

修改

### Reason

Legacy Replanner 需要在 Recovery 模式下:
1. 限制恢复尝试次数（MAX_RECOVERY_ATTEMPTS = 3）
2. 超过限制时设置 `recovery_result = "recovery_failed"`
3. 成功响应时设置 `recovery_result = "recovery_success"`

### Implementation

1. 导入 `MAX_RECOVERY_ATTEMPTS` 常量
2. 在函数开头检查 `is_recovery and recovery_attempt >= MAX_RECOVERY_ATTEMPTS` → 强制终止
3. `respond` 决策路径: 成功响应时追加 `recovery_result = "recovery_success"`
4. 计划为空时的响应路径: 同样追加 `recovery_result = "recovery_success"`
5. 所有路径保留原有正常模式的 continue/replan/respond 逻辑

### Impact

- 与 `app/services/aiops_service.py` 的 `legacy_execute()` 协同
- 前端可读取 `recovery_result` 判断恢复是否成功

### Compatibility

- 正常模式（`is_recovery=False`）完全不受影响
- Recovery 模式新增的 `recovery_result` 字段对正常模式无副作用

### Rollback Plan

移除所有 `is_recovery` 相关代码块，恢复原始 respond 逻辑。

---

## AIOPS-FUSION-005

### Modified Files

- `app/core/incident_router.py`

### Change Type

修改

### Reason

Event Driven Pipeline 的 `_common_pipeline` 需要在 Verifier + Replanner 循环结束后:
1. 检测 Workflow 是否失败（final_state ∈ {FAILED, ESCALATED}）
2. 构建 `FailureContext` 传递给上层 (AIOpsService)
3. 对失败原因进行分类

### Implementation

1. `_common_pipeline` 最终输出增加 `workflow_failed` 和 `failure_context` 字段
2. 新增 `_categorize_failure()` 静态方法，将失败归类为:
   - `retry_exhausted` — 重试耗尽/熔断
   - `timeout` — 动作超时
   - `action_failed` — 全部动作失败
   - `compensation_failed` — 补偿失败
   - `runbook_match_failed` — 无执行结果（Runbook 匹配失败）
   - `unknown` — 未知原因
3. `FailureContext` 由 incident record 中的 triage_result, plan, execution_results, state_history 构建

### Impact

- `app/services/aiops_service.py`: `process_incident()` 读取 `workflow_failed` 和 `failure_context`

### Compatibility

- `POST /api/aiops/incident`: complete 事件新增 `workflow_failed` 和 `failure_context` 字段（向后兼容）
- `POST /api/aiops`: 不经过此链路

### Rollback Plan

恢复 `_common_pipeline` 的最终输出部分（移除 `failure_context` 构建和 `workflow_failed` 字段），
删除 `_categorize_failure` 方法。

---

## AIOPS-FUSION-006

### Modified Files

- `app/services/aiops_service.py`

### Change Type

修改（核心融合）

### Reason

这是 AIOps Fusion 的核心修改。将 Legacy PRP 链路由"用户主动调用"改造为
"Event Driven Workflow 失败自动触发"的 Recovery Engine。

### Implementation

1. **`process_incident()` 改造 — 三相处理**:
   - Phase 1: 正常流转 Event Driven Pipeline (IncidentRouter)
   - Phase 2: 检测 `complete` 事件中的 `workflow_failed=true` → 触发 Legacy PRP
   - Phase 3: Recovery 也失败 → 触发 Safety Control

2. **新增 `legacy_execute(failure_context)` — Recovery Engine**:
   - 接收 `FailureContext` 构建 `PlanExecuteState`（含 `is_recovery=True`）
   - 使用 Legacy PRP Graph 执行：Planner → Executor → Replanner
   - 所有 SSE 事件前缀 `stage = "recovery:*"`

3. **新增 `_execute_safety_control()` — Safety Control Layer**:
   - Step 1: Rollback — 执行回滚动作（解除IP封禁、恢复链路、健康检查）
   - Step 2: Escalation — 更新事件状态为 ESCALATED、写入审计

4. **SSE 事件统一**:
   - Workflow 阶段: NORMALIZE, DEDUP, TRIAGE, RUNBOOK, ACTION, VERIFY
   - Recovery 阶段: recovery_start, recovery_plan, recovery_execute, recovery_replan
   - Safety 阶段: safety_rollback, safety_escalation

5. **状态更新**: Recovery 成功自动转为 RESOLVED; Safety Control 后转为 ESCALATED

### Impact

- `POST /api/aiops/incident`: 内部调度逻辑重构，API 签名不变
- `POST /api/aiops`: 旧链路兼容，无影响

### Compatibility

- `POST /api/aiops`: 完全兼容
- `POST /api/aiops/incident`: API 签名不变，内部行为增强（失败时自动恢复）
- SSE 事件: 新增事件类型 `workflow_failed`, `recovery_*`, `safety_*`（前端可按需处理）

### Rollback Plan

恢复 `aiops_service.py` 到 AIOPS-FUSION 之前的版本（简单的双模调度），
删除 `legacy_execute()`, `_execute_safety_control()` 方法，
`process_incident()` 恢复为直接透传 `router.route()` 事件。

---

## 融合后架构总览

```
                    Incident Event
                          │
                          ▼
                 EventNormalizer
                          │
                          ▼
                   Deduplicator
                          │
                          ▼
                  SeverityEngine
                          │
                          ▼
            IncidentRouter._common_pipeline()
                          │
            ┌─────────────┼─────────────┐
            ▼             ▼             ▼
       TriageAgent   RunbookAgent   ActionOrchestrator
            │             │             │
            └─────────────┼─────────────┘
                          ▼
                      Verifier
                          │
                ┌─────────┴─────────┐
                ▼                   ▼
             SUCCESS             FAILURE
                │                   │
                ▼                   ▼
            RESOLVED        FailureContext
                                │
                                ▼
                       Recovery Mode
                     (Legacy PRP Engine)
                                │
                  ┌─────────────┼─────────────┐
                  ▼             ▼             ▼
            Planner(FailureCtx) Executor   Replanner(+MAX)
                  │             │             │
                  └─────────────┼─────────────┘
                                ▼
                            Verifier
                                │
                      ┌─────────┴─────────┐
                      ▼                   ▼
                   SUCCESS             FAILURE
                      │                   │
                      ▼                   ▼
                  RESOLVED          attempt < 3?
                                        │
                                  ┌─────┴─────┐
                                  ▼           ▼
                                 YES         NO (≥3)
                                  │           │
                                  ▼           ▼
                             Replanner    Safety Control
                             (replan)         │
                                  │     ┌──────┴──────┐
                                  │     ▼             ▼
                                  └──→ Retry     Rollback +
                                                Escalation
```

## SSE 事件类型统一

```
Workflow 阶段:           Recovery 阶段:          Safety 阶段:
  incident_created        recovery_start          safety_rollback
  incident_deduplicated   recovery_plan           safety_rollback_action
  incident_triaged        recovery_execute        safety_escalation
  plan_generated          recovery_replan         safety_escalation_complete
  action_executed         recovery_complete
  verification_finished
  incident_resolved
  incident_failed
  incident_escalated
  complete
```

## 测试方案

### 1. 单元测试

| 测试项 | 覆盖内容 |
|--------|---------|
| `FailureContext.to_planner_input()` | 各字段正确格式化为 Planner 输入 |
| `PlanExecuteState` 扩展字段 | is_recovery, recovery_attempt, recovery_result 默认值 |
| `IncidentRouter._categorize_failure()` | 各失败类别的正确归类 |
| `AIOpsService._build_legacy_graph()` | Graph 节点正确编译 |

### 2. 集成测试

| 测试项 | 预期行为 |
|--------|---------|
| Workflow 成功 → RESOLVED | 不触发 Recovery |
| Workflow 失败 → Recovery 成功 | Recovery Engine 介入 → RESOLVED |
| Workflow 失败 → Recovery 3次失败 | Safety Control 触发 → Rollback + ESCALATED |
| Recovery 1次成功 | 不再重试，直接 RESOLVED |
| Recovery 第2次成功 | 更新 recovery_attempt=1，RESOLVED |

### 3. API 兼容性测试

| 测试项 | 预期 |
|--------|------|
| `POST /api/aiops` | 旧链路正常执行，行为不变 |
| `POST /api/aiops/incident` | 新链路正常，失败时自动恢复 |
| `POST /api/aiops/stsrs` | STSRS 走完整新链路 |
| `GET /api/aiops/incidents` | 列表正常 |
| `GET /api/aiops/incidents/{id}/timeline` | 时间线包含 Recovery 事件 |

## 回滚方案

如需回滚到 AIOPS-FUSION 之前的状态:

1. 恢复 `app/services/aiops_service.py` — 简单双模调度版本
2. 恢复 `app/core/incident_router.py` — 移除 FailureContext 生成
3. 恢复 `app/agent/aiops/replanner.py` — 移除 Recovery 限制
4. 恢复 `app/agent/aiops/planner.py` — 移除 Recovery 日志
5. 恢复 `app/agent/aiops/state.py` — 移除扩展字段
6. 恢复 `app/models/incident.py` — 移除 FailureContext, RecoveryState, MAX_RECOVERY_ATTEMPTS
7. 恢复 `app/models/__init__.py` — 移除新导出
