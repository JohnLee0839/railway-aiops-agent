# 第一册：LangGraph 深度面试

> 基于 RailOps Agent 源码 (SuperBizAgent v2.0.0) 的真实设计决策
> 所有问题均来自源码，所有答案均引用源码

---

## Q1：为什么 RailOps 使用 LangGraph 而非直接调用 LLM？

### 标准答案

在 `app/services/rag_agent_service.py:146` 和 `app/services/aiops_service.py:135-157` 中：

```python
# RAG Agent 使用 create_agent (LangGraph 高层封装)
self.agent = create_agent(self.model, tools=all_tools, checkpointer=self.checkpointer)

# 旧链路使用显式 StateGraph
workflow = StateGraph(PlanExecuteState)
workflow.add_node("planner", planner)
workflow.add_node("executor", executor)
workflow.add_node("replanner", replanner)
workflow.add_conditional_edges("replanner", should_continue, ...)
```

**核心原因：**
1. **状态管理**：直接调用 LLM 需要自己管理对话历史和中间状态。LangGraph 的 `StateGraph` + `MemorySaver` (`app/services/rag_agent_service.py:110`) 原生支持 checkpoint 持久化。
2. **工具调用的控制流**：AIOps 场景下 Planner→Executor→Replanner 形成循环，LangGraph 的 `conditional_edges` (`app/services/aiops_service.py:151-154`) 提供了声明式的流控。
3. **流式输出**：LangGraph 的 `astream(stream_mode="messages")` 支持 token 级流式输出，这是 AIOps 诊断报告实时推送给前端的基础。

### 追问 #1：为什么不用 AutoGen？

**答案：** AutoGen 的核心模型是"对话驱动"——Agent 之间通过消息传递协作。但 RailOps 的 AIOps 场景是"流程驱动"的：
- `IncidentRouter → TriageAgent → RunbookAgent → ActionOrchestrator → Verifier → Replanner` 形成严格的**流水线**，每个 Agent 之间通过 `Pydantic Schema`（`TriageResult → RunbookPlan → MockActionResult → VerificationResult`）传递结构化数据，而非自然语言对话。
- AutoGen 的对话模型会引入额外的 token 消耗和不确定性，不适合需要**确定性状态迁移**的运维场景。

### 追问 #2：为什么不用 CrewAI？

**答案：** CrewAI 的"角色扮演"模型（给每个 Agent 设置 role/goal/backstory）更偏"创意协作"，不适用于 AIOps 的场景：
- `app/agents/triage_agent.py` 中的 TriageAgent 通过严格的 `with_structured_output(TriageResult)` 输出 Pydantic Schema，而非自由文本。
- CrewAI 对工具调用的控制较弱，而 RailOps 的 `ActionOrchestrator` 需要 ApprovalGate + TimeoutManager + 熔断器的精细化控制。

### 追问 #3：如果不用 LangGraph，自己实现会怎样？

**答案：** 需要手动实现：
1. **Checkpoint 机制**（类比 `MemorySaver`）—— 跨请求的状态持久化
2. **条件路由**（类比 `conditional_edges`）—— Replanner 的三态/五态决策
3. **流式管道**（类比 `astream`）—— 从 LLM 输出到 SSE 的管道
4. **工具绑定**（类比 `bind_tools` + `ToolNode`）—— LLM ↔ Tool 的调用循环

估算代码量至少 500-800 行，且需要处理大量边界情况（重试、超时、状态不一致）。

### 追问 #4：LangGraph 最大的缺点是什么？

**答案：** 在当前项目中体现出的缺点：
1. **调试黑盒**：`create_agent()` (`app/services/rag_agent_service.py:146`) 内部封装太深，Agent 行为不符合预期时难以定位根因。
2. **版本变动频繁**：LangGraph API 尚不稳定（如 `get_state()` 的返回值类型在 0.1.x 到 0.2.x 之间发生变化）。
3. **并发模型限制**：`StateGraph` 的节点是串行执行的，`executor` 每次只执行 `plan[0]` (`app/agent/aiops/executor.py:34`)，无法并行执行多个步骤。

### 追问 #5：如果 1000 个 Workflow 并发怎么办？

