# 第六册：MCP 协议深度面试

> 基于 RailOps Agent 源码 — MCP 客户端管理、重试拦截器、FastMCP 服务端实现

---

## Q1：为什么 RailOps 使用 MCP 协议而非直接引入 Python SDK？

### 标准答案

```python
# app/config.py:67-78
@property
def mcp_servers(self) -> Dict[str, Dict[str, Any]]:
    return {
        "cls":     {"transport": "streamable-http", "url": "http://localhost:8003/mcp"},
        "monitor": {"transport": "streamable-http", "url": "http://localhost:8004/mcp"},
    }
```

**核心原因：** MCP (Model Context Protocol) 提供**标准化的工具接口**。不同的运维工具（CLS 日志查询、Prometheus 监控、自定义数据源）只需要实现 MCP Server 协议，Agent 就能通过统一的 `MultiServerMCPClient` 调用它们。

这实现了**工具即插即用**（Tool Plug-and-Play）：
- 添加新工具 = 启动一个 MCP Server + 在配置中注册
- 不需要修改 Agent 代码
- 不需要导入任何 SDK

### 追问 #1：为什么选择 MCP 而非直接使用 LangChain Tool？

**答案：** 对比两种方式：
```python
# 方式 A: LangChain Tool (knowledge_tool.py)
@tool(response_format="content_and_artifact")
def retrieve_knowledge(query: str) -> Tuple[str, List[Document]]:
    ...

# 方式 B: MCP Tool (通过 MultiServerMCPClient)
mcp_tools = await mcp_client.get_tools()
all_tools = local_tools + mcp_tools   # 透明合并
```

MCP 的优势：
1. **进程隔离**：MCP Server 独立进程运行，crash 不影响主服务
2. **语言无关**：MCP Server 可以用任何语言实现
3. **热更新**：修改 MCP Server 不需重启 FastAPI
4. **标准化**：MCP 是 Anthropic 提出的开放标准，多框架可用

### 追问 #2：项目支持的三种 transport 有什么区别？

**答案：** `app/config.py:46-49` 注释中说明：
```python
# transport: stdio | sse | streamable-http
```

| Transport | 适用场景 | 当前使用 |
|-----------|---------|---------|
| `stdio` | 本地子进程通信 | 未使用 |
| `sse` | 腾讯云托管 MCP (/sse/ 端点) | `.env` 中 cls 配置为 `sse` |
| `streamable-http` | 本地 FastMCP 服务 | `.env` 中 monitor 配置为 `streamable-http` |

### 追问 #3：`suggest_mcp_transport()` 的作用是什么？

**答案：** `app/agent/mcp_client.py:214-230`
```python
def suggest_mcp_transport(url: str, transport: str) -> str | None:
    if "/sse" in lower_url and transport in ("streamable-http", "http"):
        return "MCP URL 含 /sse/ 但 transport=..., 腾讯云等托管端点应使用 transport=sse"
    if transport == "sse" and "/mcp" in lower_url and "/sse" not in lower_url:
        return "MCP URL 为本地 FastMCP 路径但 transport=..., 本地服务通常应使用 transport=streamable-http"
```

这是**配置自检机制**——在 `RagAgentService._initialize_agent()` (`app/services/rag_agent_service.py:123-129`) 中调用，帮助开发者发现 transport 配置错误。这是防御性编程的体现。

---

## Q2：`retry_interceptor` 为什么设计为拦截器而非包裹在 Tool 调用中？

### 标准答案

```python
# app/agent/mcp_client.py:46-102
async def retry_interceptor(
    request: MCPToolCallRequest,
    handler,
    max_retries: int = 3,
    delay: float = 1.0,
):
    for attempt in range(max_retries):
        try:
            result = await handler(request)
            return result
        except Exception as e:
            wait_time = delay * (2 ** attempt)  # 指数退避
            await asyncio.sleep(wait_time)
    # 返回错误结果而非抛出异常
    return CallToolResult(content=[TextContent(type="text", text=error_msg)], isError=True)
```

**核心原因：** 作为 MCP 客户端的**中间件**，它拦截所有 MCP 工具的调用，统一添加重试逻辑。这比在每个工具中分别实现重试更优雅——新增的 MCP 工具自动获得重试能力。

### 追问 #1：为什么重试失败后返回 `CallToolResult(isError=True)` 而非抛异常？

