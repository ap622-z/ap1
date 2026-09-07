"""集中配置。所有运行参数经环境变量 / .env 注入，密钥不进代码、不进文档。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根 .env：config 现位于 backend/config/，向上两级即仓库根
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 运行形态 ----
    env: str = "dev"  # dev / prod：隔离由独立数据库实例承载，不落表列
    api_prefix: str = "/api"
    log_level: str = "INFO"
    log_file: str | None = None  # 结构化日志另落一份文件（观察/测试用）

    # ---- MySQL（事实源）----
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "ap1"
    mysql_password: str = "ap1"
    mysql_db: str = "ap1"
    mysql_pool_size: int = 10
    mysql_max_overflow: int = 20

    # ---- qdrant（偶像知识 / 歌词向量）----
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_collection: str = "idol_knowledge"

    # ---- redis（MVP 仅拉起、无业务消费者）----
    redis_url: str = "redis://127.0.0.1:6379/0"

    # ---- 模型（DeepSeek / OpenAI 兼容 chat）----
    llm_base_url: str = "https://api.deepseek.com"  # OpenAI 兼容
    llm_api_key: str = ""
    llm_model: str = "deepseek-v4-flash"
    llm_max_tokens: int = 1024

    # ---- 联网搜索 mcp（Tavily，API 兼容）----
    search_base_url: str = "https://api.tavily.com"
    search_api_key: str = ""

    # ---- 向量化（嵌入）----
    # local：离线确定性字符 n-gram（默认，供开发 / 测试 / 无密钥时使用）
    # openai_compatible：指向 OpenAI 兼容 /embeddings 端点（如硅基流动 SiliconFlow，模型 bge-m3）
    embed_provider: str = "local"
    embed_model: str = ""  # 例：BAAI/bge-m3
    embed_base_url: str = ""  # 例：https://api.siliconflow.cn/v1
    embed_api_key: str = ""
    embed_dim: int = 1024  # local 提供方的本地向量维度；远程维度由首个请求自动探测
    embed_max_input_chars: int = 6000  # 超长文本截断上限，避免超模型 token 上限

    # ---- 上下文窗口与压缩 ----
    window_token_budget: int = 6000  # summary + 边界后全部轮次的总预算
    keep_recent_turns: int = 10  # 最近 N 轮永不压缩
    max_turns_in_window: int = 400

    # ---- Agent 执行护栏 ----
    max_run_steps: int = 8
    token_estimate_per_char: float = 0.6  # 中文为主内容的粗略估算系数

    # ---- 其它 ----
    register_nickname_prefix: str = "星辰"
    auth_token_ttl_days: int = 365
    seed_on_startup: bool = True  # 容器启动时若知识库为空自动摄入素材，开箱即用


@lru_cache
def get_settings() -> Settings:
    return Settings()
