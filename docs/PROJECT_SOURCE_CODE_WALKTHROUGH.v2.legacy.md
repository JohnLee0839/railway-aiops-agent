# 《railways_V.2 铁路智能运维系统 · 源码带读白皮书》

> **版本**：基于 2026-08-14 当前仓库源码重新生成的版本（第 2 版）
> **上一版**：《铁路智能运维系统源码详解.md》（仓库根目录，Metric-driven AIOps 版）
> **重写原因**：项目已接入作者自研的 ML 威胁检测模型（ZL STSRS 模型），旧白皮书中的架构、调用链、数据流与"TriageAgent 是唯一诊断者"的核心结论已不再准确。本文以当前源码为唯一事实依据，重新建立完整技术认知。
> **本文约定**：所有源码结论均标注 `文件:L行号`；所有结论标注事实等级（L1=源码直接证据，L2=调用链推导，L3=架构推断，L4=建议）。

---

# 第一部分 项目总览

## 1.1 项目定位

`railways_V.2` 是一个铁路信号系统智能运维（AIOps）平台，同时具备两条相互独立的子系统：

1. **RAG 对话子系统**（`/api/chat*`）：面向用户问答，Agent 可调用 Milvus 知识库检索工具与 MCP 工具。
2. **AIOps 事件处置子系统**（`/api/aiops/*`）：接收原始监测指标 / 告警，完成"归一化 → 去重 → 分级 → **ML 威胁检测** → LLM 诊断 → 计划生成 → 动作执行 → 验证 → 重规划 → 恢复/升级"的完整闭环。

**源码证据**：
- 两条子系统的路由注册：`app/main.py:L62-L65`
- RAG 对话服务：`app/services/rag_agent_service.py:L82-L83`
- AIOps 服务：`app/services/aiops_service.py:L42-L43`

## 1.2 当前版本架构（一句话）

```
输入（指标/告警/对话）
  ↓
FastAPI（app/main.py）
  ├─ RAG 链路:  RagAgentService(LangGraph create_agent + 本地工具 + MCP 工具)
  └─ AIOps 链路: AIOpsService
       ├─ 主流水线（事件驱动 Python 管道，非 LangGraph）:
       │    IncidentRouter.route()
       │      EventNormalizer → Deduplicator → SeverityEngine
       │      → AttackDetector.predict()  ★ 监督学习 ML 模型（新增，核心变化）
       │      → TriageAgent → RunbookAgent → ActionOrchestrator
       │      → Verifier → Replanner（重试/补偿/升级循环）
       ├─ 失败恢复: PRP Recovery Engine（LangGraph StateGraph: planner→executor→replanner）
       └─ 兜底:     Safety Control（回滚 + 人工升级）
```

## 1.3 技术栈（以 `pyproject.toml` 为事实）

| 技术 | 依赖声明 | 在源码中的实际用途 |
|---|---|---|
| FastAPI + uvicorn + sse-starlette | `pyproject.toml:L9-L11` | HTTP 服务与 SSE 流式输出 |
| LangChain / langgraph / langchain-openai | `pyproject.toml:L12-L16` | Chat 链路 Agent、PRP 恢复图 |
| langchain-qwq（ChatQwen） | `pyproject.toml:L32` | 所有 LLM 调用（见 11.7） |
| dashscope / openai | `pyproject.toml:L17-L18` | DashScope OpenAI 兼容接口 |
| pymilvus / langchain-milvus | `pyproject.toml:L19` `L28` | 向量库（collection `biz`） |
| pydantic / pydantic-settings | `pyproject.toml:L20-L21` | 数据模型与配置 |
| fastmcp / langchain-mcp-adapters | `pyproject.toml:L30-L31` | MCP Server / Client |
| **numpy / scikit-learn** | `pyproject.toml:L33-L34` | **ZL ML 模型推理（本次新增）** |
| httpx / aiohttp / aiofiles / python-multipart | `pyproject.toml:L22-L25` | HTTP 客户端、文件上传 |
| loguru | `pyproject.toml:L26` | 日志 |

**环境实测**（`uv` 环境）：numpy 2.4.2、sklearn 1.9.0 已安装，真实 ZL pickle 模型可反序列化并完成推理（实测输出见 5.7.5）。

## 1.4 核心模块（按当前源码实际职责）

| 模块 | 文件 | 职责（源码自述/实测） |
|---|---|---|
| API 层 | `app/api/*.py` | 请求解析、SSE 包装、转发服务层 |
| 服务层 | `app/services/*.py` | RAG Agent 服务、AIOps 编排、向量索引/嵌入 |
| 事件路由 | `app/core/incident_router.py` | 主流水线编排（含 ML 检测节点） |
| 核心设施 | `app/core/*.py` | 状态机、事件/审计存储、Milvus 客户端 |
| **ML 模块** | `app/ml/*.py` | **AttackDetector 接口 + ZL 模型适配器（新增）** |
| 事件处理 | `app/events/*.py` | 归一化、去重、分级、超时/熔断 |
| 数据层 | `app/data/stsrs_adapter.py` | STSRS 数据读取 + 多源融合（隐藏攻击标签） |
| 多 Agent | `app/agents/*.py` | Triage / Runbook / ActionOrchestrator / Verifier / Replanner |
| Legacy 恢复 | `app/agent/aiops/*.py` | PRP（Plan-Execute-Replan）恢复图 |
| 工具 | `app/tools/*.py` | 知识检索、时间、Prometheus 告警、Mock 动作 |
| MCP 服务端 | `mcp_servers/*.py` | CLS 日志（8003）、Monitor 监控（8004） |

## 1.5 项目目录结构（当前真实结构）

```
railways_V.2/
├── app/
│   ├── main.py                    # FastAPI 入口
│   ├── config.py                  # Pydantic Settings 配置（含 ML 配置）
│   ├── api/                       # chat.py / aiops.py / file.py / health.py
│   ├── services/                  # aiops_service / rag_agent_service / vector_*
│   ├── core/                      # incident_router / state_machine / incident_store
│   │                              # audit_store / milvus_client / llm_factory(未使用)
│   ├── events/                    # event_normalizer / deduplicator / severity_engine / timeout_manager
│   ├── agents/                    # triage / runbook / action_orchestrator / verifier / replanner
│   ├── agent/                     # mcp_client.py + aiops/ (PRP 恢复图)
│   ├── ml/                        # attack_detector / zl_attack_detector / feature_extractor
│   ├── models/                    # incident.py / metrics.py / request.py / response.py
│   ├── data/                      # stsrs_adapter.py
│   ├── tools/                     # knowledge_tool / time_tool / query_metrics_alerts / mock_actions
│   └── utils/logger.py
├── mcp_servers/                   # cls_server.py(8003) / monitor_server.py(8004)
├── tests/                         # 3 个测试文件 + test_data/（STSRS 数据）
├── aiops-docs/                    # 知识库原始文档（5 篇运维 SOP）
├── static/                        # 前端 index.html / app.js
├── docs/                          # architecture/ interview/（旧文档，历史参考）
├── uploads/                       # 上传文件目录（RAG 索引入口）
├── volumes/                       # milvus/etcd/minio 数据卷
├── vector-database.yml            # Milvus docker-compose
├── Makefile / start-windows.bat   # 启动脚本
├── .env                           # 实际生效配置
└── 铁路智能运维系统源码详解.md      # 旧白皮书（本文的历史参考）
```

> 注意：旧白皮书提到的 `app/data/prometheus_simulator.py`、`app/agents/incident_router.py` 在当前仓库中**已不存在**（见 16.1 问题 16）。

## 1.6 与旧白皮书的关键差异（本次重写的直接原因）

| 维度 | 旧白皮书（Metric-driven 版） | 当前源码事实 |
|---|---|---|
| 攻击类型判定者 | TriageAgent 是"唯一/真正诊断 Agent" | **AttackDetector（ML）先回答 "What happened?"，TriageAgent 负责"验证、解释、补充"**。`app/agents/triage_agent.py:L1-L15` 明确描述新协作模式 |
| ML 模型 | 不存在（技术栈表无 numpy/sklearn） | `app/ml/` 三个文件 + `pyproject.toml:L33-L34` 依赖 |
| IncidentRouter | 未提及检测器 | 构造函数注入 `create_attack_detector()`：`app/core/incident_router.py:L55-L66`；`route()` 含检测步骤 `L164-L184` |
| 数据流 | Normalizer → Severity → Triage | 插入新节点：Severity → **AttackDetector.predict()** → Triage |
| config.py | "无需额外配置" | 新增 8 个 ML 配置项：`app/config.py:L56-L64` |
| TriageAgent KB 查询 | `_query_casekb_by_patterns`（纯异常模式） | `_query_casekb_by_prediction`（模型预测优先，异常模式回退）：`app/agents/triage_agent.py:L321-L397` |
| 调用链 | 无 ML 分支 | ML 输出写入 `Incident.attack_prediction`，进入 LLM Prompt 与 KB 查询 |

---

# 第二部分 项目整体架构

## 2.1 系统总体架构

```
                         ┌────────────────────────────────────────────┐
                         │            FastAPI (app/main.py)           │
                         │   /api/chat*   /api/upload   /api/aiops/*  │
                         └───────────────┬────────────────────────────┘
                ┌────────────────────────┼──────────────────────────────┐
                ▼                        ▼                              ▼
   ┌────────────────────┐   ┌────────────────────┐      ┌──────────────────────────┐
   │ RAG 对话链路        │   │ 文件索引链路        │      │ AIOps 事件链路            │
   │ rag_agent_service  │   │ vector_index_      │      │ aiops_service            │
   │ (LangGraph agent)  │   │ service            │      │  ├ 主流水线(路由+5 Agent)  │
   │  ├ retrieve_       │   │  ├ splitter        │      │  ├ PRP 恢复图(LangGraph)  │
   │  │  knowledge(Milvus)│  │  ├ embedding       │      │  └ Safety Control         │
   │  ├ get_current_time│   │  └ Milvus          │      └──────────────────────────┘
   │  ├ query_prometheus│   └────────────────────┘
   │  └ MCP 工具(cls/   │
   │     monitor)       │
   └────────────────────┘
```

- **两条子系统共享的资源**：Milvus 知识库（`app/services/vector_store_manager.py:L153` 单例）、MCP 客户端（`app/agent/mcp_client.py:L17` 全局单例）、配置（`app/config.py:L92`）。
- **两条子系统互不调用**：AIOps 流水线不使用 RagAgentService，RagAgentService 不感知 Incident 流水线（L2：`app/services/aiops_service.py` 与 `app/services/rag_agent_service.py` 之间无 import 关系）。

## 2.2 模块关系

```
app/api/aiops.py ──→ app/services/aiops_service.py ──→ app/core/incident_router.py
                                                           │
                    ┌──────────────────────────────────────┼───────────────────────────┐
                    ▼                                      ▼                           ▼
          app/events/*（归一化/去重/分级）        app/ml/*（ML 检测器）        app/agents/*（5 个 Agent）
                                                           │                           │
                                                           ▼                           ▼
                                          app/models/metrics.py         app/tools/*（KB 检索 + Mock 动作）
                                                                                     │
                                                                        app/services/vector_store_manager.py → Milvus
```

- `app/agent/aiops/*`（PRP 恢复图）仅被 `app/services/aiops_service.py` 调用（`app/services/aiops_service.py:L23`），是"失败后的内部恢复策略"，没有任何公开 API 能直接启动它（`app/services/aiops_service.py:L11-L14` 源码自述）。
- `app/core/__init__.py:L7-L9` 明确说明 IncidentRouter 不在 `__init__` 中 eager import，以避免 `incident_router → triage_agent → tools → milvus_client → core.__init__` 循环导入；`app/agents/__init__.py:L13-L15` 同样使用 lazy accessor。

## 2.3 数据流（宏观）

```
原始指标（JSON/STSRS 文件）
  → AIOpsService.process_metrics/process_stsrs
  → EventNormalizer.normalize（Metric-driven 路径 Incident，attack_type=UNKNOWN；Prometheus 基础设施告警 / Manual 例外见 7.1）
  → Deduplicator.process（窗口去重）
  → SeverityEngine.evaluate（规则分级 P1-P4）
  → IncidentRouter._build_metric_record（Incident → RailMetricRecord）
  → AttackDetector.predict（ZL/规则/Mock → AttackPrediction）        ★ ML 介入点
  → TriageAgent.triage（LLM 验证/解释/补诊断 → TriageResult）
  → RunbookAgent.generate_plan（KB 检索 + LLM → RunbookPlan）
  → ActionOrchestrator.execute_plan（Mock 动作，超时/重试/审批）
  → Verifier.verify（成功/重试/补偿/升级）
  → Replanner.decide（5 态路由，≤3 轮循环）
  → 终态 RESOLVED / FAILED / ESCALATED
  →（失败）PRP 恢复图 →（再失败）Safety Control
  → SSE 事件流（全程） + AuditStore（全程）
```

**关键事实**：ML 模型的输出 `AttackPrediction` 写入 `Incident.attack_prediction` 字段（`app/models/incident.py:L181-L184`），随后被 TriageAgent 消费（`app/agents/triage_agent.py:L129-L131`）。**ML 输出不直接决定动作**——动作计划由 RunbookAgent 基于 TriageResult 生成（`app/core/incident_router.py:L269`）。

> **当前源码限制（2026-08-14 实测）**：`Replanner.decide` 与 `_common_pipeline` 的双重状态迁移会使真实成功/补偿/升级/失败分支抛 `ValueError`，上述“终态 RESOLVED / FAILED / ESCALATED”在默认真实 Replanner 下无法按图示到达（见 7.5、14.1、16.1-17）。

## 2.4 控制流（宏观）

- **主流水线**：单线程 async 生成器顺序执行（`async def route` / `async def _common_pipeline`，`app/core/incident_router.py:L107-L198`、`L204-L506`）；重试/补偿是 `while retry_cycle < max_cycles` 循环（`L326`）。
- **状态机**：`StateMachine.transition()` 校验每次迁移合法性（`app/core/state_machine.py:L121-L201`），非法迁移抛 `ValueError`。
- **恢复图**：LangGraph 状态图，条件边 `should_continue` 决定继续执行或结束（`app/services/aiops_service.py:L452-L488`）。
- **异步**：SSE 通过 `asyncio.Queue` 在订阅者间分发（`app/core/audit_store.py:L128-L136`）。

## 2.5 外部依赖（运行时）

| 外部系统 | 配置 | 使用方 |
|---|---|---|
| 阿里云 DashScope（qwen-max / text-embedding-v4） | `app/config.py:L27-L30` | Triage/Runbook/PRP LLM、Embedding |
| Milvus（localhost:19530） | `app/config.py:L32-L35` | RAG 检索与写入 |
| MCP：CLS 当前 `.env` 生效值 localhost:3000/sse（config 默认 8003）、Monitor localhost:8004 | `app/config.py:L45-L50` + `.env:L26-L33` | RAG 对话 Agent、PRP 恢复图 |
| Prometheus（127.0.0.1:9090） | `app/config.py:L52-L54` | `query_prometheus_alerts` 本地工具 |
| **ZL 模型产物（D:/STUDY/ZL/…，仓库外）** | `app/config.py:L59-L62` | **AIOps 威胁检测（新增）** |

---

# 第三部分 API / 服务入口源码导读

## 3.1 服务启动

**源码位置**：`app/main.py:L19-L41`（lifespan）、`L84-L93`（uvicorn 启动）

启动链路：
1. 进程入口：`python -m uvicorn app.main:app --host 0.0.0.0 --port 9900`（Makefile `run` 目标，`Makefile:L428-L430`；`app/main.py:L87-L92` 提供 `python app/main.py` 等价方式）。
2. `lifespan` 启动时：日志 banner → `milvus_manager.connect()`（`app/main.py:L31`，实现在 `app/core/milvus_client.py:L59-L141`）→ yield；关闭时 `milvus_manager.close()`（`app/main.py:L40`）。
3. 路由注册：`app/main.py:L62-L65`——health 无前缀；chat/file/aiops 均挂 `/api` 前缀。
4. 静态前端：`app/main.py:L69` 挂载 `/static`，`GET /` 返回 `static/index.html`（`L71-L81`）。

> 注意：`app/services/vector_store_manager.py:L33` 在**模块导入期**就调用 `milvus_manager.connect()`（早于 lifespan），其注释说明这是为了避免 langchain_milvus 访问 Collection 时抛 `ConnectionNotExistException`。这意味着 **Milvus 必须在应用启动前可用**，否则 `vector_store_manager` 导入即失败（L1）。

## 3.2 API Router 一览（全部端点，当前源码）

