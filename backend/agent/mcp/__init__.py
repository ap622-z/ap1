"""mcp —— 外部能力来源协议。MVP 首个 = 联网搜索（Tavily 兼容）。

在能力层登记为可调用工具，与本地 tool 同接口形态（日后可挂更多 server）。
"""

from __future__ import annotations

from typing import Any

from backend.errors import CapabilityUnavailableError

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "当问题需要实时、最新或外部信息（如近况、新闻、天气、票务）时调用联网搜索。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要联网搜索的查询，中文"},
            },
            "required": ["query"],
        },
    },
}


async def web_search(provider: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """执行联网搜索。provider 需具备 search(query) → [{title, content, url}]。"""
    query = str(arguments.get("query", "")).strip()
    if not query:
        return {"ok": False, "message": "搜索查询为空"}
    try:
        results = await provider.search(query)
    except Exception as exc:
        raise CapabilityUnavailableError("联网搜索暂时不可用") from exc
    if not results:
        return {"ok": True, "found": False, "message": "未找到相关实时信息"}
    return {
        "ok": True,
        "found": True,
        "items": [
            {"title": r["title"], "content": r["content"], "url": r["url"]}
            for r in results
        ],
    }
