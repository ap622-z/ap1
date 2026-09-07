"""能力层：tool / skill / mcp 三形态。

- tool：可调用单元。MVP 首个 = 偶像信息检索（经 repository RAG 读路径）。
- skill：行为指导，非可调用函数。MVP 首个 = 提问（含糊时反问澄清），以指导文本注入当次 prompt。
- mcp：外部能力来源协议。MVP 首个 = 联网搜索（Tavily 兼容），在能力层登记为可调用工具，
  与本地 tool 同接口形态（日后可挂更多 server）。
"""

from __future__ import annotations

import re
from typing import Any

from backend.errors import CapabilityUnavailableError
from backend.providers import WebSearchProvider
from backend.repository import Repository

# ---------------------------------------------------------------------------
# 技能（skill）：提问 —— 行为指导而非函数
# ---------------------------------------------------------------------------
_VAGUE_MARKERS = ("他", "她", "那个", "这个", "帮我", "你说", "怎么样", "怎么办", "什么", "吗", "呢")


QUESTION_SKILL_TAG = "[question-skill]"


def question_skill_trigger(user_text: str, context_rounds: int) -> tuple[bool, str]:
    """轻量规则判定是否命中「提问」skill。

    当用户表述含糊、且该信息可能影响后续理解与判断时，返回 (True, 注入的指导文本)。
    这里不追求 NLP，MVP 用信息缺失/含糊信号：
    - 本会话还没有任何先例（第一句）且句子很短、以代词/疑问收尾 → 极可能缺指代；
    - 提问句式但主语缺失（以“她/他/那个”开头）。
    命中即把带 tag 的指导注入当次 system prompt（tag 供观测/测试确认注入与否）。
    """
    text = (user_text or "").strip()
    if not text:
        return False, ""
    is_first = context_rounds == 0
    starts_pronoun = re.match(r"^(他|她|它|那个|这个人|这个事)", text) is not None
    short_vague = len(text) <= 12 and any(m in text for m in ("什么", "吗", "呢", "怎么样"))
    if (is_first and (starts_pronoun or short_vague)) or starts_pronoun:
        guidance = (
            f"{QUESTION_SKILL_TAG} 若用户的表述含糊、缺关键信息"
            "（如只用了“他/她/那个”，或问题缺少必要背景），先自然地反问澄清，"
            "不要自行假设关键信息；澄清后再继续。"
        )
        return True, guidance
    return False, ""


# ---------------------------------------------------------------------------
# 可调用工具描述
# ---------------------------------------------------------------------------
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


class CapabilityRunner:
    """执行可调用工具。实现与模型侧 function calling 同构。"""

    def __init__(self, repo: Repository, search: WebSearchProvider) -> None:
        self._repo = repo
        self._search = search

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        return [RETRIEVE_TOOL, WEB_SEARCH_TOOL]

    async def run(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "retrieve_idol_info":
            return await self._retrieve(arguments)
        if name == "web_search":
            return await self._web_search(arguments)
        raise CapabilityUnavailableError(f"未知工具: {name}")

    async def _retrieve(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip()
        limit = int(arguments.get("limit", 3) or 3)
        if not query:
            return {"ok": False, "message": "检索查询为空"}
        try:
            results = await self._repo.search_knowledge(query, limit=limit)
        except Exception as exc:  # qdrant/MySQL 等瞬时故障 → 作为能力不可用交还模型
            raise CapabilityUnavailableError("偶像资料检索暂时不可用") from exc
        if not results:
            return {"ok": True, "found": False, "message": "未检索到相关偶像资料"}
        lines = [r["text"] for r in results]
        return {"ok": True, "found": True, "items": lines}

    async def _web_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return {"ok": False, "message": "搜索查询为空"}
        try:
            results = await self._search.search(query)
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
