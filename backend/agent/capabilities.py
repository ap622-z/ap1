"""能力层门面：注册与执行编排 tool / skill / mcp。

- tool（agent/tool）：可调用工具，偶像信息/歌词检索。
- skill（agent/skill）：行为指导，非可调用函数（提问反问），由 agent 注入 prompt。
- mcp（agent/mcp）：外部能力来源（联网搜索），登记为可调用工具。

本模块只负责把可调用工具（tool + mcp）的 spec 汇总给模型、按名分发执行；
skill 不经这里执行，见 skill.question_skill_trigger 的注入路径。
"""

from __future__ import annotations

from typing import Any

from backend.agent import mcp as _mcp
from backend.agent import tool as _tool
from backend.errors import CapabilityUnavailableError


class CapabilityRunner:
    """执行可调用工具。实现与模型侧 function calling 同构。"""

    def __init__(self, repo: Any, search: Any) -> None:
        self._repo = repo
        self._search = search

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        return [_tool.RETRIEVE_TOOL, _mcp.WEB_SEARCH_TOOL]

    async def run(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "retrieve_idol_info":
            return await _tool.retrieve(self._repo, arguments)
        if name == "web_search":
            return await _mcp.web_search(self._search, arguments)
        raise CapabilityUnavailableError(f"未知工具: {name}")
