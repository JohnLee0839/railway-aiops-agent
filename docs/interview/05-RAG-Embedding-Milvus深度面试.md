# 第五册：RAG + Embedding + Milvus 深度面试

> 基于 RailOps Agent 源码 — 文档切分、向量化、存储、检索全链路分析

---

## Q1：为什么选择 Milvus 作为向量数据库而非 FAISS 或 Chroma？

### 标准答案

```python
# app/core/milvus_client.py:44-52
class MilvusClientManager:
    COLLECTION_NAME: str = "biz"
    VECTOR_DIM: int = 1024
    ID_MAX_LENGTH: int = 100
    CONTENT_MAX_LENGTH: int = 8000
    DEFAULT_SHARD_NUMBER: int = 2
```

**核心原因（基于源码推断）：**
1. **分布式支持**：`num_shards=2` (`app/core/milvus_client.py:186`) 表明考虑了水平扩展。FAISS 是单机库，Chroma 是轻量嵌入式。
2. **生产级特性**：Milvus 通过 Docker Compose 部署（`vector-database.yml`），包含 etcd（元数据）+ minio（对象存储）+ standalone（计算），是完整分布式架构。
3. **原生 JSON 字段**：`DataType.JSON` (`app/core/milvus_client.py:170-172`) 支持富元数据存储。
4. **LangChain 集成**：`langchain_milvus.Milvus` (`app/services/vector_store_manager.py:42`) 开箱即用。

### 追问 #1：为什么 `num_shards=2` 而不是更多？

**答案：** `app/core/milvus_client.py:186`
```python
num_shards=self.DEFAULT_SHARD_NUMBER,  # = 2
```
Shard 数直接影响写入并行度和查询延迟。2 个 shard 是开发/小规模部署的默认值。生产环境根据数据量调整——每 100 万向量约需 1-2 个 shard。

### 追问 #2：为什么索引类型选择 `IVF_FLAT` 而非 `HNSW`？

**答案：** `app/core/milvus_client.py:197-201`
```python
index_params = {
    "metric_type": "L2",
    "index_type": "IVF_FLAT",
    "params": {"nlist": 128},
}
```

| 索引 | 优点 | 缺点 |
|------|------|------|
| IVF_FLAT (当前) | 构建快，内存占用低 | 查询精度略低于 HNSW |
| HNSW | 查询快，高精度 | 内存占用大，构建慢 |

`IVF_FLAT + nlist=128` 是平衡选择。`nlist=128` 意味着查询时只扫描 128 个聚类中的 `nprobe=10` 个 (`app/services/vector_search_service.py:71`)，实际扫描约 10/128 ≈ 7.8% 的数据。

### 追问 #3：为什么度量类型选 `L2`（欧氏距离）而非 `IP`（内积）？

**答案：** L2 距离对向量模长不敏感，适合 DashScope `text-embedding-v4` 的输出（该模型不保证向量归一化）。IP 要求向量归一化（单位长度），否则结果不稳定。

### 追问 #4：`CONTENT_MAX_LENGTH = 8000` 有什么后果？

**答案：** `app/core/milvus_client.py:51`
```python
CONTENT_MAX_LENGTH: int = 8000
```
超过 8000 字符的文本分片将被截断。结合 `chunk_max_size=800` 的配置（`app/config.py:42`），正常情况下不会溢出。但 `document_splitter_service.py` 的二次分割使用 `chunk_size * 2 = 1600`，仍在 8000 范围内。但如果 `_merge_small_chunks` 合并出超大片段，可能被截断。

### 追问 #5：为什么使用 `auto_id=False` + UUID 而非 Milvus 自增 ID？

**答案：** `app/services/vector_store_manager.py:46`
```python
auto_id=False,  # 使用自定义 id
```
配合 `app/services/vector_store_manager.py:79`
```python
ids = [str(uuid.uuid4()) for _ in documents]
```

使用自定义 UUID 的好处：
1. **确定性**：可以通过 ID 追踪文档来源
2. **删除支持**：`delete_by_source` (`vector_store_manager.py:95-121`) 需要先删旧数据再插入新数据，自增 ID 无法实现
3. **分布式友好**：多实例写入不冲突

---

## Q2：文档分割为什么采用两阶段分割（Markdown Header + Recursive Character）？

### 标准答案

```python
# app/services/document_splitter_service.py:22-37
# 第一阶段: Markdown 标题分割器
self.markdown_splitter = MarkdownHeaderTextSplitter(
    headers_to_split_on=[
        ("#", "h1"),
        ("##", "h2"),
        # 不再按三级标题分割，避免过度碎片化
    ],
    strip_headers=False,
)

# 第二阶段: 递归字符分割器
self.text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=self.chunk_size * 2,  # 1600
    chunk_overlap=self.chunk_overlap,
)
```