| 端点 | 文件:行 | 说明 |
|---|---|---|
| `GET /` | `app/main.py:L71-L81` | 返回前端首页 |
| `GET /health` | `app/api/health.py:L13-L64` | 健康检查（Milvus 不可用 → 503） |
| `POST /api/chat` | `app/api/chat.py:L20-L68` | 非流式对话 |
| `POST /api/chat_stream` | `app/api/chat.py:L71-L176` | SSE 流式对话 |
| `POST /api/chat/clear` | `app/api/chat.py:L179-L201` | 清空会话 |
| `GET /api/chat/session/{session_id}` | `app/api/chat.py:L204-L225` | 会话历史 |
| `POST /api/upload` | `app/api/file.py:L25-L103` | 上传文档并建索引 |
| `POST /api/index_directory` | `app/api/file.py:L106-L135` | 目录批量索引 |
| `POST /api/aiops/incident` | `app/api/aiops.py:L33-L123` | 通用事件入口（SSE） |
| `POST /api/aiops/stsrs` | `app/api/aiops.py:L126-L179` | STSRS 专用入口（SSE） |
| `POST /api/aiops/metrics` | `app/api/aiops.py:L186-L256` | **原始指标入口（ML 检测主入口，SSE）** |
| `GET /api/aiops/sse/{thread_id}` | `app/api/aiops.py:L263-L285` | 订阅审计事件流 |
| `GET /api/aiops/incidents` | `app/api/aiops.py:L292-L325` | 事件列表 |
| `GET /api/aiops/incidents/{id}` | `app/api/aiops.py:L328-L363` | 事件详情 |
| `GET /api/aiops/incidents/{id}/timeline` | `app/api/aiops.py:L366-L380` | 审计时间线回放 |
| `GET /api/aiops/stats` | `app/api/aiops.py:L383-L394` | 统计 |

**确认事实**：不存在 `POST /api/aiops` 端点，但前端 `static/app.js:L1181` 调用的是 `${apiBaseUrl}/aiops`（即 `POST /api/aiops`）→ **前端 AIOps 按钮会 404**（见 16.1 问题 5）。

## 3.3 Request / Response

- `app/models/request.py` 中仅有两个请求模型：`ChatRequest`（`L9-L22`，字段别名 `Id`/`Question`）、`ClearRequest`（`L25-L31`）。
- 响应模型：`ChatResponse`、`SessionInfoResponse`、`ApiResponse`、`HealthResponse`（`app/models/response.py:L10-L38`）。
- **AIOps 三个 POST 端点接收裸 `payload: dict`，无 Request Schema 校验**（`app/api/aiops.py:L35`、`L128`、`L187`）。格式解析在端点内手写：统一格式 `{"source": ..., "raw_event": {...}}` 或旧扁平格式（`app/api/aiops.py:L72-L95`）。
- AIOps 三个 POST 端点（incident/stsrs/metrics）响应统一为 SSE（`sse_starlette.EventSourceResponse`），`data` 为 JSON 字符串（`app/api/aiops.py:L106-L109`）；GET 查询端点（incidents/detail/timeline/stats）返回普通 JSON（`app/api/aiops.py:L292-L394`）。
- `RawIncidentRequest`（`app/models/incident.py:L276-L288`）也是 Pydantic 请求模型，但仅用于 `process_incident_stream` 内部解析统一格式，不是 FastAPI 请求体 schema。
- 业务数据模型集中在 `app/models/incident.py`（9 个枚举 + 20 个 Pydantic 模型）与 `app/models/metrics.py`（指标与预测模型）。

## 3.4 Service Layer

| 服务 | 文件 | 核心方法 |
|---|---|---|
| `AIOpsService`（单例） | `app/services/aiops_service.py:L42-L577` | `process_incident`(L55-L188)、`process_stsrs`(L190-L204)、`process_metrics`(L206-L243)、`_execute_recovery`(L245-L318)、`_execute_safety_control`(L320-L450)、`subscribe_sse`(L490-L496) |
| `RagAgentService`（单例，streaming=True） | `app/services/rag_agent_service.py:L82-L421` | `query`(L189-L252)、`query_stream`(L254-L322)、`get_session_history`(L324-L383)、`clear_session`(L389-L408) |
| `VectorIndexService`（单例） | `app/services/vector_index_service.py:L58-L174` | `index_directory`(L66-L128)、`index_single_file`(L130-L170) |
| `DocumentSplitterService`（单例） | `app/services/document_splitter_service.py:L13-L176` | `split_markdown`(L45-L81)、`split_text`(L83-L112)、`split_document`(L118-L132) |
| `VectorStoreManager`（单例） | `app/services/vector_store_manager.py:L18-L153` | `add_documents`(L63-L93)、`delete_by_source`(L95-L121)、`similarity_search`(L132-L149) |
| `DashScopeEmbeddings`（单例） | `app/services/vector_embedding_service.py:L12-L131` | `embed_documents`(L60-L91)、`embed_query`(L93-L123) |

## 3.5 异常处理（全景汇总）

| 位置 | 策略 | 源码 |
|---|---|---|
| API chat 端点 | try/except → 返回 `code:500` JSON | `app/api/chat.py:L58-L68` |
| API aiops 端点 | 生成器内 try/except → SSE `error` 事件 | `app/api/aiops.py:L112-L121` |
| Milvus 连接 | MilvusException/ConnectionError → RuntimeError，先 close | `app/core/milvus_client.py:L130-L141` |
| **ML 检测调用** | try/except 全捕获 → warn 后**继续流程**（TriageAgent 可独立诊断） | `app/core/incident_router.py:L182-L184` |
| **ML 加载失败** | FallbackAttackDetector 捕获 `AttackDetectorLoadError` → 规则检测器 | `app/ml/attack_detector.py:L108-L131` |
| **ML 输入非法** | `AttackDetectorInputError` **不吞掉**，向上传播（fallback 明确禁用） | `app/ml/attack_detector.py:L132-L134` |
| TriageAgent LLM 失败 | 规则回退诊断 `_fallback_triage` | `app/agents/triage_agent.py:L177-L179`、`L517-L561` |
| RunbookAgent LLM 失败 | 规则回退计划 `_fallback_plan` | `app/agents/runbook_agent.py:L165-L167`、`L305-L378` |
| KB 查询失败 | 各 Agent 内 try/except → 返回空上下文，不阻断 | `app/agents/triage_agent.py:L317-L319` 等 |
| Mock 动作 | TimeoutManager：重试 3 次 + 指数退避 + 熔断 + 升级 | `app/events/timeout_manager.py:L112-L210` |
| MCP 工具 | 拦截器重试 3 次，返回 `isError=True` 结果而非抛异常 | `app/agent/mcp_client.py:L46-L102` |
| 补偿执行异常 | → FAILED 终态 | `app/core/incident_router.py:L391-L402` |
| 恢复/安全控制异常 | 各自 try/except → error 事件 | `app/services/aiops_service.py:L311-L318`、`L381-L388` |
| 状态机非法迁移 | 抛 ValueError（审计回调异常则仅 warn） | `app/core/state_machine.py:L149-L155`、`L198-L199` |

**ML 模型推理失败时系统怎么办？（源码确认的完整答案）**
```
模型文件缺失 / sklearn 缺失 / pickle 损坏
   → AttackDetectorLoadError
   → FallbackAttackDetector 捕获 → RuleBasedAttackDetector（阈值规则）
   → AttackPrediction(fallback_used=True, fallback_reason=...)     [app/ml/attack_detector.py:L108-L131]
输入缺字段
   → AttackDetectorInputError → Fallback 不兜底 → IncidentRouter catch
   → warn，attack_prediction 为空，流程继续                        [app/core/incident_router.py:L182-L184]
推理输出违反契约（形状/概率和≠1）
   → AttackDetectorInferenceError → 同上，流程继续                  [app/ml/attack_detector.py:L135-L137]
TriageAgent LLM 失败
   → _fallback_triage 规则诊断                                     [app/agents/triage_agent.py:L517-L561]
```

---

# 第四部分 RAG 源码导读

## 4.1 文档处理（写入链）

```
POST /api/upload
  app/api/file.py:upload_file()           # 校验扩展名(txt/md)、大小(10MB)、保存 uploads/
  → app/services/vector_index_service.py:index_single_file()
     1. Path.read_text(utf-8)                                    [L150]
     2. vector_store_manager.delete_by_source(path)              [L155]  先删旧数据
     3. document_splitter_service.split_document()               [L158]
     4. vector_store_manager.add_documents(documents)            [L163]
```

- 扩展名白名单 `["txt","md"]`：`app/api/file.py:L20`；文件名清洗 `_sanitize_filename`：`app/api/file.py:L154-L169`。
- 索引失败**不影响上传成功**（仅记日志）：`app/api/file.py:L81-L83`。
- 批量入口 `POST /api/index_directory` 只扫 `*.txt`/`*.md`：`app/services/vector_index_service.py:L90`。

## 4.2 Chunk（文档分块）

**源码位置**：`app/services/document_splitter_service.py:L13-L172`

- 参数来自配置：`chunk_max_size=800`、`chunk_overlap=100`（`app/config.py:L42-L43`）。
- `.md` 三阶段（`split_markdown`，L45-L81）：
  1. `MarkdownHeaderTextSplitter` 按 `#`/`##` 标题分割（`L22-L29`，`strip_headers=False` 保留标题文本）；
  2. `RecursiveCharacterTextSplitter(chunk_size=1600, chunk_overlap=100)` 二次分割（`L32-L37`）；
  3. `_merge_small_chunks(min_size=300)` 合并过小分片（`L134-L172`）；
  4. 每个分片元数据写 `_source`/`_extension`/`_file_name`（`L71-L74`）。
- `.txt` 直接走递归分割器，chunk_size=1600（`split_text`，L83-L112）。

## 4.3 Embedding

**源码位置**：`app/services/vector_embedding_service.py:L12-L131`

- `DashScopeEmbeddings(Embeddings)` 实现 LangChain 标准接口，底层用 `openai.OpenAI(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")`（`L37-L40`）。
- 模型 `text-embedding-v4`、维度 1024（`L127-L131` 单例；配置在 `app/config.py:L30`）。
- 错误处理：失败 raise `RuntimeError`（`L89-L91`、`L121-L123`）；API Key 掩码记录日志（`L52-L57`）。

## 4.4 Vector Store（Milvus）

**源码位置**：`app/core/milvus_client.py:L44-L318`（底层）、`app/services/vector_store_manager.py:L18-L153`（LangChain 封装）

- Collection：`biz`（常量 `app/core/milvus_client.py:L48`；`app/services/vector_store_manager.py:L15`）。
- Schema：`id`(VARCHAR,100,主键) / `vector`(FLOAT_VECTOR,1024) / `content`(VARCHAR,8000) / `metadata`(JSON)（`app/core/milvus_client.py:L152-L173`）。
- 索引：IVF_FLAT、L2、nlist=128（`L192-L208`）。
- 维度不匹配时自动 drop 并重建 collection（`L102-L123`）。
- langchain_milvus 字段映射：`text_field=content`、`vector_field=vector`、`primary_field=id`、`metadata_field=metadata`、`auto_id=False`（`app/services/vector_store_manager.py:L42-L52`）。
- 兼容性补丁：`_patch_pymilvus_milvus_client_orm_alias()` 强制 MilvusClient 使用 `default` 连接别名（`app/core/milvus_client.py:L18-L41`）。
- 按 `_source` 删除：JSON 路径表达式 `metadata["_source"] == "..."`（`app/services/vector_store_manager.py:L106-L113`）。

## 4.5 Retriever

**源码位置**：`app/tools/knowledge_tool.py:L13-L48`

```python
@tool(response_format="content_and_artifact")
def retrieve_knowledge(query: str) -> Tuple[str, List[Document]]:
    vector_store = vector_store_manager.get_vector_store()
    retriever = vector_store.as_retriever(search_kwargs={"k": config.rag_top_k})   # top_k=3
    docs = retriever.invoke(query)
    ...
```

- `rag_top_k` 默认 3（`app/config.py:L38`）。
- **没有 reranker**（全文检索 "rerank" 无命中，L1）。
- 检索失败返回错误字符串而非抛异常（`L46-L48`）。

## 4.6 Context 构造

**源码位置**：`app/tools/knowledge_tool.py:L51-L85`（`format_docs`）

格式：`【参考资料 i】\n标题: h1 > h2\n来源: 文件名\n内容: page_content`。该上下文被 LLM 直接消费（如 TriageAgent 的 `_build_diagnosis_input` 拼接，`app/agents/triage_agent.py:L484-L489`）。

## 4.7 RAG 与 Agent 的关系

| 消费方 | 查询内容 | 源码 |
|---|---|---|
| RagAgentService（对话 Agent） | 用户问题 → `retrieve_knowledge` 工具 | `app/services/rag_agent_service.py:L104`（工具注册） |
| TriageAgent | TopologyKB（列车/信号拓扑）+ CaseKB（模型预测类型/异常模式） | `app/agents/triage_agent.py:L303-L319`、`L321-L397` |
| RunbookAgent | CaseKB → RunbookKB → TopologyKB 三级优先级 | `app/agents/runbook_agent.py:L173-L258` |
| PRP Planner（恢复引擎） | 经验文档检索后制定计划 | `app/agent/aiops/planner.py:L86-L99` |

- KB 的"分区"（CaseKB/RunbookKB/TopologyKB）**只是查询措辞的约定，底层是同一个 Milvus `biz` collection**（L2：`retrieve_knowledge` 不按 kb_type 过滤，`app/tools/knowledge_tool.py:L13-L48`；`KBQueryRequest.kb_type` 模型存在但无实现使用）。
- `app/tools/__init__.py:L29-L64` 对知识工具做**懒加载**——`retrieve_knowledge` 的导入会触发 Milvus 初始化，故延迟到首次调用（避免轻量测试/事件路由导入时依赖 Milvus，`L1-L6` 注释为证）。

## 4.8 RAG 与 ML 的关系（事实）

**当前源码中 ML 与 RAG 没有直接调用关系**：
- ZL 模型特征仅来自 `RailMetricRecord` 的指标字段（`app/ml/zl_attack_detector.py:L74-L96`），不来自知识库。
- RAG 不参与特征工程、不参与模型输入。
- 二者的间接关系：**ML 的预测结果（attack_type）成为 RAG 检索的查询词**——TriageAgent 优先用模型预测的类型查 CaseKB（`app/agents/triage_agent.py:L336-L348`）。这是当前代码中"ML 影响 RAG"的唯一路径（L1）。

---

# 第五部分 ML 模型源码导读（本次新增核心章节）

## 5.1 ML 模型定位

当前项目对 ML 的定位是**威胁检测的"第一答案"提供者**，与 LLM 明确分工（源码自述）：

> `app/ml/attack_detector.py:L9-L11`
> - AttackDetector: 回答 "What happened?" (分类: DoS / Jamming / Replay / ...)
> - TriageAgent: 回答 "Why? Impact? How to fix?" (解释 + RAG + 诊断报告)

ML 层只做**推理**，不含训练逻辑（`app/ml/__init__.py:L4-L9`；`app/ml/zl_attack_detector.py:L1-L6` "intentionally keeps training-time logic out of railways_V.2"）。

## 5.2 模型文件

**模型产物位于仓库之外的兄弟项目 `D:/STUDY/ZL`，当前版本 V2。**

### 5.2.1 模型文件本体（实测）

- 默认路径：`<project_root>/../ZL/models/baseline/v2_compact_top3_hist_gradient_boosting.pkl`（`app/ml/zl_attack_detector.py:L333-L338` 兜底候选；配置 `zl_model_root=../ZL` 见 `app/config.py:L59`）。
- **实测 pickle payload 结构**（2026-08-14 以 sklearn 1.9.0 反序列化验证）：

```python
keys: ['generated_at', 'model_name', 'feature_columns', 'sample_hash_columns',
       'target_column', 'target_id_column', 'target_mapping', 'config', 'classifier']
model_name       = 'v2_compact_top3_hist_gradient_boosting'
feature_columns  = ['Distance', 'PacketLoss', 'Latency']          # 仅 3 个特征
target_mapping   = {'Normal': 0, 'DoS': 1, 'Jamming': 2, 'ReplayAttack': 3}
classifier       = sklearn.ensemble.HistGradientBoostingClassifier（classes_=[0,1,2,3]）
scaler           = None（payload 中无此键）
```

- **模型类型**：`sklearn.ensemble.HistGradientBoostingClassifier`（梯度提升树，4 分类）。该类型名与模型文件名 `..._hist_gradient_boosting.pkl` 一致（L1 实测）。

### 5.2.2 模型元数据（manifest）