**答案：** 当前架构的瓶颈：
1. `MemorySaver` (`app/services/aiops_service.py:52`) 是进程内存实现，1000 并发下内存爆炸。
2. LangGraph 的 `checkpointer` 可替换为 `SqliteSaver` 或 `PostgresSaver`（LangGraph 原生支持）。
3. 需要引入 `RedisSaver`（社区方案）或自己实现分布式 checkpointer。
4. 每个 workflow 可映射到独立的 `thread_id`，利用 LangGraph 的 `config["configurable"]["thread_id"]` 实现状态隔离。

---

## Q2：为什么旧链路使用 StateGraph 而新链路不使用？

### 标准答案

```python
# 旧链路 — 使用 LangGraph StateGraph
# app/services/aiops_service.py:131-157
workflow = StateGraph(PlanExecuteState)
workflow.add_node("planner", planner)
workflow.add_node("executor", executor)
workflow.add_node("replanner", replanner)
workflow.set_entry_point("planner")
workflow.add_edge("planner", "executor")
workflow.add_edge("executor", "replanner")
workflow.add_conditional_edges("replanner", should_continue, ...)

# 新链路 — 不使用 StateGraph，手动编排
# app/services/aiops_service.py:88-89
async for event in self.router.route(raw_event, source, thread_id):
    yield event
```

**核心原因：**
1. **架构演进**：旧链路是 MVP 阶段的产物，验证 Plan-Execute-Replan 模式。新链路是事件驱动的升级版，需要更灵活的控制。
2. **控制粒度不同**：新链路需要 ApprovalGate、TimeoutManager、熔断器、补偿机制这些 LangGraph 不原生支持的细粒度控制。
3. **Agent 模型不同**：旧链路 3 个节点共享一个 `PlanExecuteState`，新链路通过 `IncidentStore` + `AuditStore` + `StateMachine` 实现更复杂的状态管理。

### 追问 #1：LangGraph 的 StateGraph 本质是什么？

**答案：** 本质是一个有向图 + 带 reducer 的状态管理。核心抽象：
```python
# app/agent/aiops/state.py:10-24
class PlanExecuteState(TypedDict):
    input: str
    plan: List[str]
    past_steps: Annotated[List[tuple], operator.add]  # ← reducer: 追加而非覆盖
    response: str
```
`Annotated[List[tuple], operator.add]` 是 LangGraph 的核心创新——每次节点返回的 `past_steps` 不是覆盖旧值，而是**追加**。这使得多个步骤的执行结果可以累积。

### 追问 #2：`operator.add` 的作用是什么？为什么不用普通 list？

**答案：** `app/agent/aiops/state.py:21`
```python
past_steps: Annotated[List[tuple], operator.add]
```
LangGraph 在合并状态时，对每个字段调用其 `Annotated` 中指定的 reducer 函数。`operator.add` 对 list 来说等价于 `list.extend()`。这意味着 executor 返回 `{"past_steps": [("step1", "result1")]}` 时，LangGraph 自动将新 steps 追加到已有的 `past_steps` 列表中，而非替换。注释也明确写了：`# 两个结果不覆盖`。

### 追问 #3：如果用新链路的 IncidentRouter 模式替代所有 StateGraph 使用场景，可行吗？

**答案：** 可以，但需要权衡：
- ✅ 更灵活的控制流（补偿、审批、熔断）
- ✅ 更好的可观测性（每个步骤都有审计记录）
- ❌ 失去了 LangGraph 的 checkpoint 自动持久化
- ❌ 失去了 LangGraph 的 time-travel debugging
- ❌ 需要手动管理状态一致性和错误恢复

### 追问 #4：当前项目中 `create_agent()` 和 `StateGraph` 各用于什么场景？

**答案：**
- `create_agent()` → RAG 对话 Agent (`app/services/rag_agent_service.py:146`)：需要灵活的 Tool Calling + 多轮对话 + checkpoint
- `StateGraph` → 旧链路 AIOps (`app/services/aiops_service.py:135`)：需要固定的 3 步流程 + 条件循环
- 手动编排 → 新链路 AIOps (`app/core/incident_router.py:212`)：需要精细化控制 + 补偿 + 审批

### 追问 #5：LangGraph 的 checkpoint 在项目中存储在哪里？

**答案：** `app/services/rag_agent_service.py:110` 和 `app/services/aiops_service.py:52`
```python
self.checkpointer = MemorySaver()
```
当前全部使用 `MemorySaver`（进程内存），这意味着：
- 服务重启 → 所有对话历史丢失
- 不能跨进程共享
- 但在 `rag_agent_service.py` 中，`clear_session()` (`line 389-408`) 和 `get_session_history()` (`line 324-387`) 提供了手动管理能力

