"""tool —— 可调用工具。MVP 首个 = 偶像信息/歌词检索（经 Repository RAG 读路径）。"""

from __future__ import annotations

from typing import Any

from backend.errors import CapabilityUnavailableError

RETRIEVE_TOOL = {
    "type": "function",
    "function": {
        "name": "retrieve_idol_info",
        "description": "当用户询问偶像的背景、经历、作品、歌曲、歌词等需要真实资料的问题时调用，"
        "从偶像知识库检索有据内容。检索不到也不要编造。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要检索的问题或关键词，中文"},
                "limit": {"type": "integer", "description": "返回条数，默认 3"},
            },
            "required": ["query"],
        },
    },
}


async def retrieve(repo: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """执行检索工具。repo 需具备 search_knowledge(query, limit)。"""
    query = str(arguments.get("query", "")).strip()
    limit = int(arguments.get("limit", 3) or 3)
    if not query:
        return {"ok": False, "message": "检索查询为空"}
    try:
        results = await repo.search_knowledge(query, limit=limit)
    except Exception as exc:  # qdrant/MySQL 等瞬时故障 → 作为能力不可用交还模型
        raise CapabilityUnavailableError("偶像资料检索暂时不可用") from exc
    if not results:
        return {"ok": True, "found": False, "message": "未检索到相关偶像资料"}
    lines = [r["text"] for r in results]
    return {"ok": True, "found": True, "items": lines}