- 默认 manifest：`metadata/manifests/v2_compact_tree_manifest.json`（配置 `app/config.py:L62`；加载逻辑 `app/ml/zl_attack_detector.py:L347-L362`）。
- **实测 manifest 关键字段**：全量训练行数 6,994,527；采样 800,000（每类 200,000）；特征数 12→3（降维 75%）；测试集 macro F1 ≈ 0.9989；父实验 `v1_hist_gradient_boosting`；晋升自消融变体 `top3_only`。
- 注意：manifest 内记录的 `model_path` 是**旧机器绝对路径** `E:\ZL\STSRS\...`，适配器在解析时已做存在性回退处理（`app/ml/zl_attack_detector.py:L315-L345`；风险记录见 16.2）。

### 5.2.3 训练链路的位置（事实边界）

训练代码**不在本仓库**，位于 `D:/STUDY/ZL/src/stsrs_data_engineering/`：
- `baseline_training.py`（基线训练）、`v1_ablation.py`（特征消融实验）、`v2_compact_tree.py`（V2 紧凑模型，头部读到的 `V2CompactTreeResult` 与 `_write_v2_report` 证明其职责）、`model_service.py`（ZL 自己的推理服务实现，定义了 `LoadedModelArtifact`/`PredictionResult`，与本仓库的 `ZLAttackDetector` 是平行实现而非复用关系）、`scripts/run_model_service_smoke_test.py`（其自带 smoke 测试）。
- **本仓库不 import ZL 的任何代码**：`app/ml/zl_attack_detector.py:L10-L25` 的 import 仅有标准库 + loguru + 本仓模块——即 railways_V.2 直接加载 pickle 产物，与 ZL 训练工程解耦（L1）。

## 5.3 模型加载

**源码位置**：`app/ml/zl_attack_detector.py:L272-L313`（`_load_artifact`）、`L315-L345`（`_resolve_model_path`）

加载流程：
1. **缓存**：`self._artifact` 非空直接复用（`L273-L274`）——进程内只加载一次。
2. **路径解析**（`_resolve_model_path`）按顺序尝试：
   a. 配置的显式 `zl_model_path`（`L318-L319`）；
   b. manifest 的 `result.model_path`（存在性回退，`L321-L327`）；
   c. manifest 的 `result.model_name` → `models/baseline/{model_name}.pkl`（`L328-L331`）；
   d. 硬编码兜底 `models/baseline/v2_compact_top3_hist_gradient_boosting.pkl`（`L333-L338`）。
3. **反序列化**：`pickle.load(handle)`（`L279`），提取 `feature_columns`/`target_mapping`/`classifier`/`scaler`。
4. **失败语义**（`L287-L296`）：
   - `FileNotFoundError` → `AttackDetectorLoadError`；
   - `ImportError/ModuleNotFoundError`（如缺 sklearn）→ `AttackDetectorLoadError`；
   - `pickle.UnpicklingError/EOFError/KeyError/TypeError/ValueError` → `AttackDetectorLoadError`（产物不兼容/不完整）。
5. **numpy 依赖按需导入**：`_import_numpy()`（`L371-L380`），缺失时抛 `AttackDetectorLoadError`（可被 fallback 接住）。

## 5.4 数据输入

**输入类型**：`RailMetricRecord`（`app/models/metrics.py:L32-L51`）——由 `IncidentRouter._build_metric_record()` 从 `Incident.metrics_snapshot` 重建（`app/core/incident_router.py:L512-L560`）。

重建规则（L1）：
- `metrics_snapshot["metrics"]` 中仅取 `RailMetrics.model_fields` 定义的字段（`L532-L536`）；
- `source_metrics`/`source_files` 原样透传（`L539`、`L559`）；
- `train_id`/`signal_id` 优先 snapshot，回退 `incident.metadata`（`L542-L543`）；
- 时间戳解析失败回退 `incident.timestamp`（`L546-L550`）。

**上游来源**（两条路径）：
1. 实时 API：`POST /api/aiops/metrics` → `AIOpsService.process_metrics`（`app/services/aiops_service.py:L206-L243`）→ 包装为 `raw_event.metrics_snapshot`。
2. 离线数据：`STSRSAdapter.load_and_fuse()`（`app/data/stsrs_adapter.py:L110-L164`）→ `RailMetricRecord` 列表（当前无 API 直接消费该适配器，仅测试使用 `tests/test_stsrs_fusion.py`；AIOps 主链路通过 metrics_snapshot 承载融合结果）。

## 5.5 Feature Engineering（特征工程）——必须精确到字段

### 5.5.1 真正生效的特征工程：`ZLFeatureAdapter`

**源码位置**：`app/ml/zl_attack_detector.py:L47-L181`；调用点：`ZLAttackDetector.predict()` 内 `L219`。

字段映射表（`_FIELD_MAP`，`L50-L61`）：

| ZL 训练字段名 | RailMetricRecord 字段 | 类型处理 |
|---|---|---|
| `Speed` | `metrics.speed` | float 强转 |
| `Distance` | `metrics.distance` | float 强转 |
| `Location` | `metrics.location` | float 强转 |
| `SignalStatus` | `metrics.signal_status` | 枚举归一化 |
| `OverlapStatus` | `metrics.overlap_status` | 枚举归一化 |
| `OverlapCount` | `metrics.overlap_count` | float 强转 |
| `PacketLoss` | `metrics.packet_loss` | float 强转 |
| `Latency` | `metrics.latency` | float 强转 |
| `RenewalInterval` | `metrics.renewal_interval`；**缺失时回退** `source_metrics["train"]["renewal_interval"]` → 再回退 `source_metrics["control_center"]["renewal_interval"]` | float 强转 |
| `Burstiness` | `metrics.burstiness` | float 强转 |

关键处理细节（L1）：
- **特征顺序由模型产物决定**：`to_raw_input(record, artifact.feature_columns)` 只按 `feature_columns`（当前 = `['Distance','PacketLoss','Latency']`）收集字段并**保持该顺序**（`L83-L87`），随后 `np.asarray([[raw_input[c] for c in feature_columns]])`（`L222-L225`）。**特征顺序不依赖任何硬编码，直接跟随训练产物**——这是避免训练/推理偏移的正确做法。
- **缺失处理**：任何必需特征缺失 → 抛 `AttackDetectorInputError`（`L89-L94`），由 Fallback 明确拒绝兜底（`app/ml/attack_detector.py:L132-L134`），再由 Router 捕获后继续流程。
- **数值清洗** `_coerce_float`（`L134-L146`）：`None`/空串/bool → None；转换失败 → None；**非有限值（NaN/Inf）→ None**。
- **枚举归一化**：
  - `SignalStatus`：`green→Green`、`yellow→Yellow`、`red/danger/offline/failure→Red`，其余原样（`L148-L161`）；
  - `OverlapStatus`：`yes/true/1/abnormal/conflict/error→Yes`；`no/false/0/normal/unknown→No`，其余原样（`L163-L181`）。
- **categorical 处理**：当前 V2 模型**不用**这些枚举特征（feature_columns 只有 3 个数值字段），但适配器保留映射能力（manifest 的 `sample_hash_columns` 含 `SignalStatus__is_Green` 等 one-hot 风格列名，说明 ZL 训练工程曾做 one-hot；V2 训练时被降维丢弃）。
- **normalization / standardization**：`artifact.scaler` 为空时**不做标准化**（`L227`）；实测 V2 产物 `scaler=None` → 输入原样进模型（L1）。
- **历史数据依赖**：无。推理只用单条记录当前值（L1）。
- **RAG / 数据库 / API 依赖**：无。特征只来自请求载荷本身（L1）。

### 5.5.2 未接入生产链路的特征工程：`FeatureExtractor`（重要事实）

`app/ml/feature_extractor.py:L21-L98` 定义了另一种特征提取（`packet_loss/latency/burstiness` + 派生特征 `renewal_interval_difference`/`renewal_interval_ratio` + 状态字段 + `train_id/signal_id`）。

**事实**：全仓库 grep 确认 `FeatureExtractor`/`FeatureVector` 在 production 代码中**零调用**（仅 `tests/test_attack_detector.py:L32-L122` 使用）。真实 ZL 链路走 `ZLFeatureAdapter`，且 ZL 特征集（Distance/PacketLoss/Latency）不含 `FeatureExtractor` 的派生特征。→ `FeatureExtractor` 属于"早期接口设计的遗留代码/死代码"（详见 16.1 问题 2）。

## 5.6 Preprocessing（推理前处理，完整清单）

按 `ZLAttackDetector.predict()`（`app/ml/zl_attack_detector.py:L216-L270`）的执行顺序：

1. 计时开始（`perf_counter`，`L217`）——用于 `inference_ms`；
2. 加载/复用模型产物（`L218`）；
3. 字段提取 + 缺失校验（`L219`，见 5.5.1）；
4. 构造 `np.float64` 的 `(1, n_features)` 矩阵（`L221-L225`）；
5. `scaler.transform`（若存在；当前 None 跳过）（`L227-L229`，失败→`AttackDetectorInferenceError`）；
6. 概率推理（`L230`，见 5.7）；
7. `argmax` → 原始标签 + 置信度（`L232-L237`）；
8. 标签映射（`Normal→UNKNOWN`、`ReplayAttack→Replay Attack` 等，`DEFAULT_ZL_LABEL_MAP` `L28-L33`；概率按映射后标签合并，`_map_probabilities` `L428-L433`）；
9. **置信度阈值**：`confidence < zl_confidence_threshold`（默认 0.0）→ `attack_type` 置为 `UNKNOWN`（`L244-L245`）。注意：仅改写 attack_type，**原始概率分布与 confidence 保留**（`L254-L270` 输出结构为证）。

## 5.7 Model Inference（推理）

**源码位置**：`app/ml/zl_attack_detector.py:L382-L423`（`_predict_probabilities`）

对 `classifier` 的能力三分支（L1）：
1. 有 `predict_proba`（V2 模型命中此分支）→ `predict_proba(transformed_x)[0]`（`L385-L392`）；
2. 否则有 `decision_function` → softmax：`logits - max → exp → 归一化`（`L393-L403`）；
3. 都没有 → `AttackDetectorInferenceError`（`L404-L407`）。

**输出契约校验**（`L409-L422`）：
- 概率向量必须 1 维；
- 长度必须等于 `target_mapping` 大小；
- 概率和必须 ≈ 1.0（容差 1e-6），非有限值报错。
任一违反 → `AttackDetectorInferenceError`（Router 层捕获后流程继续，模型预测缺位）。

### 5.7.5 真实推理实测（2026-08-14，本机环境 numpy 2.4.2 / sklearn 1.9.0）

输入样本（源自测试数据特征分布：`packet_loss=95.23, latency=354.46, distance=14.75`）：

```text
raw_label=DoS  attack_type=DoS  confidence=99.9999%
probabilities = {'UNKNOWN': 1.9e-07, 'DoS': 0.9999994, 'Jamming': 2.4e-07, 'Replay Attack': 1.3e-07}
model_version = zl-V2:v2_compact_top3_hist_gradient_boosting
inference_ms  = 5148.8（首次调用，含 pickle 加载 + sklearn import；产物缓存后后续调用显著更快）
```

同时实测：`packet_loss=0.02, latency=30, distance=50`（按 API 文档的小数语义）也输出 DoS 99.997% —— 佐证了 16.1 问题 6 的**量纲错配**：模型训练域是 STSRS 百分比/原始量纲，小数语义输入属于分布外数据。

## 5.8 Prediction Output

**输出模型**：`AttackPrediction`（`app/models/metrics.py:L150-L191`）

| 字段 | 说明 | 源码 |
|---|---|---|
| `attack_type` | 映射后标签（默认 UNKNOWN） | `metrics.py:L153-L156` |
| `confidence` | argmax 概率（0-1） | `L157-L162` |
| `probabilities` | 映射后每类概率（键如 `Replay Attack`） | `L163-L166` |
| `model_version` | `zl-{version}:{model_name}`（`app/ml/zl_attack_detector.py:L210-L214`） | `L167-L170` |
| `detector_backend` | `zl` / `rule` / `mock`（fallback 时会改写为实际后端，`app/ml/attack_detector.py:L116`） | `L171-L174` |
| `fallback_used` / `fallback_reason` | 是否走了回退及原因码 | `L175-L182` |
| `inference_ms` | 推理耗时（fallback 时为整体耗时，`app/ml/attack_detector.py:L119`） | `L183-L187` |
| `feature_vector` | **审计载荷**：`feature_columns/raw_input/raw_label/raw_probabilities/model_path`（`app/ml/zl_attack_detector.py:L263-L269`）；fallback 时追加 `primary_detector/primary_model_version/primary_error_reason/primary_error_message/fallback_detector`（`app/ml/attack_detector.py:L120-L130`） | `L188-L191` |

**输出去向**（L1 调用链）：
1. `IncidentRouter.route()` 中 `incident.attack_prediction = prediction.model_dump()`（`app/core/incident_router.py:L170`）——持久化到 Incident 并随 SSE 事件 `incident_triaged` 推送（`L178-L181`）；
2. TriageAgent 读取（`app/agents/triage_agent.py:L129-L131`）→ 进入 LLM Prompt（`L431-L446`）→ 驱动 CaseKB 查询（`L336-L348`）；
3. 最终进入 `GET /api/aiops/incidents/{id}` 详情的 `incident.attack_prediction` 字段（`app/api/aiops.py:L344` 输出整个 incident dump）。

## 5.9 "ModelService" 等价物：检测器工厂与三后端

本项目没有名为 `ModelService` 的类；模型服务化的等价结构是**接口 + 工厂 + 包装器**：

- **抽象接口** `AttackDetector.predict()`（`app/ml/attack_detector.py:L33-L63`）；
- **工厂** `create_attack_detector()`（`app/ml/attack_detector.py:L298-L344`）：按 `ml_attack_detector_backend` 配置组装；
- **三个后端**：
  - `ZLAttackDetector`（真实模型，`app/ml/zl_attack_detector.py:L184-L433`）；
  - `RuleBasedAttackDetector`（阈值规则，`app/ml/attack_detector.py:L195-L296`）：
    - DoS：`packet_loss>0.5 且 latency>200 且 burstiness>0.5` → conf 0.5；
    - Jamming：`packet_loss>0.5 且 signal_status∈RED/DANGER/OFFLINE` → conf 0.45；
    - Replay：`|train.renewal - cc.renewal| > 10` → conf 0.4；
    - 置信度上限 `confidence_cap=0.6`（规则不如监督学习可靠的显式表达，`L214-L219`）；
  - `MockAttackDetector`（恒 UNKNOWN，`L152-L188`）；
- **包装器** `FallbackAttackDetector`（`L90-L145`）：仅对 `AttackDetectorLoadError` 兜底；`InputError`/`InferenceError` 明确**不兜底**（`L132-L137`）——设计语义：输入与推理契约错误必须暴露，只有"模型不可用"才静默降级。

工厂逻辑（`app/ml/attack_detector.py:L304-L344`）：
```text
backend=mock → MockAttackDetector
backend=rule → RuleBasedAttackDetector
backend=zl   → ZLAttackDetector(project_root, version, path, manifest, threshold)
                 + fallback: none → 裸 primary；mock → Mock；rule → RuleBased（默认）
其他         → warn + MockAttackDetector
```

## 5.10 ML 与 AIOps 的关系

ML 是 AIOps 主流水线中的**检测节点**（介于分级与诊断之间）：

```
Normalize → Dedup → SeverityEngine ──→ AttackDetector.predict() ──→ TriageAgent
              （确定性规则）              （监督学习，新增节点）          （LLM）
```

**源码证据**：`app/core/incident_router.py:L164-L184`（"监督学习攻击检测（新）"注释块，调用 `self.attack_detector.predict(record_like)`）；前置条件是 `incident.metrics_snapshot` 非空（`L166`）——无指标的事件（如纯手工告警）**不经过 ML**，直接进 TriageAgent（L1）。

ML 不改变状态机、不改变动作执行层：检测结果只是 Incident 的一个字段（`app/models/incident.py:L181-L184`），状态迁移仍由 `_common_pipeline` 驱动。

## 5.11 ML 与 Agent 的关系

| 问题 | 源码答案 |
|---|---|
| 谁调用 ML？ | `IncidentRouter`（编排层），而非任何单个 Agent（`app/core/incident_router.py:L55-L66` 持有 detector；Agent 类中无 `predict` 调用，grep 确认） |
| 哪个 Agent 消费 ML 输出？ | 只有 TriageAgent（`app/agents/triage_agent.py:L129-L131`）；RunbookAgent 间接消费（经 TriageResult 的 attack_type，`app/agents/runbook_agent.py:L118`） |
| ML 输出影响什么？ | ① LLM 诊断 Prompt 的"模型预测"段落（`triage_agent.py:L431-L446`）；② CaseKB 查询词（`L336-L348`）；③ SSE 事件数据（`incident_router.py:L178-L181`）。**不影响动作执行**（动作由 RunbookPlan 决定，`incident_router.py:L269-L305`） |
| ML 会成为决策节点吗？ | 不会直接分叉控制流。唯一例外：confidence 阈值把 attack_type 改写为 UNKNOWN（`zl_attack_detector.py:L244-L245`），间接影响诊断输入，但流程走向不变（L1） |