**核心原因：**
1. **第一阶段**按 `h1/h2` 标题分割→保持语义完整性（同章节内容在一起）
2. **第二阶段**按字符长度 `chunk_size*2` 二次分割→防止单个分片过大
3. **不按 `h3` 分割**→避免过度碎片化（`###` 级别的标题往往是一个段落内的小标题）

### 追问 #1：为什么 `strip_headers=False`？

**答案：** `app/services/document_splitter_service.py:28`
```python
strip_headers=False,  # 确保标题文本本身仍保留在分片内容中
```
如果设为 `True`，分割后每个 chunk 的 content 中会丢失标题文本。`strip_headers=False` 确保标题保留，使每个分片的**上下文更完整**——LLM 能知道这段内容属于哪个章节。

### 追问 #2：为什么第二阶段 `chunk_size` 是第一阶段的 2 倍（1600 vs 800）？

**答案：** `config.chunk_max_size = 800` 是第一阶段的"理想大小"。但 Markdown 按标题分割后，某些小节可能远超 800 字符。第二阶段使用 `1600` 的更大阈值，给予标题分割后的块更多的"容身空间"，减少不必要的切分。

### 追问 #3：`_merge_small_chunks` 的 `min_size=300` 是如何工作的？

**答案：** `app/services/document_splitter_service.py:134-172`
```python
def _merge_small_chunks(self, documents, min_size=300):
    for doc in documents:
        if doc_size < min_size and len(current_doc.page_content) < self.chunk_size * 2:
            current_doc.page_content += "\n\n" + doc.page_content
        else:
            merged_docs.append(current_doc)
            current_doc = doc
```
合并条件：当前分片 < 300 字符 **且** 合并后不会超过 `chunk_size * 2` (1600)。这避免了产生大量"废片"——比如一个只有一句话标题的 Markdown 段落。

### 追问 #4：如果文件是 `.txt` 而非 `.md`，分割策略有什么不同？

**答案：** `app/services/document_splitter_service.py:118-132`
```python
def split_document(self, content, file_path):
    if file_path.endswith(".md"):
        return self.split_markdown(content, file_path)  # 两阶段
    else:
        return self.split_text(content, file_path)       # 单阶段
```

`.txt` 文件直接使用 `RecursiveCharacterTextSplitter` 单阶段分割，因为纯文本没有标题结构。

### 追问 #5：为什么 `chunk_overlap=100` 而不是更大或更小？

**答案：** `app/config.py:43`
```python
chunk_overlap: int = 100
```
`800` 字符的 chunk 中 `100` 字符重叠 = 12.5%。这个比例是经验值：
- 太小（如 0）→ 分片之间语义割裂，检索时可能漏掉跨分片的关键信息
- 太大（如 400）→ 大量冗余，增加存储和 token 消耗
- 12.5% 是 RAG 社区的常见推荐值

---

## Q3：Embedding 模型为什么选择 `text-embedding-v4` + 1024 维度？

### 标准答案

```python
# app/services/vector_embedding_service.py:37-40
self.client = OpenAI(
    api_key=api_key,
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
)
self.model = model  # "text-embedding-v4"
self.dimensions = dimensions  # 1024
```

**核心原因：**
1. **阿里云生态一致性**：与 ChatQwen LLM 共享同一 API Key 和 Base URL
2. **维度灵活性**：`text-embedding-v4` 支持多种维度（`config.py:30` 注释："v4 支持多种维度（默认 1024）"）
3. **OpenAI 兼容**：通过 `openai.OpenAI` 客户端调用，可随时切换到其他兼容的 Embedding 服务

### 追问 #1：为什么通过 OpenAI 客户端而非 DashScope 原生 SDK 调用 Embedding？

**答案：** `app/services/vector_embedding_service.py:37-40` 使用 `openai.OpenAI` 而非 `dashscope.TextEmbedding`。原因：
1. **厂商无关**：`LLMFactory` (`app/core/llm_factory.py:14`) 中注释明确指出可切换多个厂商
2. **API 一致性**：所有 LLM/Embedding 调用使用相同的 OpenAI 兼容接口
3. **迁移成本低**：换 Embedding 提供商只需改 `base_url` 和 `api_key`

### 追问 #2：1024 维度是一个好的选择吗？

**答案：** 权衡：
- 更高维度（如 1536）→ 更精确但存储和检索成本更高
- 更低维度（如 512）→ 更快但可能丢失语义信息
- 1024 是中等偏高的选择，适合运维知识库（文档量不大但对精度要求高）

### 追问 #3：`_mask_api_key` 有什么安全意义？

**答案：** `app/services/vector_embedding_service.py:52-57`
```python
@staticmethod
def _mask_api_key(api_key: str) -> str:
    if len(api_key) > 8:
        return f"{api_key[:8]}...{api_key[-4:]}"
    return "***"
```
这是**日志安全**的基本实践——API Key 不应完整出现在日志中。被脱敏的 Key 显示为 `sk-9f65431...8ef6`，运维人员可以确认 Key 是否加载正确，但无法从日志中窃取。

### 追问 #4：生产环境 Embedding 服务挂了怎么降级？

