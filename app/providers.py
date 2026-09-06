"""外部能力提供方（协议门面）。

- LLMProvider：DeepSeek / OpenAI 兼容 chat completions（tool/skill 的模型侧）。
- WebSearchProvider：Tavily 兼容 /search（mcp 联网搜索）。
两者都做成「可指向本地 stub 的 HTTP 客户端」，使端到端测试不依赖外部密钥仍可跑通
同一条主链路；生产环境把 base_url / api_key 指向真实服务即可。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import Settings


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
class LLMResult:
    def __init__(self, content: str, tool_calls: list[dict[str, Any]], finish_reason: str = ""):
        self.content = content
        self.tool_calls = tool_calls
        self.finish_reason = finish_reason


class LLMProvider:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        base = settings.llm_base_url.rstrip("/")
        # DeepSeek 的 OpenAI 兼容路径是 /chat/completions
        if base.endswith("/v1"):
            self._chat_url = f"{base}/chat/completions"
        else:
            self._chat_url = f"{base}/v1/chat/completions"
        self._model = settings.llm_model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 512,
    ) -> LLMResult:
        """一次模型调用。tools 为空则模型只能返回文本。"""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.7,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self._settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self._settings.llm_api_key}"
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(self._chat_url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        choice = data["choices"][0]["message"]
        content = choice.get("content") or ""
        tool_calls_raw = choice.get("tool_calls") or []
        tool_calls: list[dict[str, Any]] = []
        for tc in tool_calls_raw:
            fn = tc.get("function", {})
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(
                {
                    "id": tc.get("id") or "",
                    "name": fn.get("name", ""),
                    "arguments": arguments,
                }
            )
        return LLMResult(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason") or data.get("choices", [{}])[0].get("finish_reason", ""),
        )


# ---------------------------------------------------------------------------
# 联网搜索（mcp）
# ---------------------------------------------------------------------------
class WebSearchProvider:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._url = f"{settings.search_base_url.rstrip('/')}/search"

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if self._settings.search_api_key:
            headers["Authorization"] = f"Bearer {self._settings.search_api_key}"
        payload = {"query": query, "max_results": max_results, "include_answer": False}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(self._url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        results = data.get("results", [])
        return [{"title": r.get("title", ""), "content": r.get("content", ""), "url": r.get("url", "")} for r in results]