## 5.12 ML 与 LLM 的关系（职责边界）

| | ML（AttackDetector） | LLM（TriageAgent/RunbookAgent/PRP） |
|---|---|---|
| 回答的问题 | "What happened?"（分类） | "Why? Impact? How to fix?"（解释/计划） |
| 输入 | 3 个数值特征 | 完整上下文（模型预测 + 指标异常分析 + KB） |
| 输出 | `AttackPrediction`（结构化概率） | `TriageResult` / `RunbookPlan`（结构化 Pydantic） |
| 确定性 | 确定性推理（同一输入同一输出） | 概率性生成 |
| 失败处理 | fallback→规则；错误→缺位继续 | 规则回退（`_fallback_triage`/`_fallback_plan`） |
| 协作方式 | **LLM 验证并解释 ML**：Prompt 明确要求"检查模型预测与指标证据是否一致，矛盾时给出独立判断"（`app/agents/triage_agent.py:L492-L497`、`L60-L62`） | — |

**最终诊断权在 LLM**：`TriageResult.attack_type` 是 LLM 综合判断的结果，随后被写回 `Incident.attack_type`（`app/core/incident_router.py:L234-L243`）——ML 的预测只是 LLM 的**强先验**，不是硬约束（L1）。

## 5.13 ML 与 RAG 的关系

见 4.8。一句话：ML 特征不依赖 RAG；ML 的预测结果驱动 RAG 的查询词（`app/agents/triage_agent.py:L336-L348`）。

## 5.14 ML 全链路总结（训练 → 推理 → 服务化）

```
[训练 — 仓库外 D:/STUDY/ZL]
STSRS 原始数据(699 万行, duckdb)
  → src/stsrs_data_engineering/baseline_training.py（v1 基线 HistGB）
  → v1_ablation.py（12 特征消融 → top3_only 胜出）
  → v2_compact_tree.py（晋升 V2：Distance/PacketLoss/Latency 3 特征）
  → 产物: models/baseline/v2_compact_top3_hist_gradient_boosting.pkl
          metadata/manifests/v2_compact_tree_manifest.json

[推理 — 本仓库]
Incident.metrics_snapshot
  → IncidentRouter._build_metric_record()              [app/core/incident_router.py:L512-L560]
  → ZLFeatureAdapter.to_raw_input()（字段映射/清洗）    [app/ml/zl_attack_detector.py:L74-L96]
  → ZLAttackDetector._load_artifact()（pickle 反序列化）[L272-L313]
  → predict(): numpy 矩阵 → predict_proba → argmax      [L216-L270]
  → AttackPrediction（含审计 feature_vector）

[服务化 — 本仓库]
AttackDetector 接口 → create_attack_detector() 工厂
  → FallbackAttackDetector（LoadError → 规则兜底）
  → 注入 IncidentRouter → 挂入 AIOps 主流水线
```

## 5.15 ML 与 Workflow / Agent / AIOps / RAG / LLM / MCP 的真实关系（总表）

| 关系 | 源码事实 | 证据 |
|---|---|---|
| ML → AIOps | ML 是 AIOps 主流水线的检测节点，位于 Severity 与 Triage 之间；仅当 `incident.metrics_snapshot` 非空时调用；输出写入 `Incident.attack_prediction`，不直接分叉状态机或动作执行 | `app/core/incident_router.py:L164-L184`、`app/models/incident.py:L181-L184` |
| ML → Workflow | ML 不直接调用 PRP/LangGraph 节点；恢复工作流的输入是 `FailureContext`，不是 `AttackPrediction`。当前源码不足以证明 ML 直接驱动 Workflow 分支；唯一间接影响是通过 Triage 输入改变诊断结果 | `app/services/aiops_service.py:L245-L266`、`app/models/incident.py:L519-L621` |
| ML → Agent | 调用方是 `IncidentRouter`（编排层）而非 Agent；消费方只有 `TriageAgent`，`RunbookAgent` 仅经 `TriageResult.attack_type` 间接受影响 | `app/core/incident_router.py:L55-L66/L164-L184`、`app/agents/triage_agent.py:L129-L131`、`app/agents/runbook_agent.py:L118` |
| ML ↔ RAG | 无直接调用关系；ML 特征只来自 `RailMetricRecord`，不来自知识库；`TriageAgent` 用 ML 预测类型查 CaseKB 是唯一间接路径 | `app/ml/zl_attack_detector.py:L74-L96`、`app/agents/triage_agent.py:L336-L348`、`app/tools/knowledge_tool.py:L13-L48` |
| ML ↔ LLM | Triage Prompt 明确要求 LLM“验证、解释和补充”ML 预测；最终诊断权在 LLM（`TriageResult.attack_type` 写回 Incident），ML 预测不是硬约束 | `app/agents/triage_agent.py:L60-L62/L492-L510`、`app/core/incident_router.py:L234-L243` |
| ML ↔ MCP | 无 import、无调用、无工具绑定；ML 模块不感知 MCP，MCP 只服务对话 Agent 与 PRP 恢复引擎。当前源码足以证明二者无直接关系 | `app/ml/*.py` 无 mcp import；`app/agent/mcp_client.py` 的 MCP 客户端/工具入口仅被 `rag_agent_service.py` 与 `app/agent/aiops/*` 使用（`app/api/chat.py:L13` 仅导入 `format_exception_chain` 做异常格式化，不消费 MCP 工具） |

> 事实等级：`ML → AIOps`、`ML → Agent`、`ML ↔ RAG`、`ML ↔ LLM`、`ML ↔ MCP` 均为 L1（源码直接证据）；`ML → Workflow` 的“无直接驱动”为 L1，但“ML 是否影响恢复策略”只能由调用链推导（L2），当前源码不足以证明 ML 输出进入恢复决策。

---

# 第六部分 Multi-Agent 源码导读

## 6.1 Agent 架构（重要事实辨析）

**当前项目实际有 9 个"Agent 角色"，但实现方式分两类：**

### A 类：AIOps 主流水线 5 个 Agent（非 LangGraph，是 LLM 结构化输出调用 + 编排）

| Agent | 文件:行 | 职责 | LLM | 结构化输出 |
|---|---|---|---|---|
| `TriageAgent` | `app/agents/triage_agent.py:L85-L179` | 验证/解释 ML 预测 + 综合诊断 | ChatQwen(rag_model, temperature=0) `L100-L104` | `TriageResult`（`L105` `with_structured_output`） |
| `RunbookAgent` | `app/agents/runbook_agent.py:L75-L378` | 生成处置计划（KB 三级检索） | 同上 `L86-L90` | `RunbookPlan`（`L91`） |
| `ActionOrchestrator` | `app/agents/action_orchestrator.py:L173-L429` | 执行 Mock 动作（含审批门） | **无 LLM** | — |
| `Verifier` | `app/agents/verifier.py:L25-L209` | 验证执行结果 | **无 LLM** | — |
| `Replanner` | `app/agents/replanner.py:L40-L152` | 5 态路由决策 | **无 LLM** | — |

> 辨析（L1）：主流水线的"Agent"没有工具调用循环、没有 LangGraph 节点。TriageAgent/RunbookAgent 是"Prompt + LLM.with_structured_output"的 LLM 包装类；ActionOrchestrator/Verifier/Replanner 是纯规则类（Replanner 的 `decide` 做枚举映射 + 状态迁移 + 审计，`L55-L111`）。把它们称为 Agent 是项目命名习惯，读者不应按"自主 Agent"理解。

### B 类：PRP 恢复引擎 3 个 LangGraph 节点（真·Workflow）

| 节点 | 文件:行 | 职责 |
|---|---|---|
| `planner` | `app/agent/aiops/planner.py:L63-L168` | LLM 生成计划（含 RAG 经验检索 + 工具清单） |
| `executor` | `app/agent/aiops/executor.py:L18-L113` | LLM bind_tools + `ToolNode` 执行单步 |
| `replanner` | `app/agent/aiops/replanner.py:L111-L340` | LLM 决策 continue/replan/respond |

### C 类：RAG 对话 Agent（LangGraph `create_agent`）

| Agent | 文件:行 | 说明 |
|---|---|---|
| `RagAgentService.agent` | `app/services/rag_agent_service.py:L146-L150` | `create_agent(model, tools=本地+MCP, checkpointer=MemorySaver)` |

## 6.2 Agent State

| State | 定义 | 持有者 | 生命周期 |
|---|---|---|---|
| `Incident` + `IncidentRecord` | `app/models/incident.py:L145-L197`、`L214-L230` | IncidentStore（内存 dict + Lock）`app/core/incident_store.py:L25-L30` | 进程级 |
| `IncidentState`（9 态） | `incident.py:L59-L69` | StateMachine 校验迁移 | 进程级 |
| `PlanExecuteState` | `app/agent/aiops/state.py:L14-L44` | PRP 图的 MemorySaver checkpoint | 进程级 |
| `AgentState`（messages） | `app/services/rag_agent_service.py:L36-L38` | 对话 agent 的 MemorySaver | 进程级 |
| `AttackPrediction` | `app/models/metrics.py:L150-L191` | 随 Incident 存储 | 进程级 |

- `past_steps` 用 `operator.add` 追加（`app/agent/aiops/state.py:L25`），LangGraph reducer 语义。
- **所有状态均为内存态**，服务重启即丢失（`app/core/incident_store.py:L17-L23` "内存实现"自述；`app/services/rag_agent_service.py:L16` 注释确认 MemorySaver 重启丢失）。

## 6.3 Agent Tool（各 Agent 实际可用的工具，L1）

| Agent | 工具 | 来源 |
|---|---|---|
| TriageAgent | 仅 `retrieve_knowledge`（Milvus） | `app/agents/triage_agent.py:L107-L111`（方法级懒加载） |
| RunbookAgent | 仅 `retrieve_knowledge` | `app/agents/runbook_agent.py:L93-L97` |
| ActionOrchestrator | 8 个 Mock 动作（`ALL_MOCK_ACTIONS`） | `app/agents/action_orchestrator.py:L27-L35`、`app/tools/mock_actions.py:L309-L318` |
| PRP planner/executor/replanner | 本地 3 工具（retrieve_knowledge/get_current_time/query_prometheus_alerts）+ MCP 工具 | `app/agent/aiops/planner.py:L103-L110` 等 |
| RagAgentService | 本地 3 工具 + MCP 工具 | `app/services/rag_agent_service.py:L104`、`L132-L144` |

> 事实：**主流水线 5 个 Agent 不使用 MCP 工具**。MCP 只服务于对话 Agent 和 PRP 恢复引擎（见第八部分）。

## 6.4 Agent 间协作

- **通信方式**：不共享 Memory、不互相调用；通过**数据契约**顺序传递——`TriageResult` → `RunbookPlan` → `List[MockActionResult]` → `VerificationResult` → `ReplanAction`（调用链见第十三部分）。
- **共享资源**：TriageAgent 与 RunbookAgent 共享同一个 Milvus 知识库（各自发查询，无会话共享）；全体共享 `IncidentRecord`（IncidentStore）与 `audit_store`。
- **控制权转移**：由 `IncidentRouter._common_pipeline` 顺序编排 + `Replanner` 决策循环实现（`app/core/incident_router.py:L204-L506`）。重试循环内重复调用 `execute_plan`/`verify`（`L353-L361`），补偿分支调用 `execute_compensation`/`verify_compensation`（`L377-L390`）。

## 6.5 Agent 与 Workflow

- 主流水线**不是 LangGraph Workflow**（无 StateGraph、无 checkpoint、无条件边），是 async 生成器管道（`app/core/incident_router.py:L107-L198`）。
- 唯一的 LangGraph StateGraph 是 PRP 恢复图（`app/services/aiops_service.py:L452-L488`，详见第九部分）。
- 对话 Agent 由 `langchain.agents.create_agent` 构建（内部是 LangGraph agent，`app/services/rag_agent_service.py:L146-L150`）。

---

# 第七部分 AIOps 源码导读（按概念逐项核对）

## 7.1 Event（事件模型）

- 核心对象 `Incident`（`app/models/incident.py:L145-L197`），来源枚举 `IncidentSource`（`L17-L21`：prometheus/mcp/stsrs/manual）。
- 归一化 `EventNormalizer.normalize()`（`app/events/event_normalizer.py:L66-L112`）按来源分发到 `_normalize_prometheus/_mcp/_stsrs/_manual`（`L83-L91`），统一生成 `trace_id`、`event_signature`、`dedup_key`（`L94-L99`）。
- **Metric-driven 原则（仅对 `normalize_metric()` 路径成立）**：`normalize_metric()` 不判攻击类型（attack_type=UNKNOWN）不分级（P4），见 `event_normalizer.py:L118-L197`。
- **例外（源码事实）**：`_normalize_prometheus` 会把基础设施告警名映射为 `CPU_HIGH/MEMORY_HIGH/DISK_HIGH/SERVICE_UNAVAILABLE/SLOW_RESPONSE/NETWORK_PARTITION`，并按 `severity` label 映射 `critical→P1 / warning→P3 / info→P4`（`event_normalizer.py:L203-L259`、`L543-L549`）；`_normalize_manual` 允许显式 attack_type（`L339-L377`）。

## 7.2 Detection（异常检测——两级）

1. **规则级分级**：`SeverityEngine.evaluate()`（`app/events/severity_engine.py:L58-L157`）：
   - P1 安全（信号状态 RED/DANGER/OFFLINE、联锁异常、速度>350 + 信号异常）`L163-L200`；
   - P2 通信（丢包>0.5、延迟>200ms、续期间隔>5000ms、来源冲突）`L202-L237`；
   - P3 性能（丢包>0.1、延迟>100ms、突发>0.5）`L239-L262`；
   - 动态升级：列车相关通信中断→P1（`L100-L108`）、≥3 指标异常→升一级（`L119-L127`）、重复>10 次→升一级（`L137-L142`）。
2. **ML 级检测**：`AttackDetector.predict()`（见第五部分）。

> 源码中**没有**独立命名的 "Detection/Prediction/Diagnosis" 类——这些概念被实现为：SeverityEngine（Detection-规则）、AttackDetector（Detection/Prediction-ML）、TriageAgent（Diagnosis-LLM）。

## 7.3 Prediction

即 5.7 的 ML 推理。语义为"攻击类型分类预测"，不是时序预测（L1）。

## 7.4 Diagnosis

`TriageAgent.triage()`（`app/agents/triage_agent.py:L113-L179`）：
1. `_analyze_metric_anomalies`（规则式异常模式提取，`L185-L297`：阈值与 SeverityEngine 一致但独立实现）；
2. `_query_topology`（`L303-L319`）+ `_query_casekb_by_prediction`（`L321-L397`：**模型预测优先**、异常模式关键词回退，关键词表 `L365-L376`）；
3. `_build_diagnosis_input`（`L403-L511`：事件信息 + 模型预测 + 异常分析 + KB 上下文 + 6 项诊断任务指令）；
4. LLM 结构化输出 `TriageResult`；失败走 `_fallback_triage`（`L517-L561`）。

## 7.5 Decision

- 计划决策：`RunbookAgent.generate_plan()`（`app/agents/runbook_agent.py:L99-L167`），KB 优先级 CaseKB→RunbookKB→TopologyKB（`L125-L136`），输出 `RunbookPlan`（含 `requires_approval`/`approval_actions`/`rollback_steps`）。
- 流程决策：`Replanner.decide()`（`app/agents/replanner.py:L55-L111`）5 态路由：
  ```
  SUCCESS→RESOLVE | RETRY→RETRY(>3次→ESCALATE) | COMPENSATE→COMPENSATE
  ESCALATE→ESCALATE | FAILED→FAIL
  ```

> **源码缺陷（2026-08-14 实测）**：`Replanner.decide()` 内部已执行 `state_machine.transition(record, verification.next_state)`（`replanner.py:L79-L111`），`_common_pipeline` 随后又对相同目标状态重复迁移（`incident_router.py:L331-L346/L368-L372/L437-L455`）。真实成功/补偿/升级/失败分支会抛出 `ValueError: X → X`（如 `VERIFIED → VERIFIED`）；只有 RETRY 分支因 decide 内先尝试非法的 `EXECUTING → EXECUTING` 被 catch 而不中断。现有 Router E2E 测试使用 `StubReplanner`（`tests/test_zl_attack_detector.py:L310-L316`），未覆盖该问题。详见 16.1-17。

