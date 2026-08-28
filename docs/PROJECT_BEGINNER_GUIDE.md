# railways_V.2 项目新手解释体系

> 适用读者：刚学完 Python、有基础 AI 知识，但第一次接触本项目的开发者。
> 事实依据：当前仓库源码（`app/`、`mcp_servers/`）与 `docs/PROJECT_SOURCE_CODE_WALKTHROUGH.md`。
> 用法：先看第 1 节速查表，再按需阅读第 2 节术语详解；第 3-5 节用于解决最容易混淆的概念。

## 1. 术语速查表（一句话）

| 术语 | 一句话理解 | 在本项目中的角色 |
| --- | --- | --- |
| ML | 用历史数据训练出的“分类判断程序” | ZL 模型判断当前指标属于 Normal / DoS / Jamming / Replay Attack |
| LLM | 能生成自然语言的大模型 | qwen-max 负责诊断解释、处置计划等文本推理 |
| Embedding | 把文本变成一串数字向量 | DashScopeEmbeddings 把知识文档和查询转成 1024 维向量 |
| Vector Store / Vector Database | 专门按向量相似度搜索的数据库 | Milvus 存储知识文档向量，collection 名为 `biz` |
| RAG | 先检索资料、再把资料交给 LLM 的问答方式 | `retrieve_knowledge` 工具从 Milvus 检索 SOP 文档给 Agent |
| Agent | 能自主决定调用什么工具的“智能体” | 聊天助手 Agent（LangGraph `create_agent`），以及 Triage/Runbook 等 LLM 节点 |
| Workflow | 按固定顺序执行步骤的流程 | AIOps 主流水线；失败后进入 PRP 恢复图 |
| Node | Workflow / 图中的一个执行步骤 | PRP 恢复图的 planner / executor / replanner 节点 |
| State | 流程运行中携带的数据 | LangGraph `PlanExecuteState`；事件侧 `IncidentState` / StateMachine |
| Tool | 一个可被调用的功能函数 | 知识检索、Prometheus 查询、时间、Mock 动作 |
| MCP | 跨进程调用外部工具的标准协议 | 连接 CLS 日志服务、Monitor 监控服务 |
| Orchestrator | 负责“安排谁先做、下一步做什么”的角色 | ActionOrchestrator 执行动作；AIOpsService 编排整个事件处置 |
| Service | 面向业务能力封装的类 | AIOpsService、RagAgentService、向量存储/嵌入服务 |
| Retriever | 从向量库中查找相关文档的对象 | Milvus `as_retriever(k=3)` |
| Pipeline | 数据按固定阶段流动的链条 | 归一化 → 去重 → 分级 → ML 检测 → 诊断 → 计划 → 执行 → 验证 → 重规划 |
| Feature Engineering | 把原始数据整理成模型能用的特征 | `ZLFeatureAdapter` 把指标转成 Distance / PacketLoss / Latency |
| Inference | 用模型对输入做预测 | `predict_proba()` 输出每个攻击类别的概率 |
| Context | 交给 LLM 的“背景资料” | 指标快照、知识库文档、事件信息 |
| Prompt | 给 LLM 的指令与输入 | Triage / Runbook / PRP 的 ChatPromptTemplate |
| Model | 泛指“训练好的参数化程序” | 本项目中既指 ZL ML 模型，也指 qwen-max LLM |
| Provider | 提供模型 / 服务的供应商 | DashScope（LLM + Embedding）、Milvus、Prometheus、MCP Server |

## 2. 术语详解

### 2.1 ML（机器学习）

**一句话理解：** 让程序从历史数据中“学”出判断规则，而不是把每条规则都手写出来。

**专业定义：** 机器学习是通过数据拟合参数化模型，使模型能够对新输入做出预测。当前项目使用的是监督学习分类器。

**在本项目中的作用：** ZL V2 是一个 `HistGradientBoostingClassifier`，从 `D:\STUDY\ZL\models\baseline\v2_compact_top3_hist_gradient_boosting.pkl` 加载；输入 3 个特征 `Distance`、`PacketLoss`、`Latency`，输出 4 类概率：Normal、DoS、Jamming、ReplayAttack，再映射为 UNKNOWN、DoS、Jamming、Replay Attack。预测结果写入 `AttackPrediction`。

