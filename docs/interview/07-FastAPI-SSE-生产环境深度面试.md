# 第七册：FastAPI + SSE + 生产环境深度面试

> 基于 RailOps Agent 源码 — 流式架构、事件驱动、生产就绪评估

---

## Q1：为什么 AIOps 诊断接口选择 SSE 而非 WebSocket？

### 标准答案

```python
# app/api/aiops.py:64
return EventSourceResponse(event_generator())

# app/api/chat.py:176
return EventSourceResponse(event_generator())
```

**核心原因：** AIOps 诊断是**单向数据流**——服务端推送诊断进度给客户端，客户端不需要向服务端发送中间结果。SSE 的优势：
1. **简单**：`EventSourceResponse` 接受一个 async generator，代码简洁
2. **HTTP 原生**：基于 HTTP，不需要升级协议（WebSocket 需要 `Upgrade` 握手）
3. **自动重连**：浏览器 `EventSource` API 内置自动重连
4. **代理友好**：HTTP/1.1 代理天然支持 SSE，WebSocket 可能需要特殊配置

### 追问 #1：当前 SSE 实现中 `EventSourceResponse` 来自 `sse-starlette` 而非 FastAPI 原生，为什么？

**答案：** FastAPI 本身基于 Starlette，而 Starlette 的 `StreamingResponse` 在 SSE 场景下有几个问题：
1. 不支持 `event:` 和 `id:` 标准的 SSE 字段
2. 缺少自动 ping/pong 保持连接
3. `sse-starlette` 专门为 SSE 优化，处理了连接中断和重连

### 追问 #2：AI诊断流中，为什么要 `break` 在 `type == "complete"` 或 `type == "error"`？

**答案：** `app/api/aiops.py:51-52`
```python
if event.get("type") in ["complete", "error"]:
    break
```
这是防止生成器继续运行。虽然 `EventSourceResponse` 在客户端断开连接后会取消生成器，但主动 `break` 提供了更明确的终止语义，避免资源泄漏。

### 追问 #3：为什么所有 SSE 数据都用 `json.dumps(ensure_ascii=False)`？

**答案：** `ensure_ascii=False` 保证中文字符不被转义为 `\uXXXX`。在运维场景下，诊断报告包含大量中文内容，使用原生的 UTF-8 编码可以减少数据传输量和前端解析复杂度。

### 追问 #4：如果客户端网络中断，SSE 流会怎么样？

**答案：** `EventSourceResponse` 会检测到客户端断开并取消 async generator。但当前代码中 `audit_store.subscribe_generator` (`app/core/audit_store.py:166-187`) 通过 `asyncio.CancelledError` 捕获取消信号并清理订阅。这是正确的资源管理。

---

## Q2：`app/main.py` 中 lifespan 管理 Milvus 连接为什么使用 `@asynccontextmanager`？

### 标准答案

```python
# app/main.py:19-41
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动: 连接 Milvus
    milvus_manager.connect()
    yield
    # 关闭: 断开 Milvus
    milvus_manager.close()
```

**核心原因：** 这是 FastAPI 推荐的资源管理模式。`yield` 之前的代码在启动时执行，`yield` 之后的代码在关闭时执行。相比于 `@app.on_event("startup")` 和 `@app.on_event("shutdown")`，lifespan 模式：
1. 自动处理异常（startup 失败不会导致 shutdown hook 执行）
2. 更清晰的资源所有权
3. 支持结构化并发

### 追问 #1：但 `VectorStoreManager.__init__` 也调用了 `milvus_manager.connect()`，这不是重复了吗？

**答案：** `app/services/vector_store_manager.py:32-33`
```python
_ = milvus_manager.connect()
```
注释解释了：`"必须在 PyMilvus / langchain_milvus 访问 Collection 之前建立连接，否则会出现 ConnectionNotExistException"`。由于 `VectorStoreManager` 在模块导入时就初始化（全局单例），它需要在 FastAPI lifespan 之前就建立连接。而 lifespan 中的 `connect()` 对已存在的连接是幂等的（`app/core/milvus_client.py:70-72`）。

### 追问 #2：`MilvusClientManager.connect()` 的幂等设计怎么实现的？

**答案：** `app/core/milvus_client.py:70-72`
```python
if self._collection is not None and self._client is not None:
    logger.debug("Milvus 已连接，跳过重复 connect")
    return self._client
```
通过检查私有属性 `_collection` 和 `_client` 是否已初始化来实现幂等。但这里有个隐含假设：不存在"部分初始化"（如 `_client` 已创建但 `_collection` 未加载）。

### 追问 #3：CORS 全开 (`allow_origins=["*"]`) 生产环境如何修改？

**答案：** `app/main.py:53-59` 注释提醒 "生产环境应该限制具体域名"。建议方案：
```python
allow_origins = config.cors_origins.split(",") if config.cors_origins else ["*"]
```

---

## Q3：`IncidentRouter` 中 SSE 事件的 `_sse()` 方法为什么包含 `trace_id` 和 `thread_id`？

