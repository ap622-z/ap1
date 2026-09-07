"""启动期配置校验：真实运行形态（非 test）下，缺必要 key/模型则阻止启动。

无密钥时的默认行为是「显式降级或阻止启动」，绝不静默带病运行：
- 嵌入（openai_compatible）缺 key/model/base_url → 阻止（否则探测失败后检索全废）；
- LLM / 搜索缺 key → 阻止（真实对话/联网不可用也属带病）。
显式降级走 EMBED_PROVIDER=local（离线嵌入）、SEED_ON_STARTUP=false（关自动灌入）。
"""

from __future__ import annotations

from backend.config.settings import Settings


def collect_startup_problems(settings: Settings) -> list[str]:
    """返回缺失的必需配置项（环境变量名）。为空 = 可启动。"""
    missing: list[str] = []
    if settings.embed_provider == "openai_compatible":
        for var, val in (
            ("EMBED_API_KEY", settings.embed_api_key),
            ("EMBED_MODEL", settings.embed_model),
            ("EMBED_BASE_URL", settings.embed_base_url),
        ):
            if not val:
                missing.append(var)
    if not settings.llm_api_key:
        missing.append("LLM_API_KEY")
    if not settings.search_api_key:
        missing.append("SEARCH_API_KEY")
    return missing