**它不是什么：** 它不做文本理解，不做对话，不做知识检索；它只负责“当前指标更像哪类攻击”。

**与其他模块的关系：** 它是 `AttackDetector` 的 `zl` 后端；`IncidentRouter` 调用 `create_attack_detector()` 创建检测器；模型加载失败时由 `FallbackAttackDetector` 回退到 rule 检测器。

**源码入口：** `app/ml/zl_attack_detector.py`、`app/ml/attack_detector.py`、`app/config.py`（`ml_attack_detector_backend="zl"`）。

### 2.2 LLM（大语言模型）

**一句话理解：** 一种能读懂文字并生成自然语言的模型。

**专业定义：** LLM 是基于海量文本训练的生成式模型，根据输入文本（Prompt）生成后续文本或结构化输出。

**在本项目中的作用：** 当前使用 `ChatQwen`（`qwen-max`，阿里云 DashScope）。它参与：RAG 对话助手、TriageAgent 诊断、RunbookAgent 生成处置计划、PRP 恢复图的 planner / executor / replanner。

**它不是什么：** 它不等于 Agent。LLM 只是“大脑”，Agent 还包含工具调用、状态管理、流程控制。

**与其他模块的关系：** 它是 Agent 的推理内核；RAG 通过 `retrieve_knowledge` 给 LLM 提供参考资料；ML 的 `AttackPrediction` 被写入 Prompt，作为 LLM 诊断的依据。

**源码入口：** `app/services/rag_agent_service.py`、`app/agents/triage_agent.py`、`app/agents/runbook_agent.py`、`app/agent/aiops/planner.py`、`app/agent/aiops/replanner.py`。

### 2.3 Embedding（嵌入）

**一句话理解：** 把一段文字变成一串固定长度的数字，让相似的文字在数字上更接近。

**专业定义：** Embedding 模型把文本映射到高维向量空间，语义相近的文本向量距离更近。

**在本项目中的作用：** `DashScopeEmbeddings`（`text-embedding-v4`，1024 维）为知识文档和查询生成向量；写入时随文档一起存入 Milvus，查询时把用户问题转成向量后做相似度检索。

**它不是什么：** 它不是 LLM，不能生成回答；它只是“翻译成向量”的服务。

**与其他模块的关系：** 它是 RAG 的“编码器”；`VectorStoreManager` 调用它完成文档向量化。

**源码入口：** `app/services/vector_embedding_service.py`。

### 2.4 Vector Store / Vector Database（向量库）

**一句话理解：** 专门存“向量 + 文本”，并按向量相似度快速找到最相关记录的数据库。

**专业定义：** 向量数据库对高维向量建立索引，支持 ANN（近似最近邻）检索。

**在本项目中的作用：** Milvus 是实际向量库，collection 名 `biz`，字段包含 `content`、`vector`、`metadata`；`VectorStoreManager` 负责初始化、写入和查询。

**它不是什么：** 它不是知识库本身，只是知识文档的“可检索索引”；原始 SOP 文档在 `aiops-docs/`。

**与其他模块的关系：** 上游是 Embedding 服务，下游是 Retriever / RAG 工具。

**源码入口：** `app/services/vector_store_manager.py`、`app/core/milvus_client.py`。

### 2.5 RAG（检索增强生成）

**一句话理解：** 先查资料，再把资料和问题一起交给 LLM，让回答有依据。

**专业定义：** RAG（Retrieval-Augmented Generation）通过检索外部知识库获得相关上下文，再交给 LLM 生成答案，用于缓解幻觉和知识时效问题。

**在本项目中的作用：** `retrieve_knowledge` 工具从 Milvus 检索 SOP 文档并格式化为上下文；TriageAgent 用它查 TopologyKB / CaseKB，RunbookAgent 用它查 CaseKB / RunbookKB / TopologyKB，聊天 Agent 也把它作为本地工具。

**它不是什么：** 它不是一个模型，而是“Embedding + 向量库 + Retriever + 上下文格式化 + LLM”的组合流程。

**与其他模块的关系：** 依赖 Embedding 与 Milvus，服务于 LLM Agent。

**源码入口：** `app/tools/knowledge_tool.py`、`app/tools/__init__.py`（惰性加载）、`app/agents/triage_agent.py`、`app/agents/runbook_agent.py`。

### 2.6 Agent（智能体）