## 7.6 Remediation

`ActionOrchestrator.execute_plan()`（`app/agents/action_orchestrator.py:L188-L276`）：
- 步骤解析 `_parse_action_name`（从 LLM 生成的中文步骤里找动作名，`L411-L429`）；
- 高风险动作（STOP_TRAIN/BLOCK_SECTION/EMERGENCY_SHUTDOWN）→ `ApprovalGate.create_request` → **Mock 模式自动批准**（`L216-L242`）；
- 执行经 `TimeoutManager.execute_with_timeout`（15s 超时、3 次重试、指数退避、熔断器，`L374-L379` → `app/events/timeout_manager.py:L112-L210`）；
- Mock 动作本体在 `app/tools/mock_actions.py`：8 个动作（`L309-L318`），随机故障注入（`_simulate_execution` `L124-L173`：FAILURE 60% / TIMEOUT 20% / EXCEPTION 20% 权重，`L43-L47`），各动作独立失败率配置（`ACTION_CONFIGS` `L67-L117`）。
- 补偿：`execute_compensation`（`L278-L344`）执行 plan.rollback_steps + 对已执行且有回滚的动作自动回滚（`ROLLBACK_MAP` `mock_actions.py:L299-L306`）。

## 7.7 Verification

`Verifier.verify()`（`app/agents/verifier.py:L39-L125`）：全成功→SUCCESS；含升级→ESCALATE；部分失败且 `retry_cycle < 2`→RETRY；重试耗尽→COMPENSATE（列出待补偿动作）。`verify_compensation`（`L127-L168`）判断补偿成败。每次结果写审计（`_make_result` `L170-L209`）。

## 7.8 Recovery / Escalation（两层兜底）

1. **PRP 恢复**：工作流失败 → `FailureContext`（`app/models/incident.py:L519-L621`，`to_planner_input()` 转文本）→ `AIOpsService._execute_recovery`（`app/services/aiops_service.py:L245-L318`）跑 PRP 图。`MAX_RECOVERY_ATTEMPTS=3` 常量存在（`app/models/incident.py:L639`），但 **`recovery_attempt` 从未递增**（初始化为 0，`aiops_service.py:L263`；replanner 只读不写，`replanner.py:L133-L155`），所以 replanner 与 `should_continue` 中的“超限”检查当前不可达。恢复成功后代码尝试把事件转 RESOLVED（`aiops_service.py:L151-L167`），但 FAILED/ESCALATED 是严格终态（`state_machine.py:L77-L92`），该迁移必然抛 `ValueError` 并被 `except ValueError: pass` 吞掉——**事件实际仍停留在 FAILED/ESCALATED**。
2. **Safety Control**：恢复也失败 → `_execute_safety_control`（`L320-L450`）：回滚 3 个动作（`L346-L350`）→ 尝试状态转 ESCALATED（`L399-L412`）→ 审计记录（`L416-L438`）。同样，从 FAILED 或 ESCALATED 再转 ESCALATED 不是合法迁移，异常被吞掉；`状态转 ESCALATED` 是源码意图，当前状态机下不生效。

---

# 第八部分 MCP 源码导读

## 8.1 MCP 架构

```
app（MCP 客户端，进程内）
  app/agent/mcp_client.py
    MultiServerMCPClient（langchain-mcp-adapters）─ retry_interceptor
        ├─ "cls"     → transport=sse, http://localhost:3000/sse   # 当前 .env 生效值
        │              （config 默认值为 streamable-http:8003/mcp，未生效）
        └─ "monitor" → transport=streamable-http, http://localhost:8004/mcp

mcp_servers/（MCP 服务端，独立进程）
  cls_server.py    FastMCP("CLS")     → streamable-http 8003 /mcp
  monitor_server.py FastMCP("Monitor") → streamable-http 8004 /mcp
```

- 配置源：`app/config.py:L45-L50` 默认值 + `mcp_servers` property（`L76-L88`）；`.env` 覆盖（`.env:L26-L33`）。**当前 `.env` 生效值**：CLS=`sse` + `http://localhost:3000/sse`，Monitor=`streamable-http` + `http://localhost:8004/mcp`（实测 `config.mcp_servers` 输出一致）。
- 启动方式：Makefile `start-cls`/`start-monitor`（`Makefile:L193-L230`）nohup 后台进程。
> 注意：`start-cls` 启动的 FastMCP server 监听 8003，但当前应用客户端配置连接 3000 的 SSE 端点，二者并不对应；CLS 的“配置默认值”与“当前生效值”需分开阅读。

## 8.2 MCP Client

**源码位置**：`app/agent/mcp_client.py`

- 全局单例 `_mcp_client`（`L17`），`get_mcp_client()`（`L112-L155`）懒初始化，`force_new=True` 可旁路单例。
- `get_mcp_client_with_retry()`（`L158-L186`）在拦截器列表最前面插入 `retry_interceptor`。
- `retry_interceptor`（`L46-L102`）：最多 3 次、指数退避 `delay * 2**attempt`（1s/2s/4s）；全部失败**返回** `CallToolResult(isError=True, text=错误信息)` 而非抛异常——保证 Agent 工具调用不因 MCP 故障崩溃。
- `load_mcp_tools_safe`（`L35-L43`）：`client.get_tools()` 失败 → 空列表 + 可读错误串（ExceptionGroup 展开 `format_exception_chain` `L20-L32`）。
- `suggest_mcp_transport`（`L214-L230`）：URL 与 transport 明显不匹配时仅警告不改配置。

## 8.3 MCP Server

| Server | 文件 | 工具数（源码实际） | 运行方式 |
|---|---|---|---|
| CLS（腾讯云日志模拟） | `mcp_servers/cls_server.py` | 5 | `mcp.run(transport="streamable-http", port=8003, path="/mcp")` `L470` |
| Monitor（监控模拟） | `mcp_servers/monitor_server.py` | 2 | `mcp.run(..., port=8004, path="/mcp")` `L435` |

## 8.4 MCP Tools（源码实际清单）

**CLS（`cls_server.py`）**：`get_current_timestamp`(L104)、`get_region_code_by_name`(L136)、`get_topic_info_by_name`(L169)、`search_topic_by_service_name`(L212)、`search_log`(L346)。全部为**硬编码模拟数据**（如 `region_mapping` 只含北上广、`mock_topics` 只含 topic-001、`search_log` 只对 topic-001 生成 INFO 日志）。

**Monitor（`monitor_server.py`）**：`query_cpu_metrics`(L124)、`query_memory_metrics`(L277)。基于时间范围**动态生成**渐增曲线 + 随机波动（`L206-L237` 等）。

> **事实（与 README 不一致）**：`mcp_servers/README.md` 声称 CLS 有 `search_service_logs`/`analyze_log_pattern`、Monitor 有 `query_process_list`/`search_historical_tickets`/`get_service_info`/`list_all_services` —— **源码中均不存在**（README 过时，见 16.1 问题 7）。

## 8.5 Agent → MCP 调用链（真实）

**MCP 客户端/工具消费者只有两处**（grep 确认；`app/api/chat.py:L13` 仅导入 `format_exception_chain` 做异常格式化，不消费 MCP 工具）：
1. **对话 Agent**：`RagAgentService._initialize_agent()` → `get_mcp_client_with_retry()` → `load_mcp_tools_safe()` → 合并进 `create_agent(tools=...)`（`app/services/rag_agent_service.py:L132-L150`）。
2. **PRP 恢复引擎**：planner/executor/replanner 各自 `get_mcp_client_with_retry() + get_tools()`（`app/agent/aiops/planner.py:L106-L107`、`executor.py:L42-L43`、`replanner.py:L174-L175`）。

```
对话 Agent / PRP 节点
  → llm.bind_tools(all_tools) / create_agent(tools)
  → LLM 决定调用 MCP 工具
  → MultiServerMCPClient（langchain-mcp-adapters）
  → retry_interceptor（3 次指数退避）
  → 按当前 .env：CLS=sse(localhost:3000)、Monitor=streamable-http(localhost:8004)
    （MCP server 自身启动端口为 8003/8004）
  → 模拟数据函数 → Dict 结果
  → 工具结果回填 LLM 上下文
```

**主流水线（IncidentRouter → Triage/Runbook/Orchestrator）不调用 MCP**（L1：`app/core/incident_router.py` 与 `app/agents/*` 无 mcp import；`app/agents/triage_agent.py:L17-L24` import 列表为证）。

---

# 第九部分 Workflow 源码导读

## 9.1 Workflow State

`PlanExecuteState`（`app/agent/aiops/state.py:L14-L44`）：

```python
class PlanExecuteState(TypedDict, total=False):
    input: str                                      # 任务描述（恢复模式=FailureContext.to_planner_input()）
    plan: List[str]                                 # 待执行步骤
    past_steps: Annotated[List[tuple], operator.add]  # 已完成步骤（追加式）
    response: str                                   # 最终报告
    is_recovery: bool                               # 是否恢复模式
    recovery_attempt: int                           # 恢复尝试次数（当前代码从未递增，见 9.4）
    failure_context: Optional[Dict[str, Any]]       # 失败上下文
    recovery_result: str                            # recovery_success/failed/...
```

## 9.2 Nodes

| 节点 | 函数 | 输入 → 输出（state 更新） |
|---|---|---|
| planner | `app/agent/aiops/planner.py:L63-L168` | input → `{"plan": [步骤...]}`；异常时默认 3 步计划（`L159-L168`） |
| executor | `app/agent/aiops/executor.py:L18-L113` | plan[0] → `{"plan": plan[1:], "past_steps": [(task, result)]}`；异常时同样弹出步骤并记录失败（`L108-L113`） |
| replanner | `app/agent/aiops/replanner.py:L111-L340` | 决策 `respond/replan/continue`；`respond` 生成 `response`；恢复模式标记 `recovery_result`（`L233-L236`、`L273-L275`） |

executor 内部流程（`executor.py:L55-L97`）：LLM `bind_tools(local+mcp)` → `ainvoke` → 若有 `tool_calls` → `ToolNode(all_tools).ainvoke` → 结果回填再 `ainvoke` 得最终文本。

## 9.3 Edges

`app/services/aiops_service.py:L456-L462`：
```python
workflow.add_node("planner", planner)
workflow.add_node("executor", executor)
workflow.add_node("replanner", replanner)
workflow.set_entry_point("planner")
workflow.add_edge("planner", "executor")
workflow.add_edge("executor", "replanner")
```

## 9.4 Conditional Routing

`app/services/aiops_service.py:L464-L486`：

```python
def should_continue(state):
    if state.get("response"):      return END
    if state.get("plan", []):      return "executor"
    if is_recovery and recovery_attempt >= MAX_RECOVERY_ATTEMPTS: return END
    return END

workflow.add_conditional_edges("replanner", should_continue,
                               {"executor": "executor", END: END})
```

**读法**：replanner 之后，若仍有 plan 且未结束 → 回 executor 循环；否则 END。replanner 内部还有两道硬闸：`past_steps>=8` 强制响应（`replanner.py:L157-L166`）、`past_steps>=5` 禁止 replan（`L247-L250`）——防无限循环。

> **事实修正**：`recovery_attempt` 在 `_execute_recovery` 中初始化为 0（`app/services/aiops_service.py:L263`），replanner 节点只读取、从未递增（`app/agent/aiops/replanner.py:L133-L155`；`state.py:L37` 注释与代码不符）。因此 `should_continue` 与 replanner 中 `recovery_attempt >= MAX_RECOVERY_ATTEMPTS` 的“超限”分支当前不可达。

## 9.5 Workflow Execution

`_execute_recovery`（`app/services/aiops_service.py:L245-L318`）：
- `recovery_graph.astream(input=initial_state, config={"configurable": {"thread_id": session_id}}, stream_mode="updates")`（`L270-L274`）；
- 按节点名格式化事件（`_format_planner_event/_format_executor_event/_format_replanner_event`，`L498-L574`）；
- 结束后 `recovery_graph.get_state(config_dict)` 取最终 state，从 `values` 读 `response`/`recovery_result`（`L289-L297`）；
- checkpoint：`MemorySaver`（`L47`）。

## 9.6 关键事实：主流水线不是 Workflow

重复强调（L1）：AIOps 主链路（Normalize→…→Replanner）**没有使用 LangGraph**。`StateGraph` 只在 PRP 恢复图中出现（全仓库 `StateGraph` 仅 `app/services/aiops_service.py:L456` 一处）。因此：
- 主流水线无 checkpoint/中断/恢复能力；
- 主流水线的"状态"由 `IncidentRecord + StateMachine` 自己管理（`app/core/state_machine.py`）。

---

# 第十部分 Event-driven 源码导读

## 10.1 Event Model

- SSE 事件类型枚举 `SSEEventType`（20 种，`app/models/incident.py:L89-L118`）；
- 事件载荷 `SSEPayload`（`L437-L460`，`to_sse_dict()` 序列化）；
- 审计条目 `AuditEntry`（`L412-L430`，含 `event_sequence` 全局递增序号）。

## 10.2 Event Producer（谁产生事件）

1. **AuditStore.record()**（`app/core/audit_store.py:L47-L126`）：序号自增 → 追加审计日志 → 构建 SSEPayload → `loop.call_soon_threadsafe(... _emit_sse ...)`（`L112-L119`，无事件循环时静默跳过）。被三处接线：
   - `StateMachine.transition` 的审计回调（`app/core/audit_store.py:L259-L266` 模块加载时自动接线）；
   - 各 Agent/编排器显式 `audit_store.record(...)`（如 `app/agents/verifier.py:L192-L207`）；
   - `ApprovalGate`/`ActionOrchestrator`（`app/agents/action_orchestrator.py:L87-L96` 等）。
2. **IncidentRouter 的 SSE 字典**：`_sse()`（`app/core/incident_router.py:L566-L579`）构建 `{type, trace_id, incident_id, thread_id, message, data}` 并由 `route()` 直接 `yield` 给 API 层（SSE 流）。

## 10.3 Event Consumer

- `AuditStore.subscribe(thread_id)`（`app/core/audit_store.py:L142-L155`）返回 `asyncio.Queue`；
- `subscribe_generator`（`L166-L187`）无限循环消费队列 → 供 FastAPI `EventSourceResponse` 使用；
- API：`GET /api/aiops/sse/{thread_id}`（`app/api/aiops.py:L263-L285`）→ `aiops_service.subscribe_sse`（`app/services/aiops_service.py:L490-L496`）。
- 前端通过 `fetch` + `response.body.getReader()` 读 AIOps POST 的 SSE 流（`static/app.js:L1181` 附近）。

## 10.4 Async Processing

- 主流水线：async 生成器（`AsyncGenerator[Dict, None]`）贯穿 API→Service→Router；
- 超时：`asyncio.wait_for`（`app/events/timeout_manager.py:L157-L160`）；
- 同步 Mock 动作转异步：`asyncio.get_running_loop().run_in_executor(None, fn)`（`app/agents/action_orchestrator.py:L405-L409`）；
- SSE 跨线程投递：`call_soon_threadsafe`（`app/core/audit_store.py:L114`）。

## 10.5 Streaming

三处 SSE：
1. `POST /api/chat_stream`（`app/api/chat.py:L71-L176`）：token 级流式（`stream_mode="messages"`，`app/services/rag_agent_service.py:L292-L312`）；
2. `POST /api/aiops/*`：流程事件流（`app/api/aiops.py:L99-L123`）；
3. `GET /api/aiops/sse/{thread_id}`：审计事件订阅。

## 10.6 判定结论（事实）

- **没有**消息队列 / 消息代理 / Pub-Sub / Redis / Kafka（依赖与源码均无）；
- **有**：进程内 asyncio.Queue 订阅分发、SSE 流式输出、滑动窗口去重、状态机迁移事件、审计回放（`replay_timeline`，`app/core/audit_store.py:L217-L237`）。
- 结论：项目是**进程内事件驱动 + SSE 推送**架构，不是分布式事件驱动系统（L1）。

---

# 第十一部分 基础技术详解（服务于源码理解）

## 11.1 FastAPI + sse-starlette

- 应用构建：`app/main.py:L45-L50`；CORS 全开放（`L53-L59`，`allow_origins=["*"]` + `allow_credentials=True`，安全问题见 16.1-11）。
- 所有流式端点用 `sse_starlette.EventSourceResponse`（`app/api/chat.py:L176`、`app/api/aiops.py:L123` 等）；数据体为 `json.dumps(...)` 字符串。
- 文件上传 `UploadFile`（`app/api/file.py:L26`），大小/类型校验见 4.1。

## 11.2 Pydantic v2