---

## Q3：Planner 中的经验检索（RAG）为什么要放在 Plan 生成之前？

### 标准答案

```python
# app/agent/aiops/planner.py:77-90
# 步骤1: 查询内部文档获取相关经验
experience_docs = ""
context_str = await retrieve_knowledge.ainvoke({"query": input_text})
if context_str and context_str.strip():
    experience_docs = context_str

# 步骤2-4: 将经验文档注入 Prompt ... 然后生成计划
```

**核心原因：**
这是典型的 **In-Context Retrieval Augmented Planning** 模式：
1. 先用 RAG 从 Milvus 检索历史经验文档
2. 然后将检索结果作为 `{experience_context}` 注入到 `planner_prompt` 中 (`app/agent/aiops/planner.py:109-119`)
3. LLM 基于"当前工具集 + 历史经验"制定计划

这比"先用 LLM 生成计划再检索"更有效，因为：LLM 在制定计划时就能"看到"历史经验，避免了"LLM 制定了一个不合理计划，然后被外部知识纠正"的浪费。

### 追问 #1：为什么不在 Prompt 中直接让 LLM 调用检索工具？

**答案：** 因为 Planner 的职责是**制定计划**，不是**执行计划**。如果让 Planner 调用工具，就会模糊 Planner 和 Executor 的边界。当前设计中：
- Planner → 只有 RAG 检索（辅助计划制定）
- Executor → 所有执行性工具调用（MCP + 本地工具）

这种"部分能力赋予"是精心设计的——Planner 可以"参考"但不"执行"。

### 追问 #2：如果 Milvus 检索失败，Plan 会怎么样？

**答案：** `app/agent/aiops/planner.py:89-90` 和 `line 118-119`
```python
except Exception as e:
    logger.warning(f"查询内部文档失败: {e}")
# ...
if experience_docs:
    experience_context = "..."  # 有经验
else:
    experience_context = ""      # 无经验，空字符串
```
LLM 仍会基于通用知识生成计划——这是一个优雅的降级。`experience_context` 为空时，Prompt 中不会出现经验部分，但 LLM 依然可以基于工具列表推理出基本步骤。

### 追问 #3：`with_structured_output(Plan)` 的作用是什么？

**答案：** `app/agent/aiops/planner.py:128`
```python
planner_chain = planner_prompt | llm.with_structured_output(Plan)
```
`with_structured_output(Plan)` 强制 LLM 输出符合 `Plan` Pydantic Schema 的结构化数据（而不是自由文本）。这是 LangChain 实现 Tool Calling / Function Calling 的方式。对于 DashScope Qwen 模型，底层会转换为 JSON Mode 或 Function Calling API 调用。

### 追问 #4：如果 LLM 返回的 JSON 不符合 Plan Schema 怎么办？

**答案：** `app/agent/aiops/planner.py:138-142`
```python
if isinstance(plan_result, Plan):
    plan_steps = plan_result.steps
else:
    plan_steps = plan_result.get("steps", [])
```
这里有一个安全的退化路径：先检查是否为 `Plan` 实例，如果不是则尝试当作 `dict` 提取 `steps`，如果还是没有则返回空列表。最外层还有 try-except（`line 150-159`）返回默认 3 步计划作为最后的回退。

### 追问 #5：这个默认 Plan 有什么问题？

**答案：** `app/agent/aiops/planner.py:152-158`
```python
return {
    "plan": [
        "收集相关信息",
        "分析数据",
        "生成报告"
    ]
}
```
这个默认计划非常**通用且模糊**。Executor 执行时可能因为缺乏具体指令而无所适从。但作为最终回退，它至少保证了**系统不会崩溃**——这是 Robustness 优先于 Usefulness 的设计权衡。

---

## Q4：Executor 为什么每次只执行一个步骤（`plan[0]`）？

### 标准答案

```python
# app/agent/aiops/executor.py:34
task = plan[0]  # 取出第一个步骤

# line 103-104
return {
    "plan": plan[1:],  # 移除第一个步骤
    "past_steps": [(task, result)],  # 追加结果
}
```