**一句话理解：** 能自己决定“调哪个工具、下一步做什么”的 LLM 程序。

**专业定义：** Agent 是具备工具调用、规划、状态记忆能力的智能体；通常由 LLM 作为推理核心，配合工具循环运行。

**在本项目中的作用：** 有两种真实形态：
- RAG 对话 Agent：`RagAgentService` 使用 LangGraph `create_agent(model, tools, checkpointer)`，带 `MemorySaver`，可调用本地工具和 MCP 工具。
- AIOps 流水线中的 LLM 节点：`TriageAgent`、`RunbookAgent` 是“LLM 结构化输出 + 规则回退”的类，不属于 LangGraph Agent；`ActionOrchestrator`、`Verifier`、`Replanner` 是确定性逻辑。

**它不是什么：** 不等于 LLM，也不等于 Workflow。LLM 是 Agent 的一部分；Workflow 是固定流程，Agent 是流程中的“会决策的节点”。

**与其他模块的关系：** Agent 调用 Tool / MCP / RAG，使用 LLM，并把自己的结果交给 Workflow。

**源码入口：** `app/services/rag_agent_service.py`、`app/agents/*`。

### 2.7 Workflow（工作流）

**一句话理解：** 把多步处理按顺序或条件串起来的流程。

**专业定义：** Workflow 定义节点、执行顺序、状态传递与分支路由，负责把“谁先做、失败后怎么办”固定下来。

**在本项目中的作用：** AIOps 主流水线由 `IncidentRouter.route()` 作为 async generator 实现（非 LangGraph）：归一化 → 去重 → 分级 → ML 检测 → 诊断 → 计划 → 执行 → 验证 → 重规划。失败恢复使用 LangGraph `StateGraph` 的 PRP 恢复图（planner → executor → replanner）。

**它不是什么：** 主流水线不是 Agent，也不是 LLM；它是确定性编排。

**与其他模块的关系：** Workflow 调用 ML、Agent、RAG、Tool，并通过 `IncidentStore` / `AuditStore` 持久化状态与审计。

**源码入口：** `app/core/incident_router.py`、`app/services/aiops_service.py`、`app/agent/aiops/*`。

### 2.8 Node（节点）

**一句话理解：** 图或流程中的一个“执行步骤”。

**专业定义：** 在 LangGraph 中，Node 是一个接收 State、返回 State 更新的函数；Workflow 由 Node 和 Edge 组成。

**在本项目中的作用：** PRP 恢复图有三个 LangGraph Node：`planner`、`executor`、`replanner`；主流水线中的“步骤”（归一化、检测等）在代码中以函数/阶段方式存在，不叫 LangGraph Node。

**它不是什么：** 不是所有类都叫 Node；只有图/流程中的执行单元才是。

**与其他模块的关系：** Node 属于 Workflow，内部调用 LLM / Tool。

**源码入口：** `app/agent/aiops/planner.py`、`app/agent/aiops/executor.py`、`app/agent/aiops/replanner.py`。

### 2.9 State（状态）

**一句话理解：** 流程运行过程中“随身携带的数据包”。

**专业定义：** State 是 Workflow 节点之间传递的共享数据；LangGraph 通过 State 记录输入、计划、历史步骤和结果。

**在本项目中的作用：** 两处：
- `PlanExecuteState`：PRP 恢复图的输入、计划、已执行步骤、恢复结果。
- `IncidentState` + `StateMachine`：事件从 NEW → TRIAGED → PLANNED → EXECUTING → VERIFIED → RESOLVED，或进入 COMPENSATING / FAILED / ESCALATED。

**它不是什么：** 不是数据库里的持久化表；它是运行期状态（事件状态同时会写入 `IncidentStore`）。

**与其他模块的关系：** Workflow 读写 State；`StateMachine` 校验合法状态迁移。

**源码入口：** `app/agent/aiops/state.py`、`app/core/state_machine.py`、`app/models/incident.py`。

### 2.10 Tool（工具）

**一句话理解：** 一个可以被调用的具体功能函数。

**专业定义：** Tool 封装“输入参数 → 输出结果”，并暴露给 Agent 调用；在 LangChain 中用 `@tool` 或 BaseTool 实现。

**在本项目中的作用：** 本地工具包括 `retrieve_knowledge`、`get_current_time`、`query_prometheus_alerts`；Mock 动作工具包括 `switch_backup_link`、`restart_gateway`、`block_suspicious_source`、`rollback_*`、`verify_network_health` 等。