- 数据模型全部继承 `pydantic.BaseModel`（`app/models/*`）。
- **事实**：模型内用的是 **v1 风格 `class Config: json_encoders`**（如 `app/models/metrics.py:L26-L29`），pytest 运行产生 `PydanticDeprecatedSince20` 弃用警告（实测 31 passed + 大量警告，见 16.1-8）。
- 配置用 `pydantic_settings.BaseSettings`（`app/config.py:L7-L18`），`env_file=".env"`、大小写不敏感、`extra="ignore"`。

## 11.3 LangChain

- `ChatPromptTemplate.from_messages`（system + placeholder）：共 5 个 Prompt——Triage/Runbook/Planner/Replanner 决策/Response 生成（`triage_agent.py:L28-L82`、`runbook_agent.py:L36-L72`、`planner.py:L28-L60`、`replanner.py:L41-L89`、`replanner.py:L92-L108`）。
- `with_structured_output(PydanticModel)` 强制结构化输出（5 处：TriageResult/RunbookPlan/Plan/Act/Response，分别在 `triage_agent.py:L105`、`runbook_agent.py:L91`、`planner.py:L137`、`replanner.py:L204`、`replanner.py:L291`）。
- `@tool` 装饰器：3 个本地工具（`app/tools/knowledge_tool.py:L13`、`time_tool.py:L10`、`query_metrics_alerts.py:L158`）；`retrieve_knowledge` 使用 `response_format="content_and_artifact"`（`knowledge_tool.py:L13`）。
- `langchain.agents.create_agent`：对话 Agent（`app/services/rag_agent_service.py:L146-L150`）。

## 11.4 LangGraph

- `StateGraph(PlanExecuteState)`：PRP 恢复图（`app/services/aiops_service.py:L456`）。
- `ToolNode`：executor 的工具执行（`app/agent/aiops/executor.py:L9`、`L58`、`L88`）。
- `MemorySaver`：对话会话与恢复图 checkpoint（`app/services/rag_agent_service.py:L110`、`app/services/aiops_service.py:L47`）。
- `add_messages`/`operator.add` reducer：消息追加与 past_steps 追加（`app/services/rag_agent_service.py:L38`、`app/agent/aiops/state.py:L25`）。
- 条件边：`add_conditional_edges`（`app/services/aiops_service.py:L482-L486`）。

## 11.5 MCP（协议层）

- 客户端库 `langchain-mcp-adapters`：`MultiServerMCPClient`、拦截器 `MCPToolCallRequest`、结果类型 `mcp.types.CallToolResult/TextContent`（`app/agent/mcp_client.py:L9-L12`）。
- 服务端框架 `fastmcp.FastMCP`：`@mcp.tool()` 装饰器 + `mcp.run(transport="streamable-http", ...)`（`mcp_servers/*.py`）。

## 11.6 Milvus（pymilvus + langchain-milvus）

- 底层 ORM：`connections.connect(alias="default", ...)` + `Collection`/`utility`（`app/core/milvus_client.py:L80-L100`）。
- 上层封装：`langchain_milvus.Milvus`（`app/services/vector_store_manager.py:L42-L52`）。
- 兼容补丁 monkey-patch MilvusClient `_using` 别名（`app/core/milvus_client.py:L18-L41`）——langchain_milvus 内部别名与 ORM 不一致的规避。

## 11.7 LLM（实际使用的模型与调用方式）

- **实际 LLM 统一为 `langchain_qwq.ChatQwen`（qwen-max）**：Triage（`app/agents/triage_agent.py:L100-L104`）、Runbook（`app/agents/runbook_agent.py:L86-L90`）、Planner/Executor/Replanner（`app/agent/aiops/planner.py:L131-L135` 等）、对话 Agent（`app/services/rag_agent_service.py:L96-L101`）。
- **`LLMFactory`（`app/core/llm_factory.py`）是死代码**：grep 确认全仓无调用（仅在 `app/core/__init__.py:L26` 导出），且它构建的 `ChatOpenAI` 与生产路径的 `ChatQwen` 是两套封装（见 16.1-1）。
- 温度：诊断/计划类 0，对话 0.7（上述位置）。

## 11.8 ML 框架

- 推理运行时：**scikit-learn**（`HistGradientBoostingClassifier.predict_proba`）+ **numpy**（输入矩阵与 argmax）。加载方式是 pickle 反序列化（`app/ml/zl_attack_detector.py:L279`）。
- 训练端（仓库外）：sklearn + duckdb（`D:/STUDY/ZL/src/stsrs_data_engineering/`）。

## 11.9 其他

- **Loguru**：控制台 + 按天轮转文件、7 天保留、zip 压缩、异步入队（`app/utils/logger.py:L11-L46`，模块导入即执行 `setup_logger()` `L46`）。
- **httpx**：Prometheus 告警工具（`app/tools/query_metrics_alerts.py:L74-L82`）。
- **aiofiles/python-multipart**：pyproject 声明；upload 端点实际用 `await file.read()`（`app/api/file.py:L66`）。

---

# 第十二部分 整个项目的数据流

```
【对话链路】                                    【AIOps 链路】
POST /api/chat(_stream)                         POST /api/aiops/metrics（典型）
   │                                              │ app/api/aiops.py:L186
   ▼                                              ▼
RagAgentService.query(_stream)                  AIOpsService.process_metrics
   │ rag_agent_service.py:L189/L254                │ aiops_service.py:L206-L243（包装 raw_event）
   ▼                                              ▼
create_agent 图（LangGraph）                    IncidentRouter.route
   ├─ LLM(ChatQwen) 决策                          │ incident_router.py:L107
   ├─ retrieve_knowledge → Milvus biz             ▼
   │     ↑ 1024 维向量，top_k=3                 EventNormalizer.normalize → Incident(UNKNOWN)
   ├─ get_current_time                            │ event_normalizer.py:L66
   ├─ query_prometheus_alerts → Prometheus        ▼
   └─ MCP cls/monitor 工具                        Deduplicator.process（实时单条指标旁路）
   ▼                                                │ incident_router.py:L125-L141
SSE token 流 / 完整答案                            ▼
                                                 SeverityEngine.evaluate（P1-P4 规则）
                                                   │ severity_engine.py:L58
                                                   ▼
                                                 IncidentRouter._build_metric_record
                                                   │ incident_router.py:L512（snapshot→RailMetricRecord）
                                                   ▼
                                      ┌─ metrics_snapshot 为空：跳过
                                      ▼
                          AttackDetector.predict            ★ ML
                            ├ ZLAttackDetector（pickle + predict_proba）
                            ├ FallbackAttackDetector（LoadError→RuleBased）
                            └ MockAttackDetector（backend=mock）
                            → AttackPrediction → incident.attack_prediction
                                                   │
                                                   ▼
                                          TriageAgent.triage
                                            ├ KB: TopologyKB + CaseKB（预测类型优先）
                                            ├ LLM 结构化输出 → TriageResult
                                            └ 失败 → _fallback_triage
                                            → incident.attack_type 更新
                                                   │
                                                   ▼
                                          RunbookAgent.generate_plan
                                            ├ KB: CaseKB→RunbookKB→TopologyKB
                                            └ → RunbookPlan（steps/审批/回滚）
                                                   │
                                                   ▼
                                          ActionOrchestrator.execute_plan
                                            ├ ApprovalGate（Mock 自动批准）
                                            ├ TimeoutManager（15s/3重试/退避/熔断）
                                            └ Mock 动作（20%± 故障注入）
                                                   │
                                                   ▼
                                          Verifier.verify（SUCCESS/RETRY/COMPENSATE/ESCALATE）
                                                   │
                                                   ▼
                                          Replanner.decide（≤3 轮循环）
                                             ├ RESOLVE → VERIFIED → RESOLVED
                                             ├ RETRY → 回到 execute_plan
                                             ├ COMPENSATE → execute_compensation → verify_compensation
                                             ├ ESCALATE → ESCALATED（终态）
                                             └ FAIL → FAILED（终态）
                                                   │
                                          ┌────────┴────────────────┐
                                          ▼（FAILED/ESCALATED）     ▼（RESOLVED）
                                   FailureContext → PRP 恢复图      complete 事件
                                   （planner→executor→replanner）      │
                                          │ 失败                     ▼
                                          ▼                        SSE + 审计 + IncidentStore
                                   Safety Control（回滚+ESCALATED）
```

> **图示前提修正**：图中 `Replanner.decide → RESOLVED / FAILED / ESCALATED` 分支在真实 Replanner 下会因二次状态迁移抛异常（见 7.5、16.1-17）；`Safety Control（回滚+ESCALATED）` 也无法把严格终态迁移到 ESCALATED（见 16.1-18）。当前只有 `StubReplanner` 测试能走到 RESOLVED。

**ML 数据流子图（贯穿位置标注）**：

```
raw metrics → _build_metric_record → ZLFeatureAdapter(字段映射/清洗)
→ np.float64 矩阵 → predict_proba → argmax → label map → threshold
→ AttackPrediction → incident.attack_prediction → TriageAgent prompt/KB → TriageResult
```

---

# 第十三部分 整个项目源码调用链（函数级）

## 13.1 AIOps 主链路（caller → callee，全部 L1 验证）

| # | Caller | Callee |
|---|---|---|
| 1 | `app/api/aiops.py:L186-L256` `process_metrics_stream` | `app/services/aiops_service.py:L206-L243` `AIOpsService.process_metrics` |
| 2 | `aiops_service.py:L238-L242` | `aiops_service.py:L55-L188` `process_incident` |
| 3 | `aiops_service.py:L78` | `app/core/incident_router.py:L107-L198` `IncidentRouter.route` |
| 4 | `incident_router.py:L121` | `app/events/event_normalizer.py:L66-L112` `EventNormalizer.normalize` |
| 5 | `incident_router.py:L131` | `app/events/deduplicator.py:L52-L106` `Deduplicator.process` |
| 6 | `incident_router.py:L160` | `app/events/severity_engine.py:L58-L157` `SeverityEngine.evaluate` |
| 7 | `incident_router.py:L168` | `incident_router.py:L512-L560` `_build_metric_record`（静态） |
| 8 | `incident_router.py:L169` | `app/ml/attack_detector.py:L101-L131` `FallbackAttackDetector.predict`（默认配置） |
| 8a | `attack_detector.py:L104` | `app/ml/zl_attack_detector.py:L216-L270` `ZLAttackDetector.predict` |
| 8b | `zl_attack_detector.py:L218` | `zl_attack_detector.py:L272-L313` `_load_artifact` |
| 8c | `zl_attack_detector.py:L219` | `zl_attack_detector.py:L74-L96` `ZLFeatureAdapter.to_raw_input` |
| 8d | `zl_attack_detector.py:L230` | `zl_attack_detector.py:L382-L423` `_predict_probabilities` |
| 8e | `attack_detector.py:L115`（LoadError 时） | `attack_detector.py:L225-L273` `RuleBasedAttackDetector.predict` |
| 9 | `incident_router.py:L187-L192` | `app/core/incident_store.py:L36-L62` `IncidentStore.create` + `app/core/state_machine.py:L121-L201` `StateMachine.transition` |
| 10 | `incident_router.py:L197` | `incident_router.py:L204-L506` `_common_pipeline` |
| 11 | `incident_router.py:L228` | `app/agents/triage_agent.py:L113-L179` `TriageAgent.triage` |
| 11a | `triage_agent.py:L314`/`L342`/`L390` | `app/tools/knowledge_tool.py:L13-L48` `retrieve_knowledge` |
| 11b | `knowledge_tool.py:L29-L34` | `app/services/vector_store_manager.py:L123-L130` `get_vector_store` + `as_retriever` |
| 11c | `triage_agent.py:L159` | LangChain `chain.ainvoke`（`TRIAGE_PROMPT \| llm.with_structured_output(TriageResult)`，`L105`） |
| 11d | 失败时 `triage_agent.py:L179` | `triage_agent.py:L517-L561` `_fallback_triage` |
| 12 | `incident_router.py:L234-L243` | 更新 `incident.attack_type`（`app/models/incident.py:L237-L274` TriageResult 契约） |
| 13 | `incident_router.py:L269` | `app/agents/runbook_agent.py:L99-L167` `RunbookAgent.generate_plan`（内部 KB 三级检索 `L173-L258` + LLM `L146`） |
| 14 | `incident_router.py:L305` | `app/agents/action_orchestrator.py:L188-L276` `ActionOrchestrator.execute_plan` |
| 14a | `action_orchestrator.py:L245` | `action_orchestrator.py:L346-L403` `_execute_single_action` |
| 14b | `action_orchestrator.py:L375` | `app/events/timeout_manager.py:L112-L210` `TimeoutManager.execute_with_timeout` |
| 14c | `action_orchestrator.py:L365` | `app/tools/mock_actions.py:L328-L330` `get_action`（→ `_simulate_execution` `L124-L173`） |
| 15 | `incident_router.py:L315` | `app/agents/verifier.py:L39-L125` `Verifier.verify` |
| 16 | `incident_router.py:L327` | `app/agents/replanner.py:L55-L111` `Replanner.decide`（枚举映射 `L113-L136` + 状态迁移/审计 `L79-L111`；真实链路存在二次迁移缺陷，见 16.1-17） |
| 17 | 补偿分支 `incident_router.py:L378`/`L388` | `execute_compensation`（`action_orchestrator.py:L278-L344`）+ `verify_compensation`（`verifier.py:L127-L168`） |
| 18 | 终态后 `incident_router.py:L490-L506` | 输出 `complete` 事件（含 `FailureContext`，`incident.py:L519-L621`） |
| 19 | `aiops_service.py:L111-L149`（失败时） | `_execute_recovery`（`aiops_service.py:L245-L318`）→ PRP 图（`L270` `astream`）→ `_execute_safety_control`（`L320-L450`） |

## 13.2 RAG 对话链路

| # | Caller | Callee |
|---|---|---|
| 1 | `app/api/chat.py:L41`/`L99` | `rag_agent_service.query`/`query_stream` |
| 2 | `rag_agent_service.py:L205`/`L272` | `_initialize_agent`（`L118-L152`） |
| 3 | `rag_agent_service.py:L132-L133` | `app/agent/mcp_client.py:L158-L186` `get_mcp_client_with_retry` + `L35-L43` `load_mcp_tools_safe` |
| 4 | `rag_agent_service.py:L146` | `langchain.agents.create_agent` |
| 5 | `rag_agent_service.py:L224`/`L292` | `agent.ainvoke`/`agent.astream(stream_mode="messages")` |
| 6 | `rag_agent_service.py:L339` | `checkpointer.get(config)`（会话历史） |

## 13.3 RAG 写入链路

| # | Caller | Callee |
|---|---|---|
| 1 | `app/api/file.py:L79` | `vector_index_service.index_single_file` |
| 2 | `vector_index_service.py:L155` | `vector_store_manager.delete_by_source` |
| 3 | `vector_index_service.py:L158` | `document_splitter_service.split_document` |
| 4 | `vector_index_service.py:L163` | `vector_store_manager.add_documents`（→ langchain_milvus `add_documents` → `DashScopeEmbeddings.embed_documents` → Milvus） |

---

# 第十四部分 一个请求如何跑完（源码级全链路）

## 14.1 典型请求：`POST /api/aiops/metrics`（ML 检测主入口）

**请求体**（SSE 流式响应）：

```json
{
  "train_id": "7Y36",
  "signal_id": "YT919",
  "timestamp": "2025-08-14T09:08:15",
  "metrics": {
    "distance": 14.75, "packet_loss": 95.23, "latency": 354.46,
    "burstiness": 4.8, "signal_status": "Green", "overlap_status": "No"
  },
  "source_metrics": {
    "control_center": {"renewal_interval": 0.0},
    "train": {"renewal_interval": 170.0}
  }
}
```