### 标准答案

```python
# app/core/incident_router.py:460-473
def _sse(self, incident, event_type, message, thread_id, data=None):
    return {
        "type": event_type.value,
        "trace_id": incident.trace_id,      # 全链路追踪ID
        "incident_id": incident.incident_id, # 事件ID
        "thread_id": thread_id,              # LangGraph线程ID
        "message": message,
        "data": data or {},
    }
```

**核心原因：** 这是分布式追踪的**三维关联**设计：
- `trace_id`：一次完整事件处理流程的唯一标识（跨 Agent、跨系统）
- `incident_id`：具体事件的业务标识
- `thread_id`：LangGraph 状态线程标识（与 checkpoint 对齐）

三者形成交叉索引，支持"按事件查线程"、"按追踪查事件"、"按线程查审计"等多维度查询。

### 追问 #1：为什么 `_sse()` 在 `IncidentRouter` 中定义而非提取为公共函数？

**答案：** 因为 `_sse()` 需要 `Incident.trace_id` 和 `thread_id` 上下文。提取为独立函数需要传入 6 个参数，可读性反而下降。而且两个 `IncidentRouter`（`agents/` 和 `core/`）各自实现了 `_sse()`，说明这确实是"上下文绑定"的辅助方法。

### 追问 #2：SSE 事件类型为什么有 20+ 种？

**答案：** `app/models/incident.py:82-110` 定义了 20+ 个 `SSEEventType` 枚举值。这是为了**前端精确渲染**——每种事件类型对应不同的 UI 状态（进度条、状态徽章、审批弹窗、错误提示）。如果只用通用的 `"message"` 类型，前端需要解析 `data` 来区分——增加了前端复杂度和耦合。

---

## Q4：当前系统的可观测性有什么不足？

### 标准答案

**已有（好）：**
```python
# app/utils/logger.py:23-45
logger.add(sys.stdout, format="...", level="DEBUG" if config.debug else "INFO")
logger.add("logs/app_{time}.log", rotation="00:00", retention="7 days", compression="zip")
```
- Loguru 结构化日志 + 按天轮转 + 自动压缩
- AuditStore 全链路审计日志（`app/core/audit_store.py`）
- 健康检查端点（`app/api/health.py`）

**缺失（需补充）：**
1. **Prometheus Metrics**：请求延迟、Agent 调用次数、LLM token 消耗
2. **OpenTelemetry Tracing**：跨服务（FastAPI → MCP Server → Milvus）的链路追踪
3. **告警规则**：错误率 > 阈值、Agent 决策超时、Milvus 连接断开

### 追问 #1：AuditStore 的数据量会无限增长吗？

**答案：** `app/core/audit_store.py` 中 `_audit_logs` 是内存字典，**没有自动清理机制**。长时间运行会导致内存溢出。生产环境必须：
1. 定期清理过期审计日志（如保留 30 天）
2. 持久化到 TimescaleDB / Elasticsearch

### 追问 #2：为什么日志同时输出到控制台和文件？

**答案：** `app/utils/logger.py:23-44`：
- 控制台：`colorize=True`, `DEBUG` 级别 → 开发时实时查看
- 文件：`rotation="00:00"`, `compression="zip"` → 生产环境问题回溯

`enqueue=True` 确保文件写入不阻塞主线程——日志 IO 是异步的。

---

## Q5：如果要部署到 Kubernetes，需要做哪些改造？

### 标准答案

**当前架构的 K8s 适配问题：**

1. **内存存储 → 外部存储**
```python
# app/core/incident_store.py:27
self._incidents: Dict[str, IncidentRecord] = {}  # ❌ 重启即丢失
```
→ 改用 PostgreSQL + Redis

2. **单进程 → 多副本**
```python
# app/agent/mcp_client.py:17
_mcp_client: Optional[MultiServerMCPClient] = None  # ❌ 进程内单例
```
→ 每副本独立连接 MCP Server（或引入 Connection Pool）

3. **文件上传 → 对象存储**
```python
# app/api/file.py:72
file_path.write_bytes(content)  # ❌ 本地文件系统
```
→ 改用 S3/MinIO

4. **健康检查 → K8s Probes**
```python
# app/api/health.py:13-64
# ✅ 已有的健康检查可复用为 readiness probe
```

5. **日志 → 集中式日志**
```python
# app/utils/logger.py:34-44
# ❌ 本地文件轮转
```
→ 输出到 stdout（K8s 自动收集）或 Loki

---

**FastAPI + SSE + 生产环境深度面试 — 本章结束**

关键文件索引：
- `app/main.py:19-41` — FastAPI lifespan
- `app/api/aiops.py` — SSE 诊断接口（7个端点）
- `app/api/chat.py` — RAG 流式对话
- `app/api/health.py` — 健康检查
- `app/core/audit_store.py` — SSE 事件总线 + 订阅机制
- `app/utils/logger.py` — Loguru 双输出配置