**它不是什么：** 不等于 MCP。本地工具是普通函数；MCP 是跨进程协议，MCP 工具只是工具的一种来源。

**与其他模块的关系：** Agent 调用 Tool；ActionOrchestrator 调用 Mock 动作工具；RAG 是其中一个 Tool 的底层能力。

**源码入口：** `app/tools/__init__.py`、`app/tools/mock_actions.py`、`app/tools/knowledge_tool.py`。

### 2.11 MCP（模型上下文协议）

**一句话理解：** 让 Agent 通过统一协议调用外部服务的“插头”。

**专业定义：** MCP（Model Context Protocol）定义客户端与工具服务器之间的 JSON-RPC 协议；客户端发现并调用服务器暴露的工具。

**在本项目中的作用：** `MultiServerMCPClient` 连接两个服务器：CLS 日志服务（8003，`cls_server.py`）和 Monitor 监控服务（8004，`monitor_server.py`），提供日志类工具（CLS）与 CPU/内存监控类工具（Monitor）；带重试拦截器和安全加载（失败时仅使用本地工具）。

**它不是什么：** 不是数据库，也不是普通函数调用；它是“外部工具服务化”的协议层。

**与其他模块的关系：** 只在 RAG 对话 Agent 中加载；AIOps 主流水线不使用 MCP。

**源码入口：** `app/agent/mcp_client.py`、`mcp_servers/cls_server.py`、`mcp_servers/monitor_server.py`、`app/config.py`。

### 2.12 Orchestrator（编排器）

**一句话理解：** 负责“按顺序安排别人干活”的角色。

**专业定义：** Orchestrator 是协调多个组件完成业务目标的控制组件，决定调用顺序、重试、补偿与升级。

**在本项目中的作用：** 两层：
- `ActionOrchestrator`：执行 RunbookPlan 的动作，处理审批门禁、超时、重试、补偿。
- `AIOpsService`：编排“主流水线 → PRP 恢复 → 安全控制”的总体流程。

**它不是什么：** 不是 Agent；它不靠 LLM 自由决策，而是按固定策略执行。

**与其他模块的关系：** Orchestrator 调用 Tool / Agent / Store，向上承接 Workflow。

**源码入口：** `app/agents/action_orchestrator.py`、`app/services/aiops_service.py`。

### 2.13 Service（服务）

**一句话理解：** 把一类业务能力封装成可复用的类。

**专业定义：** Service 层是 API 与底层组件之间的业务门面，负责初始化依赖、编排调用、返回业务结果。

**在本项目中的作用：** `AIOpsService`（事件处置）、`RagAgentService`（对话）、`VectorStoreManager`（向量库）、`DashScopeEmbeddings`（嵌入）、`VectorIndexService` / `VectorSearchService`（索引/检索辅助）。

**它不是什么：** 不是 HTTP 路由，也不是数据模型；它通常被 API 层调用。

**与其他模块的关系：** Service 连接 API、Workflow、LLM、向量库。

**源码入口：** `app/services/*`。

### 2.14 Retriever（检索器）

**一句话理解：** 负责“从向量库里把最相关的文档找出来”的对象。

**专业定义：** Retriever 将查询向量化后，在向量库中检索 Top-K 相似文档，并返回 Document 列表。

**在本项目中的作用：** `knowledge_tool.py` 中 `vector_store.as_retriever(search_kwargs={"k": config.rag_top_k})`，默认 Top-3。

**它不是什么：** 不是 RAG 的全部；它只是 RAG 的检索环节。

**与其他模块的关系：** 上游是 Vector Store，下游是上下文格式化与 LLM。

**源码入口：** `app/tools/knowledge_tool.py`。

### 2.15 Pipeline（流水线）

**一句话理解：** 数据像流水线一样依次经过多个处理阶段。

**专业定义：** Pipeline 是按固定顺序执行的一组处理步骤，前一步输出作为后一步输入。

**在本项目中的作用：** AIOps 事件处置链：`EventNormalizer → Deduplicator → SeverityEngine → AttackDetector → TriageAgent → RunbookAgent → ActionOrchestrator → Verifier → Replanner`。

