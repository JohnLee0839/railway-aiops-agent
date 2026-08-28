# RailOps Agent — 完整文档导航

> SuperBizAgent v2.0.0 — 事件驱动 AIOps 多智能体运维框架

---

## 📂 文档结构

```
docs/
├── README.md                                    # ← 本文件
│
├── architecture/                                # 架构分析
│   ├── 00-项目架构理解报告.md                     # 完整架构分析 (15 个维度)
│   └── 01-Code-Review审查报告.md                 # Google 标准代码审查
│
└── interview/                                   # Agent 面试宝典 (8 册)
    ├── 01-LangGraph深度面试.md                   # LangGraph 框架深度
    ├── 02-Multi-Agent架构深度面试.md              # 多 Agent 架构
    ├── 03-Workflow与State设计深度面试.md          # Workflow + State Machine
    ├── 04-Prompt工程深度面试.md                   # 7 个核心 Prompt
    ├── 05-RAG-Embedding-Milvus深度面试.md         # RAG 全链路
    ├── 06-MCP协议深度面试.md                      # MCP 协议实现
    ├── 07-FastAPI-SSE-生产环境深度面试.md         # API 层 + 生产就绪
    └── 08-系统设计与Leader面试.md                 # 系统设计 + Leader 视角
```

---

## 🚀 快速开始

### 阅读顺序建议

| 读者角色 | 推荐顺序 |
|---------|---------|
| **面试候选人** | 先读 interview/ → 按册号顺序阅读 |
| **新入职工程师** | 先读 architecture/00-项目架构理解报告.md → 再按需读 interview/ |
| **Tech Lead/架构师** | architecture/01-Code-Review审查报告.md → interview/08-系统设计与Leader面试.md |
| **Prompt Engineer** | interview/04-Prompt工程深度面试.md |
| **Agent 开发者** | interview/01 + 02 + 03 |

---

## 📊 项目统计

| 指标 | 数量 |
|------|------|
| Python 源文件 | 50+ |
| Agent 数量 | 5（新链路） + 3（旧链路节点） |
| Pydantic 模型 | 25+ |
| MCP Server | 2 (CLS + Monitor) |
| API 端点 | 12 |
| 状态机状态 | 9 |
| Prompt 模板 | 7 |
| 工具数量 | 3 本地 + 7 MCP + 8 Mock |

---

## 🔧 发现的 Bug (待修复)

1. **两个 IncidentRouter 并存** — `agents/incident_router.py` 和 `core/incident_router.py`
2. **`asyncio.get_event_loop()` 已废弃** — `action_orchestrator.py:408`
3. **补偿异常未捕获** — `core/incident_router.py:365` 附近
4. **`asyncio.ensure_future()` 应用 `create_task`** — `audit_store.py:115`

---

## 📝 技术栈

- **框架**: FastAPI + LangChain + LangGraph
- **LLM**: 阿里云 DashScope (通义千问 qwen-max)
- **Embedding**: text-embedding-v4 (1024 维)
- **向量库**: Milvus 2.5.10 (IVF_FLAT)
- **协议**: MCP (Model Context Protocol)
- **日志**: Loguru (轮转 + 压缩)
- **部署**: Docker Compose (Milvus etcd+minio+standalone+attu)

---

_文档生成时间: 2026-07-02 | 基于全量源码分析_