```
① HTTP 进入           app/api/aiops.py:L186-L256  process_metrics_stream()
   输入: JSON payload + 可选 ?session_id        输出: EventSourceResponse
   → 下一步: aiops_service.process_metrics()

② Service 包装        app/services/aiops_service.py:L206-L243  process_metrics()
   把 payload 包成 {"metrics_snapshot": payload, "train_id":..., "signal_id":...}
   → 下一步: process_incident(raw_event, source=STSRS, session_id)

③ Service 编排        app/services/aiops_service.py:L55-L188  process_incident()
   thread_id = "thread-{session_id}"；循环消费 router.route() 的 yield
   → 下一步: IncidentRouter.route()

④ 路由总入口          app/core/incident_router.py:L107-L198  route()
   a) EventNormalizer.normalize(raw_event, STSRS)          [L121]
      - _normalize_stsrs 检测到 metrics_snapshot → normalize_metric()
        [event_normalizer.py:L291-L305 → L118-L197]
      - Incident(attack_type=UNKNOWN, severity=P4, metrics_snapshot=增强快照)
      - 生成 event_signature / dedup_key                    [L94-L99]
      → yield SSE "incident_created"                        [L122]
   b) _should_process_single_metric → True（metrics 为 dict）→ 旁路缓冲去重 [L125-L129]
   c) SeverityEngine.evaluate(incident)                     [L160]
      - 丢包 95.23>0.5 → 通信风险 P2；有 train_id → 升级 P1  [severity_engine.py:L100-L108]
      → yield SSE "state_changed"                           [L161-L162]

⑤ ★ ML 检测节点 ★     app/core/incident_router.py:L164-L184
   a) _build_metric_record(incident) → RailMetricRecord     [L168 → L512-L560]
      输入: incident.metrics_snapshot    输出: RailMetricRecord(train_id="7Y36", ...)
   b) attack_detector.predict(record_like)                  [L169]
      - 默认配置 → FallbackAttackDetector[ZLAttackDetector, RuleBasedAttackDetector]
        (create_attack_detector, app/ml/attack_detector.py:L298-L344)
      - ZLAttackDetector.predict()                           [zl_attack_detector.py:L216-L270]
          加载产物(首次~秒级) → ZLFeatureAdapter 提取 3 特征
          → predict_proba → argmax → label map → 置信度阈值
      - 实测输出: AttackPrediction(attack_type="DoS", confidence≈1.0,
          probabilities={"UNKNOWN":~2e-7,"DoS":≈1.0,"Jamming":~2e-7,"Replay Attack":~1e-7},
          model_version="zl-V2:v2_compact_top3_hist_gradient_boosting",
          detector_backend="zl", inference_ms≈?)
   c) incident.attack_prediction = prediction.model_dump()   [L170]
      → yield SSE "incident_triaged"(data.attack_prediction) [L178-L181]
   ※ 若模型文件缺失: Fallback → RuleBasedAttackDetector → DoS(conf 0.5)
      fallback_used=True, feature_vector 记录 primary_error_reason
   ※ 若 predict 抛 InputError/InferenceError: except → warn → 继续
      [L182-L184]，TriageAgent 仍会独立诊断

⑥ 建账 + 状态         incident_store.create + state_machine.transition(NEW) [L187-L193]

⑦ 统一流水线          _common_pipeline(incident, thread_id, record) [L197 → L204-L506]
   Step A NEW→TRIAGED                                        [L219-L226]
      TriageAgent.triage(incident)                           [L228]
        - 读 incident.attack_prediction                       [triage_agent.py:L129-L131]
        - _analyze_metric_anomalies → 异常模式列表            [L142 → L185-L297]
        - _query_topology → retrieve_knowledge(Milvus top3)   [L145 → L303-L319]
        - _query_casekb_by_prediction: 预测 "DoS" → 
            query="历史案例 铁路信号安全 DoS 攻击 诊断 处置 ..." [L336-L348]
        - _build_diagnosis_input（模型预测+证据+KB）           [L152-L155 → L403-L511]
        - chain.ainvoke → TriageResult（LLM 验证: 预测 DoS 与指标一致?）
      - incident.attack_type 更新为诊断结果                    [L234-L243]
      → audit_store.record(INCIDENT_TRIAGED) + SSE            [L245-L257]
   Step B TRIAGED→PLANNED                                    [L260-L267]
      RunbookAgent.generate_plan(incident, triage_result)     [L269]
        - CaseKB → RunbookKB → TopologyKB 三级检索
        - LLM 结构化输出 RunbookPlan（steps/审批/回滚）
      → audit + SSE "plan_generated"                          [L273-L283]
   Step C PLANNED→EXECUTING                                  [L296-L303]
      ActionOrchestrator.execute_plan(incident, plan, thread_id) [L305]
        - 逐步骤解析动作名 → ApprovalGate（Mock 自动批准）
        - TimeoutManager(15s, 3 重试, 退避) + Mock 故障注入
      → SSE "action_executed" per action                      [L309-L312]
   Step D Verifier.verify                                     [L315]
      全成功 → SUCCESS / 部分失败 → RETRY / 耗尽 → COMPENSATE / 升级 → ESCALATE
      → SSE "verification_finished"                           [L318-L320]
   Step E Replanner 循环（≤3 轮）                             [L326-L460]
      当前源码缺陷（实测）：Replanner.decide 内部已先迁移状态，
      _common_pipeline 再次迁移，真实 RESOLVE/COMPENSATE/ESCALATE/FAIL
      分支会抛 ValueError（如 VERIFIED→VERIFIED），流程中断为 SSE error；
      只有 RETRY 分支因 decide 内 EXECUTING→EXECUTING 非法被 catch 而可继续。
      "RESOLVE → VERIFIED → RESOLVED" 等终态流程仅在 StubReplanner 测试中成立
      （tests/test_zl_attack_detector.py:L310-L316，见 16.1-17）。
   Step F 最终 complete 事件                                  [L490-L506]
      仅当 Step E 未抛异常时产出；含 final_state / triage / plan /
      execution_results / verification；若 FAILED/ESCALATED 附 FailureContext。
      若 Step E 抛异常，则由 API 层 try/except 输出 SSE error 事件。

⑧ 服务层收尾          aiops_service.py:L82-L107
   收到 type=="complete": 若非失败 → 转发并 return
   若 workflow_failed → yield "workflow_failed" → PRP 恢复 → 必要时 Safety Control
   → 最终 complete（含 recovery_attempted/recovery_success）
   ※ 当前源码下该分支通常不可达：真实 Replanner 在 Step E 抛异常后，由 API 层直接输出 SSE error，
      `complete + workflow_failed` 一般不会产生（见 16.1-17、16.1-18）。

⑨ HTTP 响应           app/api/aiops.py:L99-L123
   每个事件 → SSE `data: {"event":"message","data":"{...json...}"}`
   遇 complete/error 断开
```

**此请求经过的模块清单**：`api/aiops.py → services/aiops_service.py → events/event_normalizer.py → events/severity_engine.py → core/incident_router.py → ml/attack_detector.py → ml/zl_attack_detector.py（或 rule）→ core/incident_store.py → core/state_machine.py → core/audit_store.py → agents/triage_agent.py → tools/knowledge_tool.py → services/vector_store_manager.py → agents/runbook_agent.py → agents/action_orchestrator.py → events/timeout_manager.py → tools/mock_actions.py → agents/verifier.py → agents/replanner.py`。（失败时追加：`agent/aiops/{planner,executor,replanner}.py`。）

## 14.2 次典型请求一：`POST /api/chat_stream`（RAG 对话）

`app/api/chat.py:L71-L176` → `RagAgentService.query_stream`（`L254-L322`）→ `_initialize_agent`（本地 3 工具 + MCP 工具 + `create_agent`）→ `agent.astream(stream_mode="messages")` → 提取 `AIMessageChunk.content_blocks[type=text]` → SSE `content` 事件 → 结束 `complete` 事件。会话持久化在 MemorySaver（thread_id=session_id）。

## 14.3 次典型请求二：`POST /api/upload`（RAG 写入）

`app/api/file.py:L25-L103` → 保存 `uploads/` → `VectorIndexService.index_single_file`（`L130-L170`）→ 删除旧向量 → 分割（md 三阶段/txt 单阶段）→ `Milvus.add_documents`（内部调 `DashScopeEmbeddings.embed_documents`）→ 响应（索引失败不影响上传成功）。

---

# 第十五部分 项目设计思想（基于源码证据）

## 15.1 为什么用 Service Layer？

- API 层以 SSE/JSON 包装为主，同时保留轻量请求校验、格式解析与文件保存（`app/api/file.py:L26-L103`、`app/api/aiops.py:L71-L95`）；复杂业务编排收敛在 `app/services/*`（L1）。
- 全局单例（`aiops_service`/`rag_agent_service`/`vector_store_manager`…）避免重复初始化昂贵资源（Milvus 连接、MCP 客户端、LLM 封装）——代价是全局可变状态（见 16.2）。

## 15.2 为什么主流水线不用 LangGraph，而恢复引擎用？

- 主流水线的步骤是**固定顺序 + 有限循环**（分诊→计划→执行→验证→重规划），用 async 生成器 + 状态机表达更直白，且能把每个中间事件直接 `yield` 给 SSE（`app/core/incident_router.py:L107-L198` 的 AsyncGenerator 签名即证据）。
- 恢复引擎需要"规划-执行-再评估"的**开放式循环**，LangGraph 的条件边 + checkpoint 恰好匹配（`app/services/aiops_service.py:L452-L488`）。
- 历史演进证据：`app/services/aiops_service.py:L11-L14` 注释明确"PRP 图只保留为失败后的恢复策略，不再有公开入口"——旧 LangGraph 主链路被新事件驱动链路取代后降级为恢复引擎（CHANGELOG_AIOPS.md 的 AIOPS-FUSION 系列记录同一过程）。

## 15.3 为什么引入 ML（AttackDetector）？

三层递进逻辑（L3，基于源码注释与结构）：
1. 纯 LLM 诊断（旧版）不可复现、成本高、无置信度语义；
2. 规则检测器（`RuleBasedAttackDetector`）确定性但置信度上限 0.6（`app/ml/attack_detector.py:L217-L219` 注释明说"规则方法不如监督学习可靠"）；
3. 监督学习模型（ZL）提供**带概率的、可审计的**第一答案，LLM 负责解释与兜底——"ML 回答 What，LLM 回答 Why/How"（`app/ml/attack_detector.py:L9-L11`）。

## 15.4 确定性逻辑与概率性推理的分层

| 层 | 模块 | 性质 |
|---|---|---|
| 事件治理 | Normalizer/Deduplicator/SeverityEngine/StateMachine/TimeoutManager/Verifier/Replanner | 确定性规则（同输入同输出） |
| 威胁检测 | AttackDetector（ZL/规则） | 概率性（规则分支例外，但置信度被压低） |
| 诊断/计划 | TriageAgent/RunbookAgent/PRP | 概率性（LLM 生成） |
| 兜底 | fallback 检测器 / `_fallback_triage` / `_fallback_plan` / Safety Control | 确定性回退，保证 LLM/ML 全挂时系统仍能给出可解释结果 |

这个分层的证据：每层概率性组件都带有确定性 fallback，且 fallback 都不隐藏失败原因（`fallback_reason`/`feature_vector` 记录，`app/ml/attack_detector.py:L117-L130`）。

## 15.5 数据流与控制流的分离

- 数据契约：Pydantic 模型在模块间传递（Incident → TriageResult → RunbookPlan → MockActionResult → VerificationResult → FailureContext）；
- 控制：状态机（状态迁移表 `app/core/state_machine.py:L41-L83`）+ 编排循环（`_common_pipeline`）+ 条件边（PRP 图）；
- 审计旁路：任何模块都可写 `audit_store`，但**只有 StateMachine 与编排器负责状态**——状态迁移的唯一入口是 `state_machine.transition()`（grep 确认除 incident_store.sync_state 外，业务代码均经 transition）。

## 15.6 ML / RAG / LLM / Agent / MCP 如何共同组成 AIOps

```
MCP（工具面，横向）── 对话/恢复引擎可用外部能力
RAG（知识面，横向）── 诊断与计划的知识来源
ML（感知面，确定性先验）── "发生了什么"的快速分类
LLM（推理面，概率性综合）── 解释、验证、计划、恢复
Agent（组织面）── 角色与数据契约
AIOps = 组织面 ×（感知面 + 推理面 + 知识面 + 工具面）+ 状态机/审计（安全底座）
```

---

# 第十六部分 当前架构存在的问题

> 分级：**Confirmed Issue**（源码直接证据）/ **Potential Risk**（源码证据 + 场景推断）/ **Optimization Suggestion**（L4 建议）。

## 16.1 Confirmed Issue

1. **`LLMFactory` 是死代码**。全仓无调用（grep 仅 `app/core/llm_factory.py` 定义与 `app/core/__init__.py:L26` 导出），且其 `ChatOpenAI` 封装与生产路径 `ChatQwen` 不一致，易误导读者以为 LLM 经它创建。
2. **`FeatureExtractor`/`FeatureVector` 未接入生产链路**。真实 ZL 推理走 `ZLFeatureAdapter`；`FeatureExtractor` 只有测试使用（`tests/test_attack_detector.py`）。其派生特征（renewal_interval_difference/ratio）不被 V2 模型消费。属早期接口设计的遗留。
3. **`trim_messages_middleware` 定义未接线**。`app/services/rag_agent_service.py:L41-L79` 定义了上下文修剪逻辑，但 `create_agent`（`L146-L150`）没有传入 middleware——会话历史超长时无修剪。
4. **`app/config.py:L66-L74` 的 8 个 AIOps 配置全部未被使用**。grep 证明 `mock_failure_rate`/`dedup_window_seconds`/`dedup_threshold`/`default_action_timeout_seconds`/`circuit_breaker_*` 0 处引用；`max_retry_cycles` 无引用（`verifier.py:L37` 是自身硬编码）；`app.config.Settings.approval_timeout_minutes` 也无引用，`timeout_manager.py:L294` 使用的是 `TimeoutConfig.approval_timeout_minutes`（`app/models/incident.py:L509`），不是同一配置项。实际生效的是各模块硬编码值（Deduplicator(10.0, 3)、动作超时 15.0s、ApprovalGate(10min)）。
5. **前端 AIOps 调用指向不存在的端点**。`static/app.js:L1181` 请求 `${apiBaseUrl}/aiops`（= `POST /api/aiops`），后端只有 `/api/aiops/incident|stsrs|metrics`（`app/api/aiops.py:L33/L126/L186`），且请求体只有 `{session_id}` 不含事件数据 → 必然 404/无法完成处置。
6. **指标量纲语义不一致（影响 ML 正确性）**。STSRS 测试数据与 ZL 训练域中 `packet_loss=95.23`（百分数）、`latency=354.46`（ms 原始值）（`tests/test_zl_attack_detector.py:L26-L49`）；而 SeverityEngine 阈值按小数设计（`packet_loss>0.5` 注释"50% 丢包"，`app/events/severity_engine.py:L49`）；API 文档示例又是小数（`"packet_loss": 0.35`，`app/api/aiops.py:L203-L212`）。实测小数语义输入给 ZL 模型会得到与直觉不符的高置信分类（见 5.7.5）——输入分布不在训练域。
7. **`mcp_servers/README.md` 工具清单与源码不符**。README 声称的工具（`search_service_logs`/`analyze_log_pattern`/`query_process_list`/`search_historical_tickets`/`get_service_info`/`list_all_services`）在 `cls_server.py`（5 个工具）与 `monitor_server.py`（2 个工具）中不存在。
8. **Pydantic v1 风格 `class Config` + `datetime.utcnow()` 弃用**。全部数据模型用 `class Config: json_encoders`（`app/models/metrics.py:L26-L29` 等），`datetime.utcnow()` 遍布状态机/存储（`app/core/state_machine.py:L161` 等）——pytest 运行产生大量 `PydanticDeprecatedSince20`/`DeprecationWarning`（实测）。
9. **全部业务状态为进程内存态**。IncidentStore/AuditStore/MemorySaver 均无持久化（`app/core/incident_store.py:L17-L23`、`app/services/rag_agent_service.py:L16` 自述），重启即丢失事件、审计、会话。
10. **审批流是模拟的**。ApprovalGate 创建请求后立即自动批准（`app/agents/action_orchestrator.py:L237-L242` 注释"Mock 模式: 自动批准"）；`TimeoutManager.await_approval`（`app/events/timeout_manager.py:L279-L331`）全仓无调用——人工审批闭环未实现。
11. **`.env` 含明文 DashScope API Key** 且位于仓库根（`.env:L7`），CORS 全开放 + credentials（`app/main.py:L53-L59`）。
12. **AIOps 三个 POST 端点无请求 Schema**（裸 `dict`，`app/api/aiops.py:L35/L128/L187`），错误输入只能靠内部 try/except 兜底，无法获得 OpenAPI 校验与文档。
13. **`should_use_new_link` 死方法**。`app/core/incident_router.py:L72-L88` 定义但全仓无调用（route 不做该判断，所有来源统一处理）。
14. **重复代码**。`SeverityEngine._flatten_metrics`/`_detect_source_conflicts`（`severity_engine.py:L268-L366`）与 `TriageAgent._flatten_metrics`/`_detect_source_conflicts`（`triage_agent.py:L567-L629`）几乎重复；异常阈值常量也双份维护（两者 drift 风险）。
15. **`app/agents/__init__.py` 的 5 个 lazy accessor 无人使用**（grep 确认），属于防循环导入的历史遗留。
16. **旧白皮书与当前源码多处不一致**（本文重写原因，详见 1.6）：`prometheus_simulator.py` 已删除、`agents/incident_router.py` 已删除、TriageAgent"唯一诊断者"结论过时等。
17. **真实 `Replanner` 双重状态迁移会中断主流水线**（2026-08-14 实测）：`Replanner.decide()` 内部迁移（`replanner.py:L79-L111`）后，`_common_pipeline` 再次迁移（`incident_router.py:L331-L346/L368-L372/L437-L455`），`RESOLVE/COMPENSATE/ESCALATE/FAIL` 分支抛 `ValueError`；现有 E2E 测试用 `StubReplanner`（`tests/test_zl_attack_detector.py:L310-L316`）掩盖了该问题。
18. **恢复成功与 Safety Control 无法改变严格终态**：FAILED/ESCALATED 是严格终态（`state_machine.py:L77-L92`），`aiops_service.py:L151-L167` 的 RESOLVED 迁移必然失败并被 `except ValueError: pass` 吞掉；`L399-L414` 的 ESCALATED 迁移同样失败并被 `except Exception` 吞掉，事件状态不会按注释更新。
19. **`recovery_attempt` 从未递增**：`MAX_RECOVERY_ATTEMPTS=3` 检查在 replanner（`replanner.py:L141-L155`）与 `should_continue`（`aiops_service.py:L477`）中均不可达；`state.py:L37` 注释声称“由 Replanner 递增”与代码不符。
20. **`Replanner.decide` 的迁移审计缺 trace_id/thread_id**：`state_machine.transition` 调用未传这两个参数（`replanner.py:L85-L90`），审计记录中 thread 为空。