**核心原因：**
这是 **Plan-Execute-Replan** 模式的核心：每执行完一个步骤，就回到 Replanner 重新评估是否需要调整计划。如果一次性执行所有步骤，就无法中途根据中间结果调整方向。

### 追问 #1：为什么不用并发执行（Parallel Execution）？

**答案：** 在 AIOps 场景下，诊断步骤之间有强依赖关系：
- 步骤1 查询 CPU → 发现异常 → 步骤2 针对特定进程查日志
- 如果并发执行所有步骤，可能浪费资源在无关的检查上

### 追问 #2：如果 plan 中有 100 个步骤怎么办？

**答案：** Replanner 有硬性限制：
```python
# app/agent/aiops/replanner.py:130-138
MAX_STEPS = 8
if len(past_steps) >= MAX_STEPS:
    # 强制生成最终响应
    return await _generate_response(state, llm)
```
即使 Replanner 不触发终止，`MAX_STEPS = 8` 作为硬性兜底，防止无限循环。

### 追问 #3：Executor 的 SystemMessage 为什么特别强调"专注于当前步骤"？

**答案：** `app/agent/aiops/executor.py:62-75`
```python
SystemMessage(content="""你是一个能力强大的助手，负责执行具体的任务步骤。
...
注意：
- 专注于当前步骤，不要考虑其他任务
""")
```
这是为了防止 LLM 的"过度规划"倾向——LLM 在执行步骤3时可能"担心"步骤4和5，导致输出中包含对后续步骤的建议。但在 Plan-Execute-Replan 模式下，后续步骤由 Replanner 决定，Executor 应只关注当前。

### 追问 #4：ToolNode 如何处理 LLM 的多轮工具调用？

**答案：** `app/agent/aiops/executor.py:83-93`
```python
if hasattr(llm_response, "tool_calls") and llm_response.tool_calls:
    tool_messages = await tool_node.ainvoke({"messages": messages})
    messages.extend(tool_messages["messages"])
    final_response = await llm_with_tools.ainvoke(messages)
```
这是一个简化的两步循环：LLM 调用工具 → ToolNode 执行 → LLM 总结结果。对于单轮工具调用足够，但如果 LLM 需要连续调多个工具（如先用 `get_current_time` 再用 `search_log`），当前实现只支持一轮。

### 追问 #5：如果要支持多轮工具调用，怎么改？

**答案：** 将 Executor 改为 while 循环：
```python
while True:
    response = await llm_with_tools.ainvoke(messages)
    if not response.tool_calls:
        break
    tool_results = await tool_node.ainvoke({"messages": messages + [response]})
    messages.extend(tool_results["messages"])
```
但需要加上 `max_iterations` 防止无限循环——这正是 LangGraph 的 `ToolExecutor` 内置的功能。

---

## Q5：为什么 Replanner 设置 `MAX_STEPS = 8` 和 `>= 5 步禁止 replan`？

### 标准答案

```python
# app/agent/aiops/replanner.py:130-138
MAX_STEPS = 8
if len(past_steps) >= MAX_STEPS:
    # 强制生成最终响应

# line 216-218
if len(past_steps) >= 5:
    # 禁止重新规划，强制生成响应
```

**核心原因：**
这是**防止 LLM 无限循环**的多层防护：
1. 第 1 层：Prompt 中的"决策优先级口诀"引导 LLM 主动选择 respond
2. 第 2 层：`>= 5 步` 时硬性禁止 replan（代码层防护）
3. 第 3 层：`>= 8 步` 时强制终止（兜底防护）

这是一个典型的 Defense in Depth 设计。

### 追问 #1：为什么是 5 和 8 这两个数字？

**答案：** 这是**经验值**，没有理论最优解。选择逻辑：
- 5 步：通常 3-4 步是标准诊断流程，5 步已是"深入调查"
- 8 步：对应旧链路默认 plan 的 3-6 个步骤 + 2 次 replan 的空间

### 追问 #2：Replanner 的"三态决策"是否足够？

**答案：** `app/agent/aiops/replanner.py:27-31`
```python
class Act(BaseModel):
    action: str  # 'continue' | 'replan' | 'respond'
    new_steps: List[str]  # replan 时的新步骤
```
对于旧链路的 Plan-Execute-Replan 模式，三态足够了。但新链路的 `Replanner`(`app/agents/replanner.py:31-37`) 扩展到了五态：`RESOLVE / RETRY / COMPENSATE / ESCALATE / FAIL`，因为事件驱动场景需要补偿和升级。