**它不是什么：** 不是 Agent，也不是 Workflow 的同义词；Pipeline 强调线性数据流，Workflow 还可包含分支、重试与状态机。

**与其他模块的关系：** Pipeline 串联 ML、Agent、Tool、Store。

**源码入口：** `app/core/incident_router.py`。

### 2.16 Feature Engineering（特征工程）

**一句话理解：** 把原始数据整理成模型认识的“特征列”。

**专业定义：** 特征工程是从原始数据中提取、转换、选择模型输入特征的过程。

**在本项目中的作用：** 生产链路使用 `ZLFeatureAdapter`，把 `RailMetricRecord` 转成 `Distance`、`PacketLoss`、`Latency` 三个浮点特征（V2 无 scaler）。另有 `FeatureExtractor` 可生成更完整的 `FeatureVector`，但未接入 ZL 推理主链路。

**它不是什么：** 不是模型训练，也不是推理本身；它发生在“推理前”。

**与其他模块的关系：** 输入是 `RailMetricRecord`，输出进入 `predict_proba`。

**源码入口：** `app/ml/zl_attack_detector.py`、`app/ml/feature_extractor.py`、`app/models/metrics.py`。

### 2.17 Inference（推理）

**一句话理解：** 用训练好的模型对一条新数据做预测。

**专业定义：** Inference 是模型前向计算过程；分类模型通常输出各类别概率。

**在本项目中的作用：** `ZLAttackDetector._predict_probabilities()` 调用 `classifier.predict_proba()`，得到概率数组，再求 argmax 得到预测类别，并输出置信度与全类别概率。

**它不是什么：** 不是 LLM 生成文本；ML 推理输出的是数值/类别概率。

**与其他模块的关系：** 推理结果封装为 `AttackPrediction`，进入 TriageAgent 的 Prompt。

**源码入口：** `app/ml/zl_attack_detector.py`。

### 2.18 Context（上下文）

**一句话理解：** 模型或 Agent 在回答时“看到的背景信息”。

**专业定义：** Context 是输入给模型/流程的支撑信息，包括原始数据、检索结果、历史状态。

**在本项目中的作用：** 三处：`Incident.metrics_snapshot`（原始指标）、知识检索格式化文本（`format_docs`）、对话消息历史（`MemorySaver`）。

**它不是什么：** 不是模型参数，也不等于 Prompt 本身；Prompt 是“指令 + 上下文”的组合。

**与其他模块的关系：** RAG 产生上下文，LLM 消费上下文，Workflow 携带上下文。

**源码入口：** `app/models/incident.py`、`app/tools/knowledge_tool.py`、`app/services/rag_agent_service.py`。

### 2.19 Prompt（提示词）

**一句话理解：** 给 LLM 的“说明书 + 待办事项”。

**专业定义：** Prompt 是发送给 LLM 的文本输入，包含角色、任务、上下文、输出格式要求。

**在本项目中的作用：** `TRIAGE_PROMPT`、`RUNBOOK_PROMPT`、`planner_prompt`、`replanner_prompt`、`response_prompt`、聊天系统提示词均通过 `ChatPromptTemplate` 构造，并用 `with_structured_output()` 约束输出为 Pydantic 对象。

**它不是什么：** 不是代码逻辑；它只影响 LLM 输出，不能保证规则性。

**与其他模块的关系：** Prompt 把 ML 预测、RAG 上下文注入 LLM。

**源码入口：** `app/agents/triage_agent.py`、`app/agents/runbook_agent.py`、`app/agent/aiops/planner.py`、`app/agent/aiops/replanner.py`。

### 2.20 Model（模型）

**一句话理解：** 训练好的、能对新输入做预测/生成的参数化程序。

**专业定义：** Model 泛指已训练的参数集合与推理代码。本项目出现两类模型：ML 分类模型与 LLM 生成模型。

**在本项目中的作用：**
- ZL ML 模型：`HistGradientBoostingClassifier`，用于攻击分类。
- LLM：`qwen-max`（`ChatQwen`），用于诊断、计划、回答。

**它不是什么：** 一个词指两类完全不同的东西；说“模型”时必须区分 ML 模型还是 LLM。

**与其他模块的关系：** ML 模型输出 `AttackPrediction`；LLM 是 Agent 的推理核心。

**源码入口：** `app/config.py`、`app/ml/zl_attack_detector.py`、`app/services/rag_agent_service.py`。

