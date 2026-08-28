# 第四册：Prompt 工程深度面试

> 基于 RailOps Agent 源码 — 7 个核心 Prompt 的设计思想、防护机制与工程实践

---

## Q1：为什么 Planner 的 Prompt 中动态注入 `{experience_context}` 和 `{tools_description}`？

### 标准答案

```python
# app/agent/aiops/planner.py:28-59
planner_prompt = ChatPromptTemplate.from_messages([
    ("system", dedent("""
        作为一个专家级别的规划者，你需要将复杂的任务分解为可执行的步骤。
        可用工具列表（用于制定计划时参考）：
        {tools_description}
        {experience_context}
        对于给定的任务，请创建一个简单的、逐步的计划来完成它。
    """).strip()),
    ("placeholder", "{messages}"),
])
```

**核心原因：** 这是 **动态 Prompt 注入 (Dynamic Prompt Injection)** 模式。Plan 的质量取决于两个因素：
1. **能用什么工具** → `{tools_description}` 告诉 LLM 当前可用的工具能力
2. **历史怎么做的** → `{experience_context}` 从 Milvus 检索相似案例

这两个参数在每次 `planner()` 调用时动态生成（`app/agent/aiops/planner.py:104-119`），而非硬编码在 Prompt 中。

### 追问 #1：`{tools_description}` 是怎么生成的？

**答案：** `app/agent/aiops/planner.py:94-105`
```python
local_tools = list(DEFAULT_LOCAL_AGENT_TOOLS)
mcp_client = await get_mcp_client_with_retry()
mcp_tools = await mcp_client.get_tools()
all_tools = local_tools + mcp_tools
tools_description = format_tools_description(all_tools)
```

`format_tools_description` (`app/agent/aiops/utils.py:8-14`) 将工具列表格式化为 `- tool_name: tool_description` 的文本。这样 LLM 就知道有哪些工具可用，但**不需要知道工具的调用细节**（那些由 Executor 处理）。

### 追问 #2：为什么 `{tools_description}` 是纯文本而非 JSON Schema？

**答案：** Planner 的职责是**理解工具能力并制定计划**，而非实际调用工具。给 Planner JSON Schema 会：
1. 增加 token 消耗
2. 让 Planner "分心"于参数格式而非计划逻辑

实际工具调用由 Executor 的 `bind_tools(all_tools)` 处理，Executor 才知道工具的参数 Schema。

### 追问 #3：`{experience_context}` 为空时 LLM 会怎么处理？

**答案：** `app/agent/aiops/planner.py:108-119`
```python
if experience_docs:
    experience_context = dedent(f"""
        ## 相关经验文档
        {experience_docs}
    """)
else:
    experience_context = ""
```
空字符串意味着 Prompt 中不会有经验部分。LLM 将完全依赖 `tools_description` 和自身知识制定计划。这是一个优雅的降级。

### 追问 #4：`("placeholder", "{messages}")` 的作用是什么？

**答案：** 这是 LangChain `ChatPromptTemplate` 的 `MessagesPlaceholder` 简写。它允许调用者动态传入消息列表，而非在创建 Prompt 时写死。在 Planner 中（`app/agent/aiops/planner.py:131`）：
```python
plan_result = await planner_chain.ainvoke({
    "messages": [("user", input_text)],
    "tools_description": tools_description,
    "experience_context": experience_context
})
```

### 追问 #5：Planner Prompt 为什么用 `dedent()` 包裹？

**答案：** Python 的 `textwrap.dedent()` 移除公共缩进，使多行字符串在代码中缩进美观的同时，实际 Prompt 不会有缩进干扰。这是 Python Prompt Engineering 的最佳实践——LLM 对缩进不敏感，但对不必要的空白会浪费 token。

---

## Q2：Replanner Prompt 的"决策优先级口诀"是设计的核心吗？为什么有效？

### 标准答案

```python
# app/agent/aiops/replanner.py:82-84
**决策优先级口诀：** 
"优先结束 > 保持不变 > 调整计划"
"信息足够就响应，不要追求完美"
```

**核心原因：** 这是针对 LLM 最常见的失败模式设计的**行为引导 (Behavioral Steering)**：
- LLM 倾向于"追求完美"→ 永远觉得信息不够
- LLM 倾向于"过度分析"→ 不断 replan 而非 respond

口诀用**极简的语言**植入优先级，对抗这些倾向。

### 追问 #1：Prompt 中的三选一（continue/replan/respond）优先级是"建议"还是"强制"？

**答案：** 既是建议也是强制：
- **建议层**：Prompt 中的"决策优先级口诀"引导 LLM 主动选择 respond
- **强制层**：代码中的 `MAX_STEPS = 8` (`app/agent/aiops/replanner.py:130-138`) 和 `>= 5 步禁止 replan` (`line 216-218`) 是硬性兜底