### 追问 #3：如果 LLM 执意选择 replan 导致步骤数爆炸怎么办？

**答案：** `app/agent/aiops/replanner.py:208-213`
```python
if len(new_steps) > len(plan):
    new_steps = new_steps[:len(plan)]  # 强制截断
```
还有一个隐含约束：`Prompt 中的"新步骤数量必须 <= 当前剩余步骤数"`，这意味着 replan 不会导致步骤膨胀。

### 追问 #4：`_generate_response` 为什么是独立函数而非 Replanner 的一部分？

**答案：** `app/agent/aiops/replanner.py:242-291`
因为"决策"和"响应生成"是两个不同的 LLM 调用，使用不同的 Prompt：
- Replanner 决策 → `replanner_prompt` + `Act` schema
- 响应生成 → `response_prompt` + `Response` schema

分离的好处：
1. 每个 LLM 调用的 Prompt 更短（减少 token 消耗）
2. 避免 LLM 在决策时"分心"生成响应内容
3. 可以独立优化两个 Prompt

### 追问 #5：如果最终响应生成也失败怎么办？

**答案：** `app/agent/aiops/replanner.py:278-291`
```python
except Exception as e:
    fallback_response = f"""# 任务执行结果
## 原始任务
{input_text}
## 执行的步骤
{_format_simple_steps(past_steps)}
## 说明
由于系统异常，无法生成完整响应。以上是已收集的信息。
"""
```
这就是最后一个兜底——即使 LLM 调用失败，用户也能看到已执行步骤的摘要，而非空白页面。

---

## Q6：`PlanExecuteState` 中 `past_steps` 为什么用 `Annotated[List[tuple], operator.add]`？

### 标准答案

```python
# app/agent/aiops/state.py:21
past_steps: Annotated[List[tuple], operator.add]
```

这是 LangGraph 的**Reducer 机制**。LangGraph 在更新 State 时，每个字段可以通过 `Annotated` 指定一个 reducer 函数。`operator.add` 对 list 而言是 `extend`，因此每次 Executor 返回 `{"past_steps": [("step1", "result1")]}` 时，新值会追加到已有列表末尾，而非覆盖。

### 追问 #1：为什么不用 `list.append` 而是 `operator.add`？

**答案：** `operator.add(a, b)` 对 list 返回 `a + b`（新列表）。而 `list.append` 是原地修改，返回 `None`，不符合 LangGraph 的函数式 reducer 要求。

### 追问 #2：除了 `operator.add`，LangGraph 还支持哪些 Reducer？

**答案：** LangGraph 内置：
- `add_messages`（消息列表合并，用于对话历史）
- 自定义 reducer 函数
- 在 `rag_agent_service.py:38` 中：
```python
messages: Annotated[Sequence[BaseMessage], add_messages]
```

### 追问 #3：如果 Executor 不小心返回了重复的 `past_steps` 怎么办？

**答案：** 当前实现没有去重。因为 Executor 每次只执行一个步骤并移除它（`plan[1:]`），正常流程不会产生重复。但如果在 Replanner 中出现了 `continue`（不修改 state），下一次 Executor 执行相同的 `plan[0]` 会产生相似的结果——这也是为什么 Replanner 的 `respond` 优先级高于 `continue`。

### 追问 #4：`past_steps` 的类型是 `List[tuple]` 而非 `List[Tuple[str, str]]`，有什么原因？

**答案：** Python `TypedDict` 中，`tuple` 泛型参数在运行时不被检查。使用 `List[tuple]` 而非 `List[Tuple[str, str]]` 是简化——实际上 `past_steps` 中每个元素是 `(step_description: str, result: str)`。更严格的写法应该是 `List[Tuple[str, str]]`。

---

**LangGraph 深度面试 — 本章结束**

关键文件索引：
- `app/agent/aiops/state.py` — PlanExecuteState 定义
- `app/agent/aiops/planner.py` — Planner 节点（含经验检索）
- `app/agent/aiops/executor.py` — Executor 节点
- `app/agent/aiops/replanner.py` — Replanner 节点（三态决策+多层防护）
- `app/services/rag_agent_service.py` — LangGraph create_agent 使用
- `app/services/aiops_service.py` — StateGraph 构建 + 旧链路编排