### 2.21 Provider（供应商 / 服务提供方）

**一句话理解：** 提供模型、数据库、工具服务的“外部厂商或服务进程”。

**专业定义：** Provider 是外部能力提供者，通过 API/协议向应用提供模型或数据服务。

**在本项目中的作用：** DashScope 提供 LLM 与 Embedding；Milvus 提供向量库；Prometheus 提供指标查询；CLS / Monitor MCP Server 提供日志与监控工具。

**它不是什么：** 不是模型本身，也不是本地业务模块；它是系统边界外的依赖。

**与其他模块的关系：** Service 层封装 Provider 访问；MCP 客户端连接 MCP Server。

**源码入口：** `app/config.py`、`app/services/vector_embedding_service.py`、`app/agent/mcp_client.py`。

## 3. 重点解决四个容易混淆的问题

### 3.1 ML vs Embedding vs LLM

```text
ML Model（ZL 分类器）
→ 处理什么？Distance、PacketLoss、Latency 三个数值特征
→ 输出什么？AttackPrediction：攻击类型 + 各类别概率 + 置信度

Embedding Model（text-embedding-v4）
→ 处理什么？一段文本
→ 输出什么？一个 1024 维向量

LLM（qwen-max）
→ 处理什么？Prompt：事件信息 + ML 预测 + RAG 上下文
→ 输出什么？诊断报告 / 处置计划 / 自然语言回答
```

三者不是同一类东西：ML 做数值分类，Embedding 做文本向量化，LLM 做文本生成。它们可以串成一条链：ML 先判断攻击类型，RAG 检索资料，LLM 再把两者写成解释和计划。

### 3.2 Agent vs Workflow

当前项目真实实现：
- Workflow：`IncidentRouter.route()` 是 async generator 流水线，按固定顺序执行；PRP 恢复图是 LangGraph `StateGraph`。
- Agent：聊天助手是 LangGraph `create_agent`（LLM + 工具循环）；AIOps 中的 TriageAgent / RunbookAgent 是“LLM 结构化输出 + 规则回退”的节点，ActionOrchestrator / Verifier / Replanner 是确定性逻辑。

为什么有了 Workflow 还需要 Agent？Workflow 负责“稳定地按顺序跑、状态不丢、失败有出口”；Agent 负责“这件事到底怎么诊断、怎么处置”。攻击类型已经由 ML 给出，但“影响是什么、为什么、按什么 SOP 恢复”需要 LLM 推理；同时不能把整个流程交给 LLM，否则状态、审计、重试会失控。因此本项目是“确定性 Workflow 编排 + LLM 节点参与决策”。

类比只是辅助：可以想象 Workflow 是流水线轨道，Agent 是轨道上的“会思考的工人”；但这不等于实现，真实实现以源码为准。

### 3.3 RAG vs LLM

为什么不直接把问题交给 LLM？因为 LLM 不知道本项目特定的运维 SOP、设备拓扑、历史案例；硬问会产生幻觉。RAG 先通过 `retrieve_knowledge` 从 Milvus 检索相关文档，把参考资料拼进 Context，再让 LLM 基于资料回答。

当前项目实际知识来源：`aiops-docs/` 中的运维 SOP 文档被向量化进 Milvus `biz` collection；TriageAgent 查询 TopologyKB / CaseKB，RunbookAgent 查询 CaseKB / RunbookKB / TopologyKB，聊天 Agent 使用 `retrieve_knowledge`。

RAG 不是模型：它是“Embedding + 向量库 + Retriever + 上下文格式化 + LLM”的流程。

### 3.4 MCP vs 普通函数调用

普通函数调用：进程内直接调用 `retrieve_knowledge`、`get_current_time` 等，没有网络协议。

MCP：通过 JSON-RPC 协议跨进程调用外部服务器暴露的工具。本项目使用 `MultiServerMCPClient` 连接 CLS（8003）和 Monitor（8004）两个 FastMCP 服务器，并带重试拦截器与安全加载。

为什么需要 MCP：日志查询、监控数据查询是独立服务能力，通过 MCP 可以统一“发现工具、调用工具、失败降级”，而不把外部服务细节写死在 Agent 里。不要泛化成“MCP 就是调用工具”：本地工具本来也是函数调用，MCP 增加的是协议、进程边界和外部服务封装。