这种"软引导 + 硬约束"的双层设计确保即使 LLM 忽略 Prompt 建议，代码也会强制终止。

### 追问 #2：为什么 Replanner 的 System Prompt 用 `dedent("""...""").strip()` 而不用 `f-string`？

**答案：** `strip()` 去掉首尾空白，`dedent()` 去掉缩进。不用 `f-string` 是因为 Replanner Prompt 包含 `{tools_description}` 和 `{messages}` 两个占位符——但这些是 `ChatPromptTemplate` 的占位符，不是 Python f-string。如果用 f-string，这些占位符会被 Python 解析报错。

### 追问 #3：Replanner Prompt 中的 "⚠️" emoji 是否影响 LLM 行为？

**答案：** 在 Replanner Prompt 中（`app/agent/aiops/replanner.py:61-68`）：
```python
- ⚠️ 不要等到"完美"才响应，"足够好"就应该立即 respond
- ⚠️ 如果剩余步骤不是"必需"的，应选择 respond
- ⚠️ 严格限制：...
```

研究表明，LLM 对视觉强调符号（⚠️、**粗体**、## 标题）有一定敏感性。⚠️ 符号在这里的作用是**注意力引导**——让 LLM 在多个约束中优先关注这些 hard constraints。

### 追问 #4：`"新步骤数量必须 <= 当前剩余步骤数"` 这个约束是如何实现的？

**答案：** 两层实现：
1. **Prompt 层**（`app/agent/aiops/replanner.py:71`）：在 System Prompt 中声明规则
2. **代码层**（`app/agent/aiops/replanner.py:208-213`）：
```python
if len(new_steps) > len(plan):
    new_steps = new_steps[:len(plan)]
    # 强制截断
```
代码层是硬性兜底，确保即使 LLM 违反 Prompt 规则，计划也不会膨胀。

### 追问 #5：为什么 Replanner 有独立的 `_generate_response` 函数，而非在同一个 Prompt 中完成？

**答案：** 这是 **职责分离 (Separation of Concerns)** 的设计：
```python
# replanner_prompt: 决策（选 continue/replan/respond）
# response_prompt:  生成响应（写 Markdown 报告）
```
分离的好处：
1. 每个 Prompt 更短（token 消耗少）
2. 决策 Prompt 可以用 `temperature=0`（需要确定性）
3. 响应 Prompt 可以用 `temperature=0.3`（需要一定的创造性描述）
4. 避免 LLM "分心"——做决策时不应同时想怎么写报告

---

## Q3：为什么 RunbookAgent 的 Prompt 强调"严禁硬编码固定动作"？

### 标准答案

```python
# app/agents/runbook_agent.py:45-48
"""
严禁硬编码固定动作！必须基于提供的知识库内容动态生成计划：
- STSRS Replay Attack 不能固定执行某个动作
- DoS 不能固定执行某个动作
- Jamming 不能固定执行某个动作
"""
```

**核心原因：** LLM 有一种强烈的"模式匹配"倾向——看到 `DoS` 就想执行 `block_suspicious_source`，看到 `Replay Attack` 就想执行 `restart_gateway`。如果不加约束，LLM 会走捷径，绕过了知识库检索的意义。

### 追问 #1：为什么这个约束不放在代码层面（如后处理校验）？

**答案：** 实际上代码层也做了约束——`RunbookAgent._fallback_plan` (`app/agents/runbook_agent.py:275-324`) 虽然包含硬编码的 fallback 计划，但**仅在 LLM 调用失败时**才使用。正常运行路径下，LLM 生成的计划基于 KB 检索结果。Prompt 层的"严禁硬编码"是**预防性约束**——防止 LLM 从一开始就走捷径。

### 追问 #2：如果 LLM 就是不听这个约束怎么办？

**答案：** 当前没有后处理校验。这是一个已知的设计风险——LLM 完全可能忽略 Prompt 中的"严禁"指令。优化方案：
1. 后处理：检查生成的 `RunbookPlan.steps` 是否与 KB 检索内容相关（通过向量相似度）
2. 验证：如果 `source_kb` 为空或 `confidence` 过低，标记为需人工审核
3. Guardrails：使用 NeMo Guardrails 或类似框架做输出校验

### 追问 #3：RunbookAgent 允许的 6 个 Mock 动作是硬编码在 Prompt 中的，这和"严禁硬编码固定动作"矛盾吗？

