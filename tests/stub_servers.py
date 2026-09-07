"""E2E 测试的外部桩服务：模型 / 联网搜索。

它们跑在真实 HTTP 端口上，说同一种协议（OpenAI 兼容 chat、Tavily 兼容 /search），
App 内无任何测试专用分支——测试把 base_url 指向这些本地桩即可驱动同一主链路。
桩的“行为”是确定性的，模拟一个会把偶像问题拿去检索、把检索结果落到回答里的模型。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

RETRIEVE = "retrieve_idol_info"
WEB = "web_search"
SKILL_TAG = "[question-skill]"
_COMPRESS_MARK = "压缩成一段简练的中文摘要"
_VAGUE_WORDS = ("他", "她", "那个", "这个", "帮我", "怎样", "怎么样", "怎么", "吗", "呢")


class ModelBrain:
    """决定桩模型“想做什么”。可在测试内替换策略以覆盖不同场景。"""

    def __init__(self) -> None:
        self.fail_once = False  # 置 True 后下一次 chat 请求返回 500，之后恢复（外部能力故障）
        self.malformed = False  # 置 True 后下一次 chat 返回 200 但缺 choices → 内部解析失败（处理中故障）
        self.loop_forever = False  # 置 True 后永远返回工具调用（测单 run 最大步数护栏）

    def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated LLM outage")
        messages = payload.get("messages", [])
        tools = [t.get("function", {}).get("name") for t in payload.get("tools", [])]
        system = "\n".join(m.get("content") or "" for m in messages if m.get("role") == "system")
        last_user = next(
            (m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), ""
        )
        last_tool = next(
            (m for m in reversed(messages) if m.get("role") == "tool"), None
        )
        has_tool_result = last_tool is not None

        # 0) 护栏测试：永不结束的工具循环
        if self.loop_forever and tools:
            return _tool_call(tools[0], {"query": last_user[:80]})

        # 1) 压缩请求
        if _COMPRESS_MARK in system:
            return {"role": "assistant", "content": "（本段对话已压缩为摘要：提及了用户近况与偶像话题。）"}

        # 2) 刚拿到工具结果且尚未给出最终回答 → 用检索/搜索结果作答（有据可查）
        if has_tool_result and messages[-1].get("role") == "tool":
            content = messages[-1].get("content") or ""
            try:
                obj = json.loads(content)
            except Exception:
                obj = {}
            if obj.get("ok") and obj.get("found") and obj.get("items"):
                first = obj["items"][0]
                if isinstance(first, dict):
                    first = first.get("content") or first.get("title") or str(first)
                return {"role": "assistant", "content": f"我查到：{str(first)[:600]}"}
            return {"role": "assistant", "content": "我暂时没查到相关内容，咱们聊点别的也可以～"}

        # 3) 提问 skill：系统注入了 skill tag 且 用户表述含糊 → 先反问澄清
        if SKILL_TAG in system and _is_vague(last_user):
            return {"role": "assistant", "content": "你指的是哪一个呢？可以再多告诉我一点细节吗？"}

        # 4) 需要联网 → 调 web_search
        if WEB in tools and _needs_live(last_user):
            return _tool_call(WEB, {"query": last_user[:80]})

        # 5) 偶像/歌曲/歌词问题 → 调检索 tool（一条 user 消息只检索一次）
        if RETRIEVE in tools and not has_tool_result and _asks_fact(last_user):
            return _tool_call(RETRIEVE, {"query": last_user[:120], "limit": 3})

        # 6) 默认：正常陪伴式应答
        snippet = last_user[:40]
        return {"role": "assistant", "content": f"嗯嗯，我在认真听：{snippet}…… 我在这里陪着你。"}


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ],
    }


def _is_vague(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    return len(t) <= 10 and any(w in t for w in _VAGUE_WORDS)


def _needs_live(text: str) -> bool:
    return any(k in (text or "") for k in ("最新", "实时", "今天", "现在", "天气", "新闻"))


def _asks_fact(text: str) -> bool:
    t = (text or "").strip()
    if len(t) > 200:
        return True  # 长文本更可能含事实问题，交由检索兜底
    return any(k in t for k in ("谁", "什么", "哪", "吗", "介绍", "是", "怎么样", "?", "？")) or "连淮伟" in t


class ModelHandler(BaseHTTPRequestHandler):
    brain = ModelBrain()

    def log_message(self, *args: Any) -> None:  # 静默
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/chat/completions"):
            if self.brain.malformed:  # 内部解析失败路径：200 但无 choices
                self.brain.malformed = False
                self._send(200, {"id": "chatcmpl-malformed", "object": "chat.completion"})
                return
            try:
                choice = self.brain.decide(body)
            except RuntimeError:
                self._send(500, {"error": "simulated outage"})
                return
            resp = {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": choice, "finish_reason": "stop"}],
            }
            self._send(200, resp)
        else:
            self._send(404, {"error": "not found"})

    def _send(self, code: int, obj: dict[str, Any]) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class SearchHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        q = body.get("query", "")
        results = [
            {
                "title": "今日要闻",
                "content": f"关于“{q}”的最新信息：本地桩返回的一条实时摘要内容。",
                "url": "https://example.com/stub",
            }
        ]
        self._send(200, {"query": q, "results": results})

    def _send(self, code: int, obj: dict[str, Any]) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class StubServer:
    """在独立线程里跑一个 ThreadingHTTPServer，返回其实际端口。"""

    def __init__(self, handler_cls: type, *, reset: bool = False) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