**答案：** 这是**优雅降级**策略（`app/agent/mcp_client.py:97-102`）。如果抛异常：
- LangGraph 的 ToolNode 会中断整个执行
- Executor 无法感知"工具失败但系统正常"

返回带 `isError=True` 的结果后，LLM 可以在下一轮决策中看到错误信息并调整策略。这是对 Agent 友好的错误处理。

### 追问 #2：指数退避的公式为什么是 `delay * (2 ** attempt)`？

**答案：** 标准二进制指数退避（Binary Exponential Backoff）：
- 第 1 次重试：等待 `1.0 * 2^0 = 1.0` 秒
- 第 2 次重试：等待 `1.0 * 2^1 = 2.0` 秒
- 第 3 次重试：等待 `1.0 * 2^2 = 4.0` 秒

总等待时间约 7 秒。但 `max_retries=3` 意味着总共执行 3 次（1 次原始 + 2 次重试），而非 4 次。注释 "第 1/3 次尝试" 暗示这是总尝试次数而非重试次数。

### 追问 #3：为什么不加 Jitter（随机抖动）？

**答案：** 当前实现没有 Jitter。在分布式系统中，多个客户端同时重试可能导致"惊群效应"（Thundering Herd）。标准做法是添加 ±25% 的随机抖动。但在这个场景下，MCP 客户端是**单例** (`app/agent/mcp_client.py:17`)，不会有多个客户端同时重试，所以不加 Jitter 影响不大。

---

## Q3：全局 MCP 客户端单例 `_mcp_client` 的设计有什么考量？

### 标准答案

```python
# app/agent/mcp_client.py:17
_mcp_client: Optional[MultiServerMCPClient] = None

# app/agent/mcp_client.py:112-155
async def get_mcp_client(force_new=False):
    global _mcp_client
    if force_new:
        return _create_mcp_client(...)  # 不缓存
    if _mcp_client is None:
        _mcp_client = _create_mcp_client(...)
    return _mcp_client
```

**设计原因：**
1. **避免重复连接**：每个 MCP Server 连接有建立成本
2. **统一配置**：所有 Agent 使用同一套 MCP 工具
3. **`force_new=True`** 提供了打破单例的 escape hatch

### 追问 #1：全局单例在 FastAPI 异步环境下安全吗？

**答案：** 有潜在风险。`MultiServerMCPClient` 内部可能绑定到特定 event loop。如果 FastAPI 使用多个 worker 进程，每个进程的 `_mcp_client` 是独立的——这是安全的。但如果使用 `uvicorn` 的 `--workers` 多进程 + 内存单例，跨进程共享的 MCP 连接会失效。

### 追问 #2：`load_mcp_tools_safe` 的防御性设计体现在哪里？

**答案：** `app/agent/mcp_client.py:35-43`
```python
async def load_mcp_tools_safe(client):
    try:
        tools = await client.get_tools()
        return tools, None
    except BaseException as e:
        return [], format_exception_chain(e)
```

关键点：捕获 `BaseException`（而非 `Exception`），包括 `asyncio.CancelledError`。返回空列表 + 错误信息而非抛出异常。这意味着 MCP 工具加载失败**不会阻止 RAG Agent 启动**——它只用本地工具继续服务。

### 追问 #3：`format_exception_chain` 为什么需要展开 `ExceptionGroup`？

**答案：** `app/agent/mcp_client.py:20-32`
```python
def format_exception_chain(exc):
    sub_exceptions = getattr(exc, "exceptions", None)
    if sub_exceptions is not None:
        # 展开 ExceptionGroup
        for i, sub in enumerate(sub_exceptions):
            lines.append(f"  [{i}] {format_exception_chain(sub)}")
```
Python 3.11+ 引入了 `ExceptionGroup`（PEP 654），多个并发任务可能抛出包装异常。不展开的话，日志中只看到 `ExceptionGroup: 3 exceptions` 而不知道具体是哪些异常。

---

## Q4：FastMCP Server 的 Mock 数据设计有什么工程考量？

### 标准答案

以 CLS Server 为例 (`mcp_servers/cls_server.py`)：

```python
# 所有工具通过 @mcp.tool() 注册
@mcp.tool()
@log_tool_call          # ← 统一的日志装饰器
def search_log(topic_id, start_time, end_time, ...):
    # 返回 Mock 数据
    ...
```

