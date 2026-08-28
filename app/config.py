"""配置管理模块

使用 Pydantic Settings 实现类型安全的配置管理
"""

from typing import Dict, Any
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 应用配置
    app_name: str = "SuperOpsAgent"
    app_version: str = "1.0.0"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 9900

    # DashScope 配置
    dashscope_api_key: str = ""  # 默认空字符串，实际使用需从环境变量加载
    dashscope_model: str = "qwen-max"
    dashscope_embedding_model: str = "text-embedding-v4"  # v4 支持多种维度（默认 1024）

    # Milvus 配置
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_timeout: int = 10000  # 毫秒

    # RAG 配置
    rag_top_k: int = 3
    rag_model: str = "qwen-max"  # 使用快速响应模型，不带扩展思考

    # 文档分块配置
    chunk_max_size: int = 800
    chunk_overlap: int = 100

    # MCP 服务配置（transport: stdio | sse | streamable-http）
    # 腾讯云托管 MCP 的 URL 通常含 /sse/，需使用 sse；本地 FastMCP 使用 streamable-http
    mcp_cls_transport: str = "streamable-http"
    mcp_cls_url: str = "http://localhost:8003/mcp"
    mcp_monitor_transport: str = "streamable-http"
    mcp_monitor_url: str = "http://localhost:8004/mcp"

    # Prometheus
    prometheus_base_url: str = "http://127.0.0.1:9090"
    prometheus_request_timeout: float = 10.0

    # ML attack detector configuration
    # Supported backends: zl | rule | mock.
    ml_attack_detector_backend: str = "zl"
    zl_model_root: str = "../ZL"
    zl_model_version: str = "V2"
    zl_model_path: str = ""
    zl_model_manifest_path: str = "metadata/manifests/v2_compact_tree_manifest.json"
    zl_confidence_threshold: float = 0.0
    zl_detector_fallback: str = "rule"

    # AIOps 事件驱动配置
    mock_failure_rate: float = 0.2  # Mock 动作故障注入概率
    dedup_window_seconds: float = 10.0  # 去重滑动窗口大小
    dedup_threshold: int = 3  # 去重阈值
    max_retry_cycles: int = 3  # 最大重试轮次
    approval_timeout_minutes: int = 10  # 人工审批超时（分钟）
    default_action_timeout_seconds: float = 30.0  # 默认动作超时
    circuit_breaker_threshold: int = 5  # 熔断器失败阈值
    circuit_breaker_recovery_seconds: float = 30.0  # 熔断恢复时间

    @property
    def mcp_servers(self) -> Dict[str, Dict[str, Any]]:
        """获取完整的 MCP 服务器配置"""
        return {
            "cls": {
                "transport": self.mcp_cls_transport,
                "url": self.mcp_cls_url,
            },
            "monitor": {
                "transport": self.mcp_monitor_transport,
                "url": self.mcp_monitor_url,
            }
        }


# 全局配置实例
config = Settings()