**答案：** 当前没有降级策略。`DashScopeEmbeddings.embed_documents()` (`app/services/vector_embedding_service.py:89-91`) 失败会直接抛 `RuntimeError`。生产环境建议：
```python
try:
    return self.client.embeddings.create(...)
except Exception as e:
    logger.warning(f"DashScope failed, trying fallback: {e}")
    return self.fallback_client.embeddings.create(...)
```

---

## Q4：向量检索为什么有两套实现（`VectorSearchService` 和 `VectorStoreManager.similarity_search`）？

### 标准答案

两套实现服务于不同场景：

**VectorSearchService** (`app/services/vector_search_service.py:44-102`) — 原生 PyMilvus API：
```python
results = collection.search(
    data=[query_vector],
    anns_field="vector",
    param={"metric_type": "L2", "params": {"nprobe": 10}},
    limit=top_k,
    output_fields=["id", "content", "metadata"],
)
```
返回 `SearchResult` 对象，包含 `score`（L2 距离）。

**VectorStoreManager.similarity_search** (`app/services/vector_store_manager.py:132-149`) — LangChain 封装：
```python
docs = self.vector_store.similarity_search(query, k=k)
```
返回 `List[Document]`，无分数信息。

### 追问 #1：为什么 `knowledge_tool.py` 使用 `VectorStoreManager` 的 retriever 方式而非 `VectorSearchService`？

**答案：** `app/tools/knowledge_tool.py:30-33`
```python
retriever = vector_store.as_retriever(
    search_kwargs={"k": config.rag_top_k}
)
docs = retriever.invoke(query)
```
因为 `knowledge_tool` 被注册为 LangChain `@tool`，需要与 LangChain 生态兼容。`as_retriever()` 返回 `BaseRetriever` 接口，可以被 Agent 自动调用。

### 追问 #2：`nprobe=10` 的选择有什么考量？

**答案：** `app/services/vector_search_service.py:71`
```python
"params": {"nprobe": 10},
```
`nprobe` 控制 IVF_FLAT 索引查询时扫描的聚类数。`nprobe=10` + `nlist=128` = 扫描约 7.8% 的数据。这是精度与速度的平衡——更大的 `nprobe` 更精确但更慢，更小的 `nprobe` 更快但可能漏掉相关结果。

### 追问 #3：为什么 `VectorSearchService` 中有 `output_fields=["id", "content", "metadata"]` 但没有 `vector`？

**答案：** 向量数据通常不需要返回给调用者——它只用于计算距离。返回 `content` 和 `metadata` 用于构建搜索结果，`id` 用于追踪。排除 `vector` 字段可以减少网络传输量。

---

## Q5：Milvus 向量维度不匹配时的自动重建是如何实现的？

### 标准答案

```python
# app/core/milvus_client.py:102-123
if vector_field and hasattr(vector_field, 'params') and 'dim' in vector_field.params:
    existing_dim = vector_field.params['dim']
    if existing_dim != self.VECTOR_DIM:
        logger.warning(f"检测到向量维度不匹配！当前: {existing_dim}, 配置: {self.VECTOR_DIM}")
        utility.drop_collection(self.COLLECTION_NAME)
        self._create_collection()
```

**设计原因：** 切换 Embedding 模型（如从 `text-embedding-v3` 768 维升级到 `text-embedding-v4` 1024 维）会导致向量维度变化。如果不重建 Collection，插入新向量时会失败。自动检测+重建提供无缝迁移。

### 追问 #1：这个自动删除重建有什么风险？

**答案：** 最大的风险是**数据丢失**——`drop_collection` 会删除全部已索引文档。当前代码没有备份机制。生产环境应该：
1. 先创建新 collection（如 `biz_v2`）
2. 重新索引所有文档到新 collection
3. 原子切换 alias
4. 删除旧 collection

### 追问 #2：`_patch_pymilvus_milvus_client_orm_alias` 是什么问题？

**答案：** `app/core/milvus_client.py:18-41`
这是针对 `langchain_milvus` 与 `PyMilvus` ORM 连接别名不一致的**补丁**。`langchain_milvus` 创建的 `MilvusClient` 使用 `cm-{id}` 作为连接别名，但 PyMilvus ORM (`Collection`) 期望使用 `default`。补丁强制将 `_using` 设为 `"default"`，使两者一致。

---

**RAG + Embedding + Milvus 深度面试 — 本章结束**

关键文件索引：
- `app/core/milvus_client.py` — Milvus 连接管理 + 自动维度修复
- `app/services/vector_embedding_service.py` — DashScope Embeddings (OpenAI 兼容)
- `app/services/vector_store_manager.py` — Milvus VectorStore 封装
- `app/services/vector_search_service.py` — 原生 PyMilvus 检索
- `app/services/document_splitter_service.py` — 两阶段文档分割
- `app/tools/knowledge_tool.py` — RAG 工具 (content_and_artifact)
- `vector-database.yml` — Milvus Docker Compose 部署