## 4. 角色分工表

| 角色 | 初学者可以理解为 | 当前项目真实职责 | 输入 | 输出 |
| --- | --- | --- | --- | --- |
| ML 模型 | 攻击类型判断器 | ZL 分类器推理 | `RailMetricRecord` | `AttackPrediction` |
| LLM | 会写中文报告的模型 | 诊断、计划、回答 | Prompt（事件 + 预测 + 知识） | 结构化文本结果 |
| Embedding | 文本转向量器 | 文档/查询向量化 | 文本 | 1024 维向量 |
| 向量库 | 相似文本搜索库 | 存文档向量并检索 | 向量 + 查询 | Top-K 文档 |
| Retriever | 搜索执行者 | Milvus 相似检索 | 查询文本 | `List[Document]` |
| RAG | 检索增强问答 | 检索 + 格式化上下文给 LLM | 问题 | 上下文文本 |
| Agent | 会决策的工具调用者 | 聊天 Agent / LLM 诊断节点 | 用户问题或 Incident | 回答 / 诊断 / 计划 |
| Workflow | 流程编排 | 事件处置流水线 / PRP 恢复图 | 事件 + 状态 | SSE 事件流 |
| Node | 图中的一个步骤 | planner / executor / replanner | State | 更新后的 State |
| State | 流程携带的数据 | PlanExecuteState / IncidentState | 事件/执行结果 | 状态迁移 |
| Tool | 可调用功能 | 知识检索、时间、Prometheus、Mock 动作 | 参数 | 结果 |
| MCP | 外部工具协议 | CLS / Monitor 工具接入 | 工具调用 | 外部服务结果 |
| Orchestrator | 干活安排者 | ActionOrchestrator / AIOpsService | 计划 / 失败上下文 | 动作结果 / 恢复决策 |
| Service | 业务门面 | AIOps / RAG / 向量服务 | 业务请求 | 业务结果 |
| Pipeline | 固定处理链 | 归一化 → 检测 → 诊断 → 处置 | 原始事件 | 处置结果 |
| Provider | 外部供应商 | DashScope / Milvus / Prometheus / MCP | API 调用 | 模型 / 数据 / 工具 |

## 5. 新手最容易产生的误解

1. “ML 模型负责所有 AI” — 错。ZL 只做攻击分类；解释、计划、回答由 LLM / Agent / Workflow 完成。
2. “LLM 就是 Agent” — 错。LLM 是模型；Agent 是 LLM + 工具 + 循环 + 状态。
3. “RAG 是一个模型” — 错。RAG 是“嵌入 + 向量库 + 检索 + 上下文 + LLM”的流程。
4. “Agent 就是 Workflow” — 错。Agent 做决策；Workflow 做固定编排。本项目两者并存。
5. “Workflow 就是 Agent” — 错。主流水线不是 Agent，只有其中的 LLM 节点具备推理决策能力。
6. “MCP 是数据库” — 错。MCP 是协议；数据库是 Milvus。
7. “Embedding 是 LLM” — 错。Embedding 输出向量，LLM 输出文本。
8. “Tool 就是 MCP” — 错。本地工具是普通函数；MCP 是工具的外部协议来源之一。
9. “向量库就是知识库” — 半对。向量库是知识文档的可检索索引；原始知识在 `aiops-docs/`。
10. “Pipeline 就是 Agent” — 错。Pipeline 是固定数据流；Agent 是其中会决策的组件。
11. “所有 Agent 都一定依赖 LLM” — 错。ActionOrchestrator、Verifier、Replanner 是确定性逻辑；Triage / Runbook 在 LLM 失败时有规则回退。
12. “Provider 就是模型” — 错。Provider 是外部服务供应商，模型由 Provider 托管。
13. “Feature Engineering 就是模型训练” — 错。本项目只做推理前的特征适配；模型训练发生在 ZL 仓库。
14. “Inference 就是 LLM 生成” — 错。ML 推理输出数值概率；LLM 生成是另一类推理。

## 6. 如何继续深入

- 想看整体结构：`docs/PROJECT_MENTAL_MODEL.md`
- 想看真实请求跑一遍：`docs/PROJECT_RUNTIME_STORY.md`
- 想看完整源码导读：`docs/PROJECT_SOURCE_CODE_WALKTHROUGH.md`