## 16.2 Potential Risk

1. **pickle 反序列化安全**：模型文件直接 `pickle.load`（`app/ml/zl_attack_detector.py:L279`）。当前是本地可信产物，若模型文件被替换为恶意 pickle 可导致代码执行。
2. **首次推理延迟 ~5-7s**（实测：含 1MB pickle 加载 + sklearn import；本机两次首载实测约 5.1s 与 7.0s）。在高并发冷启动场景，首个事件会显著变慢；之后走 `_artifact` 缓存（`L273-L274`）。
3. **manifest 内旧绝对路径**（`E:\ZL\STSRS\...`）依赖 `_resolve_model_path` 的存在性回退逻辑（`L315-L345`）；若未来 manifest 更新路径语义变化，回退链可能失效。
4. **`Replanner.retry_count` 跨事件累积**：`Replanner` 是 Router 的实例属性（`app/core/incident_router.py:L53`），`retry_count` 在 `_map_status` 中自增且无 reset 调用点（`app/agents/replanner.py:L52`、`L121`）——第二个事件可能直接触发"超限→ESCALATE"。
5. **`run_in_executor` 的同步动作无法被 `asyncio.wait_for` 真正中断**（`app/agents/action_orchestrator.py:L405-L409` + `app/events/timeout_manager.py:L157`）：Mock 动作的 `time.sleep` 超时后线程仍继续执行（超时语义不精确）。
6. **模型输出仅作"建议"**：`confidence < threshold → UNKNOWN` 只改 attack_type 不改概率（`zl_attack_detector.py:L244-L245`），下游若只读 probabilities 会绕过阈值语义（当前下游读的是 attack_type，风险暂不触发）。

## 16.3 Optimization Suggestion（L4）

- 为 `AttackPrediction` 增加模型版本注册/校验（当前 `model_version` 只是字符串拼接）。
- ML 结果可缓存（同 dedup_key 短窗内复用推理结果）。
- 量纲统一：在 `ZLFeatureAdapter` 增加单位声明或输入校验（拒绝/转换小数语义的 packet_loss），避免分布外输入。
- 用 `ConfigDict` + `timezone-aware datetime` 替换弃用写法。
- 将 16.1-4 的配置项接入实际代码或删除。
- 前端 AIOps 按钮改为调用 `/api/aiops/metrics` 或 `/api/aiops/incident` 并携带事件体。

---

# 第十七部分 项目可以如何扩展（基于当前架构）

## 17.1 ML 模型替换 / 多模型 / 版本管理

- **替换**：实现 `AttackDetector` 子类 + 改 `ml_attack_detector_backend` 即可，Agent 层零改动（接口契约 `app/ml/attack_detector.py:L33-L63`；工厂 `L298-L344` 是唯一入口）。
- **多模型**：现有 `FallbackAttackDetector` 已示范包装模式；可仿照实现 `EnsembleAttackDetector`（接口注释里也提到，`L39-L44`）。
- **版本管理**：`model_version` 属性与 `ZL_MODEL_VERSION` 配置已存在（`zl_attack_detector.py:L210-L214`）；可扩展为按版本加载不同 manifest（`_load_manifest` 已支持自定义路径）。

## 17.2 RAG 扩展

- 现检索为纯向量 top-k（`app/tools/knowledge_tool.py:L30-L34`）；可在 `as_retriever` 与 `format_docs` 之间插入 reranker。
- KB 分区：`KBQueryRequest.kb_type` 模型已预留（`app/models/incident.py:L467-L473`），实现按 metadata 过滤即可获得真正的 CaseKB/RunbookKB/TopologyKB 分区。

## 17.3 Agent 扩展

- 新增 pipeline Agent：在 `_common_pipeline` 中插入新 Step（`app/core/incident_router.py:L204-L506` 的结构清晰，每个 Step 都是"transition → 调用 → 审计 → SSE"模板）。
- 将主流水线迁移回 LangGraph：状态已在 `IncidentRecord`，可映射为 graph state（恢复引擎已示范 StateGraph 用法）。

## 17.4 MCP Tool 扩展

- 新 server 只需：① 实现 FastMCP server；② `app/config.py:L76-L88` 的 `mcp_servers` property 加一项；③ `.env` 加 URL。客户端零代码改动（`MultiServerMCPClient(servers)` 字典驱动，`app/agent/mcp_client.py:L189-L211`）。

## 17.5 Workflow 扩展

- PRP 图（`app/services/aiops_service.py:L452-L488`）可增加"验证器"节点（当前恢复模式没有显式 verify 节点，靠 replanner 判断）。

## 17.6 AIOps 能力扩展

- 持久化：把 IncidentStore/AuditStore 的内存 dict 换成 DB/Redis（接口方法已稳定）。
- 真实动作系统：替换 `ALL_MOCK_ACTIONS` 注册表（`app/tools/mock_actions.py:L309-L318`）为真实运维 API 调用，TimeoutManager/审批门/回滚语义可直接复用。
- 模型输出参与决策：当前 ML 不直接分叉控制流；如需"高置信直接处置、低置信升级人工"，可在 `_common_pipeline` Step A 后按 `attack_prediction` 加条件分支（架构上已有插入点）。

---

# 第十八部分 项目源码文件索引

| 文件 | 职责 | 核心 Class / Function | 被谁调用 | 调用谁 |
|---|---|---|---|---|
| `app/main.py` | FastAPI 入口 | `lifespan`、`app` | uvicorn/Makefile | api 路由、milvus_client |
| `app/config.py` | 全局配置 | `Settings`、`config` | 几乎所有模块 | .env |
| `app/api/health.py` | 健康检查 | `health_check` | HTTP | milvus_client |
| `app/api/chat.py` | 对话 API | `chat`、`chat_stream` | HTTP | rag_agent_service |
| `app/api/file.py` | 上传/索引 API | `upload_file`、`index_directory` | HTTP | vector_index_service |
| `app/api/aiops.py` | AIOps API | `process_metrics_stream` 等 8 端点 | HTTP | aiops_service、incident_store、audit_store |
| `app/services/aiops_service.py` | AIOps 编排+恢复 | `AIOpsService`、`process_incident`、`_build_recovery_graph` | api/aiops | incident_router、PRP 图、Safety |
| `app/services/rag_agent_service.py` | RAG 对话服务 | `RagAgentService`、`query_stream` | api/chat | create_agent、MCP client、工具 |
| `app/services/vector_index_service.py` | 文档索引 | `VectorIndexService` | api/file | splitter、vector_store_manager |
| `app/services/document_splitter_service.py` | 文档分块 | `DocumentSplitterService` | vector_index | LangChain splitters |
| `app/services/vector_embedding_service.py` | Embedding | `DashScopeEmbeddings` | vector_store_manager | DashScope API |
| `app/services/vector_store_manager.py` | 向量库封装 | `VectorStoreManager` | knowledge_tool、vector_index | langchain_milvus、milvus_client |
| `app/core/incident_router.py` | 主流水线编排 | `IncidentRouter`、`route`、`_common_pipeline`、`_build_metric_record` | aiops_service | events、**ml**、agents、stores |
| `app/core/state_machine.py` | 状态机 | `StateMachine`、`transition` | router、replanner、aiops_service | audit callback |
| `app/core/incident_store.py` | 事件存储 | `IncidentStore` | router、api | — |
| `app/core/audit_store.py` | 审计+SSE | `AuditStore`、`record`、`subscribe_generator` | 全体 | asyncio.Queue |
| `app/core/milvus_client.py` | Milvus 底层 | `MilvusClientManager`、`connect` | main、vector_store_manager | pymilvus |
| `app/core/llm_factory.py` | **（未使用）** | `LLMFactory` | 无 | — |
| `app/events/event_normalizer.py` | 事件归一化 | `EventNormalizer`、`normalize`、`normalize_metric` | router | — |
| `app/events/deduplicator.py` | 滑动窗口去重 | `Deduplicator` | router | — |
| `app/events/severity_engine.py` | 规则分级 | `SeverityEngine`、`evaluate` | router、triage fallback | — |
| `app/events/timeout_manager.py` | 超时/重试/熔断 | `TimeoutManager`、`CircuitBreaker` | action_orchestrator | asyncio |
| **`app/ml/attack_detector.py`** | **检测接口+工厂** | `AttackDetector`、`FallbackAttackDetector`、`RuleBasedAttackDetector`、`MockAttackDetector`、`create_attack_detector` | incident_router | zl_attack_detector |
| **`app/ml/zl_attack_detector.py`** | **ZL 模型适配器** | `ZLAttackDetector`、`ZLFeatureAdapter`、`ZLModelArtifact` | factory | pickle/numpy/sklearn |
| **`app/ml/feature_extractor.py`** | **（生产未使用）** | `FeatureExtractor` | 仅测试 | — |
| `app/agents/triage_agent.py` | 分诊诊断 | `TriageAgent`、`triage`、`_analyze_metric_anomalies` | router | ChatQwen、retrieve_knowledge |
| `app/agents/runbook_agent.py` | 计划生成 | `RunbookAgent`、`generate_plan` | router | ChatQwen、retrieve_knowledge |
| `app/agents/action_orchestrator.py` | 动作执行 | `ActionOrchestrator`、`ApprovalGate` | router、safety | mock_actions、timeout_manager |
| `app/agents/verifier.py` | 结果验证 | `Verifier` | router | audit_store |
| `app/agents/replanner.py` | 路由决策 | `Replanner`、`ReplanAction` | router | state_machine、incident_store |
| `app/agent/mcp_client.py` | MCP 客户端 | `get_mcp_client(_with_retry)`、`retry_interceptor` | rag service、PRP | langchain_mcp_adapters |
| `app/agent/aiops/state.py` | PRP 状态 | `PlanExecuteState` | PRP 图 | — |
| `app/agent/aiops/planner.py` | PRP 规划 | `planner` | PRP 图 | ChatQwen、工具、RAG |
| `app/agent/aiops/executor.py` | PRP 执行 | `executor` | PRP 图 | ChatQwen、ToolNode |
| `app/agent/aiops/replanner.py` | PRP 重规划 | `replanner`、`Act`/`Response` | PRP 图 | ChatQwen |
| `app/models/incident.py` | 事件模型全集 | `Incident`、`TriageResult`、`RunbookPlan`、`FailureContext` 等 | 全体 | — |
| `app/models/metrics.py` | 指标/预测模型 | `RailMetricRecord`、`AttackPrediction`、`FeatureVector` | ml、router、stsrs | — |
| `app/models/request.py` / `response.py` | HTTP 模型 | `ChatRequest` 等 | api/chat | — |
| `app/data/stsrs_adapter.py` | 数据融合 | `STSRSAdapter`、`load_and_fuse` | 测试/离线 | — |
| `app/tools/knowledge_tool.py` | RAG 检索工具 | `retrieve_knowledge`、`format_docs` | 各 Agent | vector_store_manager |
| `app/tools/time_tool.py` | 时间工具 | `get_current_time` | 对话/PRP | — |
| `app/tools/query_metrics_alerts.py` | Prometheus 告警 | `query_prometheus_alerts` | 对话/PRP | httpx |
| `app/tools/mock_actions.py` | Mock 动作 | 8 动作 + `get_action` 等 | action_orchestrator | random/time |
| `app/utils/logger.py` | 日志 | `setup_logger` | 导入期自动 | loguru |
| `mcp_servers/cls_server.py` | CLS MCP 服务 | 5 个 `@mcp.tool` | MCP client | FastMCP |
| `mcp_servers/monitor_server.py` | 监控 MCP 服务 | 2 个 `@mcp.tool` | MCP client | FastMCP |
| `static/app.js` / `index.html` | 前端 | `sendAIOpsRequest` 等 | 浏览器 | API（注意 16.1-5） |
| `tests/*` | 测试（31 用例全过） | 见附录 B | pytest | app |

---

# 附录 A：事实等级约定

- **L1（源码直接证据）**：代码中明确存在（引用文件:行号）。
- **L2（调用链推导）**：多个源码位置组合确定（如"全仓 grep 无调用"）。
- **L3（架构推断）**：基于结构的设计思想判断（已在文中标注）。
- **L4（建议）**：优化方向，非源码事实。
- 文中标注"实测"的结论来自 2026-08-14 在本机（numpy 2.4.2 / sklearn 1.9.0 / ZL 产物存在）的验证运行。

# 附录 B：测试清单与运行结果

- `tests/test_attack_detector.py`（11 例）：FeatureExtractor 特征提取/派生特征、AttackPrediction 模型、Mock/RuleBased 检测器三规则、Incident 携带预测、`_build_metric_record` 重建、全 ML 流水线。
- `tests/test_zl_attack_detector.py`（10 例）：ZLFeatureAdapter 字段映射/缺失报错、EventNormalizer 展开 metrics_snapshot、pickle 模型预测（FakeClassifier）、置信度阈值→UNKNOWN、Fallback 标记 model_load_failed、Fallback 不吞 invalid_input、**Router 端到端（stub 全链路到 RESOLVED 且 attack_prediction 进入事件流）**、实时指标旁路去重、**真实 ZL V2 pickle smoke（本机通过）**。注意：Router E2E 使用 `StubReplanner`，未覆盖真实 Replanner 状态迁移（见 16.1-17）。
- `tests/test_stsrs_fusion.py`（10 例）：多源融合、FusionKey、公共/冲突指标分离、攻击字段剥离、来源识别、SeverityEngine 来源冲突。
- **实测结果**：`31 passed`（`pytest tests/ --no-cov`，2026-08-14；另有 DeprecationWarning/PydanticDeprecatedSince20 警告若干，见 16.1-8）。
- 测试揭示的行为契约（开发者明确保证的行为）：Fallback 只兜底 load 失败、InputError 必须暴露、单条实时指标旁路去重缓冲、预测结果必须进入 SSE 事件流。

# 附录 C：新旧白皮书差异对照表

| 主题 | 旧白皮书表述 | 当前源码事实 | 本文章节 |
|---|---|---|---|
| 诊断职责 | TriageAgent 是"真正诊断 Agent" | ML 先预测，TriageAgent 验证/解释/补充 | 1.6、5.1、5.12 |
| ML 存在性 | 无 | `app/ml/` 3 文件 + 外部 ZL 产物 | 第五部分 |
| 调用链 | Normalize→Severity→Triage | 插入 AttackDetector 节点 | 12、13.1 |
| 数据流 | 无 ML 分支 | attack_prediction 贯穿 Incident→Triage | 5.8、5.11 |
| 配置文件 | config 无需额外配置 | +8 个 ML 配置 | 1.6 |
| `prometheus_simulator.py` | 存在 | 已删除 | 1.5、16.1-16 |
| `agents/incident_router.py` | 存在（双 Router bug） | 已删除，仅 core 一份 | 16.1-16 |
| TriageAgent KB 查询 | 纯异常模式 | 模型预测优先 | 5.13 |
| 技术栈 | 无 numpy/sklearn | pyproject 已声明并安装 | 1.3 |
| AIOps 恢复 | 描述为 Legacy PRP | 仍是恢复引擎（未变） | 7.8、9 |