**工程考量：**
1. **`@log_tool_call` 装饰器** (`cls_server.py:23-69`) 提供统一的可观测性——自动记录每次工具调用的参数、结果和耗时。
2. **Mock 数据具有业务语义**——不是随机数据，而是模拟真实运维场景（时间序列数据从低到高渐变）。
3. **代码结构和真实实现一致**——替换 Mock 时只需改工具函数体，不影响接口。

### 追问 #1：`@log_tool_call` 装饰器捕获异常为什么用 `raise` 而非 `return error`？

**答案：** `cls_server.py:62-67`
```python
except Exception as e:
    logger.error(f"返回状态: ERROR")
    logger.error(f"错误信息: {str(e)}")
    raise    # ← 向上抛出
```

这使得 FastMCP 框架能感知到工具调用失败，并返回标准的 MCP Error 响应给客户端。如果改成 `return {"error": ...}`，FastMCP 会认为工具调用成功但结果包含错误字段——混淆了传输层和业务层的错误。

### 追问 #2：为什么 Monitor Server 的时间序列数据是"从低到高逐渐增长"的？

**答案：** `mcp_servers/monitor_server.py:206-224`
```python
if time_index < 3:
    cpu_value = base_cpu + (time_index * 0.5)   # 初始阶段: 10% 左右
else:
    growth_factor = (time_index - 2) * 8.5
    cpu_value = min(base_cpu + growth_factor, 96.0)  # 最终接近 96%
```

这模拟了真实故障场景——CPU 使用率在正常阶段保持低位，然后**逐渐攀升到 95%+**。如果直接生成随机数据，Agent 的诊断行为无法被验证。这种"带业务语义的 Mock"是测试 AIOps Agent 的关键。

### 追问 #3：CLS Server 中的 `search_log` 为什么根据 `topic_id` 返回不同数据？

**答案：** `cls_server.py:412-465`
```python
if topic_id == "topic-001":
    # 返回应用日志（INFO 级别）
    ...
else:
    # 返回错误：topic 不存在
    ...
```
这模拟了现实中不同 topic 返回不同日志类型的场景。Agent 需要正确处理 "topic 不存在" 的错误——这正是验证 Agent 鲁棒性的测试点。

---

## Q5：MCP 客户端和本地工具如何统一在 Agent 中使用？

### 标准答案

```python
# app/agent/aiops/executor.py:39-47
local_tools = list(DEFAULT_LOCAL_AGENT_TOOLS)       # 3个本地工具
mcp_client = await get_mcp_client_with_retry()
mcp_tools = await mcp_client.get_tools()              # N个MCP工具
all_tools = local_tools + mcp_tools                   # 统一列表
llm_with_tools = llm.bind_tools(all_tools)            # 全部绑定
```

**核心设计：** 本地工具和 MCP 工具在 `bind_tools()` 层面完全透明——LLM 不知道也不关心工具的来源。这是 MCP 协议的核心价值：**工具来源对消费者透明**。

### 追问 #1：如果本地工具和 MCP 工具同名怎么办？

**答案：** 当前没有冲突处理。`all_tools = local_tools + mcp_tools` 中如果存在同名，`bind_tools()` 可能报错或后者覆盖前者。生产环境应添加去重或名字空间前缀。

### 追问 #2：为什么 `DEFAULT_LOCAL_AGENT_TOOLS` 是 tuple 而非 list？

**答案：** `app/tools/__init__.py:27-31`
```python
DEFAULT_LOCAL_AGENT_TOOLS = (
    retrieve_knowledge,
    get_current_time,
    query_prometheus_alerts,
)
```
Tuple 不可变，防止意外修改全局工具集。每次使用时通过 `list(DEFAULT_LOCAL_AGENT_TOOLS)` 拷贝一份，因为后续会 `.extend(mcp_tools)`。

---

**MCP 协议深度面试 — 本章结束**

关键文件索引：
- `app/agent/mcp_client.py` — MCP 客户端单例 + 重试拦截器 + transport 检测
- `app/config.py:46-78` — MCP 服务器配置
- `mcp_servers/cls_server.py` — CLS 日志查询 MCP Server
- `mcp_servers/monitor_server.py` — 监控数据 MCP Server
- `app/agent/aiops/executor.py:39-51` — 本地+MCP 工具统一绑定
- `app/services/rag_agent_service.py:118-141` — 安全加载 MCP 工具