**答案：** 不矛盾。"严禁硬编码固定动作"指的是**不允许固定 attack_type→action 的映射**（如 `DoS → block_suspicious_source`），而不是不允许列出可用动作列表。列出 6 个动作是告诉 LLM **可选范围**，而非告诉它**必须选哪个**。

---

## Q4：`response_format="content_and_artifact"` 在 retrieve_knowledge 工具中的作用是什么？

### 标准答案

```python
# app/tools/knowledge_tool.py:14
@tool(response_format="content_and_artifact")
def retrieve_knowledge(query: str) -> Tuple[str, List[Document]]:
    # ...
    return context, docs
```

**核心原因：** LangChain 的 `@tool` 装饰器中 `response_format="content_and_artifact"` 告诉框架这个工具返回**两个值**：
1. `content`（`str`）→ 给 LLM 看的格式化文本
2. `artifact`（`List[Document]`）→ 给代码层用的原始数据

### 追问 #1：为什么需要返回两个值？

**答案：** `app/agent/aiops/planner.py:82-83`
```python
context_str = await retrieve_knowledge.ainvoke({"query": input_text})
```
当使用 `.ainvoke()` 而非在 Tool Calling 场景下调用时，`content_and_artifact` 工具只返回 `content`（字符串）。这意味着 Planner 可以直接拿到格式化文本作为经验上下文，而不需要自己格式化——这是典型的"同一工具、不同调用方式、不同返回值"的设计。

### 追问 #2：`format_docs()` 函数的输出格式为什么包含"【参考资料 N】"这样的标记？

**答案：** `app/tools/knowledge_tool.py:77-78`
```python
formatted = f"【参考资料 {i}】"
if header_str:
    formatted += f"\n标题: {header_str}"
formatted += f"\n来源: {source}"
formatted += f"\n内容:\n{doc.page_content}\n"
```

这是一种 **Prompt-friendly 格式**。LLM 更容易理解带编号和标签的结构化文本。`【参考资料 N】` 作为视觉标记帮助 LLM 区分不同来源。

### 追问 #3：为什么 `ainvoke()` 对 `content_and_artifact` 工具只返回 content？

**答案：** 这是 LangChain 的约定——`ainvoke()` 返回"对 LLM 友好的结果"。如果需要 artifact，需要使用 `ainvoke(return_only_outputs=False)` 或 `.abatch()`。在当前项目中，Planner 只需要文本上下文就够了，不需要原始 `Document` 对象。

---

## Q5：每个 Agent 的 Prompt 为什么都用 `ChatPromptTemplate.from_messages()` 而非字符串拼接？

### 标准答案

所有 Agent Prompt 统一使用这个模式：
```python
# 例如 app/agents/triage_agent.py:25-45
TRIAGE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", dedent("""...""")),
    ("placeholder", "{messages}"),
])
```

**核心原因：**
1. **结构清晰**：System Message 和 User Message 分离，语义明确
2. **类型安全**：`from_messages` 接受 tuple 列表，每个 tuple 是 `(role, content)`
3. **动态注入**：`MessagesPlaceholder` (`"placeholder"`) 允许调用者动态插入消息
4. **框架集成**：与 `with_structured_output()` 链式调用兼容

### 追问 #1：为什么 TriageAgent 和 RunbookAgent 用 `temperature=0`？

**答案：** `app/agents/triage_agent.py:58-62` 和 `app/agents/runbook_agent.py:87-91`
```python
self.llm = ChatQwen(
    model=config.rag_model,
    api_key=config.dashscope_api_key,
    temperature=0,    # ← 确定性输出
)
```

AIOps 场景需要**确定性**和**可复现性**。`temperature=0` 意味着相同的输入产生相同的输出（尽可能）。这对于审计合规非常重要。

### 追问 #2：但 RAG 对话 Agent 用的是 `temperature=0.7`，为什么？

**答案：** `app/services/rag_agent_service.py:99`
```python
self.model = ChatQwen(
    model=self.model_name,
    temperature=0.7,   # ← 中等创造性
    streaming=True,
)
```

RAG 对话是面向终端用户的，需要一定的创造性使回答自然、不机械。而 AIOps 诊断需要"照章办事"。

---

**Prompt 工程深度面试 — 本章结束**

关键文件索引：
- `app/agent/aiops/planner.py:28-59` — Planner Prompt（动态工具+经验注入）
- `app/agent/aiops/replanner.py:41-108` — Replanner Prompt + Response Prompt
- `app/agents/triage_agent.py:25-45` — TriageAgent Prompt（铁路领域特定）
- `app/agents/runbook_agent.py:37-73` — RunbookAgent Prompt（禁止硬编码）
- `app/tools/knowledge_tool.py:14-44` — content_and_artifact 格式
- `app/services/rag_agent_service.py:159-187` — RAG System Prompt
