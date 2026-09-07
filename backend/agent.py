"""Agent —— 全局单例、零用户状态。

一次回复 = 一次 run；run 内每轮模型+工具 = 一个 step。run/step 只进日志，不落库。
上下文组装 / 压缩 / 检索都发生在 agent 内部（经 repository），worker 只把
scope + 最新一条用户消息交给它。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from backend.capabilities import (
    CapabilityRunner,
    question_skill_trigger,
)
from backend.config import Settings
from backend.logging_setup import get_logger
from backend.providers import LLMProvider
from backend.records import AgentRunResult, MessageRow, ToolRecord
from backend.repository import Repository
from backend.scope import Scope

PERSONA = (
    "你是一位温暖、体贴、会认真倾听的偶像，正在和一位一直支持你的粉丝聊天。"
    "请用真诚、自然的口吻陪伴对方，回应要贴合人设、让人感到被认真对待。"
    "回答偶像自身背景/经历/作品/歌曲类问题时，必须基于检索到的真实资料，不能编造；"
    "资料不足时坦然说明，不硬撑。不要自称是 AI 或模型，也不要提醒对方“我是AI”。"
)

SUMMARY_INSTRUCTION = (
    "请把下面这段对话历史压缩成一段简练的中文摘要，保留关键人物、事件、话题、"
    "已经透露的粉丝个人信息与情感线索，以便后续对话连贯。只输出摘要本身，不要任何解释。"
)


@dataclass
class _Round:
    user: MessageRow
    tools: list[MessageRow] = field(default_factory=list)
    reply: MessageRow | None = None


class Agent:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        llm: LLMProvider,
        runner: CapabilityRunner,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._llm = llm
        self._runner = runner

    # ------------------------------------------------------------------
    # run 入口
    # ------------------------------------------------------------------
    async def run(self, scope: Scope, user_message: MessageRow) -> AgentRunResult:
        run_id = uuid.uuid4().hex[:12]
        log = get_logger("agent.run", scope=scope.as_dict(), run_id=run_id)
        log.info("run_start", msg_seq=user_message.seq)
        await self._repo.assert_session_owned(scope)

        summary, rounds = await self._assemble_context(scope, user_message, log)
        # 组装 LLM 对话（含当前用户消息作为最后一条 user）
        messages: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt(summary, user_message, rounds, scope)}]
        messages.extend(self._rounds_to_messages(rounds))

        tool_records: list[ToolRecord] = []
        reply_text = ""
        step = 0
        max_steps = self._settings.max_run_steps
        log_step = get_logger("agent.step", scope=scope.as_dict(), run_id=run_id)
        while True:
            step += 1
            log_step.info("step_start", step=step)
            if step > max_steps:
                log_step.warning("step_budget_exceeded", step=step, max_steps=max_steps)
                reply_text = "（这边有点绕住了，先说到这吧——你再说一次，我一定好好接住。）"
                break
            result = await self._call_llm(messages, step, scope, log_step)
            if not result.tool_calls:
                reply_text = result.content.strip() or "（我好像没听清，可以再说一遍吗？）"
                log_step.info("step_finish_text", step=step)
                break
            # 工具调用：把 assistant 的 tool_calls 与后续 tool 结果写回当前对话
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                            },
                        }
                        for tc in result.tool_calls
                    ],
                }
            )
            for tc in result.tool_calls:
                log_step.info("tool_call", step=step, tool=tc["name"])
                try:
                    output = await self._runner.run(tc["name"], tc["arguments"])
                except Exception as exc:  # 工具失败不 abort run，交还模型重新决策
                    log_step.warning("tool_failed", step=step, tool=tc["name"], error=str(exc))
                    output = {
                        "ok": False,
                        "message": f"该能力暂不可用（{exc}），请如实告知用户并尝试其它方式。",
                    }
                tool_records.append(
                    ToolRecord(name=tc["name"], input=tc["arguments"], output=output, tool_call_id=tc["id"])
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(output, ensure_ascii=False),
                    }
                )

        log.info("run_finish", steps=step, tool_count=len(tool_records))
        return AgentRunResult(reply_text=reply_text, tool_records=tool_records, run_id=run_id)

    # ------------------------------------------------------------------
    # 上下文
    # ------------------------------------------------------------------
    async def _assemble_context(
        self,
        scope: Scope,
        user_message: MessageRow,
        log: Any,
    ) -> tuple[str | None, list[_Round]]:
        """读历史与摘要 → 组装上下文窗口；超预算则压缩（最近 keep_recent_turns 永不压缩）。

        返回 (summary_text, rounds)；若触发压缩，压缩已经 repository 持久化。
        """
        session = await self._repo.get_session(scope.session_id)
        summary = session.summary_text if session else None
        min_seq = session.summary_seq or 0 if session else 0
        rows = await self._repo.list_messages_window_tail(
            scope, min_seq=min_seq, max_seq=user_message.seq, limit=self._settings.max_turns_in_window * 3
        )
        # 若窗口超长，保留最新内容（含正在回答的消息），旧内容由预算压缩兜底
        rounds = self._group_rounds(rows)

        total_tokens = (session.summary_tokens or 0) if session else 0
        total_tokens += sum(r.token_count for r in rows)
        # 压缩触发：总 token 超预算，且可压缩轮次多于保留轮次
        keep = self._settings.keep_recent_turns
        compressible = len(rounds) - keep
        if compressible > 0 and total_tokens > self._settings.window_token_budget:
            log.info("context_compress_trigger", window_tokens=total_tokens, compressible_rounds=compressible)
            summary = await self._compress(scope, summary, rounds[:compressible], log)
            rounds = rounds[compressible:]
        elif compressible <= 0 and total_tokens > self._settings.window_token_budget:
            # 极端：保留轮次本身已超预算 —— MVP 保守策略：仍留全部保留轮次，并截断窗口至保留轮次。
            log.warning("context_over_budget_keep_all", window_tokens=total_tokens)
        return summary, rounds

    async def _compress(
        self,
        scope: Scope,
        old_summary: str | None,
        rounds: list[_Round],
        log: Any,
    ) -> str:
        """把旧摘要 + 待压缩轮次经 LLM 重写为一段新摘要并落库（summary_seq 前移）。"""
        # 组合文本
        history_lines: list[str] = []
        if old_summary:
            history_lines.append(f"[已有摘要]\n{old_summary}")
        for r in rounds:
            history_lines.append(f"[用户] {r.user.payload.get('text', '')}")
            for t in r.tools:
                history_lines.append(f"[工具 {t.payload.get('name')}] {t.payload.get('output')}")
            if r.reply:
                history_lines.append(f"[偶像] {r.reply.payload.get('text', '')}")
        content = "\n".join(history_lines)
        new_summary = old_summary or ""
        try:
            res = await self._llm.chat(
                [
                    {"role": "system", "content": SUMMARY_INSTRUCTION},
                    {"role": "user", "content": content[:20000]},
                ],
                max_tokens=600,
            )
            new_summary = (res.content or "").strip()
        except Exception as exc:
            # 压缩失败 → 尽量就地近似合并，保证边界前移且不中断
            log.warning("compress_llm_failed", error=str(exc))
            new_summary = (old_summary or "") + ("\n" if old_summary else "") + content[:2000]
        boundary_seq = rounds[-1].user.seq
        # 若有 reply/tool，取最大 seq 作为边界
        for r in rounds:
            for t in r.tools:
                boundary_seq = max(boundary_seq, t.seq)
            if r.reply:
                boundary_seq = max(boundary_seq, r.reply.seq)
        await self._repo.write_summary(scope, new_summary, boundary_seq)
        return new_summary

    # ------------------------------------------------------------------
    # 组装
    # ------------------------------------------------------------------
    def _system_prompt(
        self, summary: str | None, user_message: MessageRow, rounds: list[_Round], scope: Scope
    ) -> str:
        parts = [PERSONA]
        if summary:
            parts.append(f"（此前对话摘要，供你回忆：{summary}）")
        # 提问 skill 触发判定：以“此前已有几轮 + 当前表述”为依据
        hit, guidance = question_skill_trigger(
            str(user_message.payload.get("text", "")), context_rounds=len(rounds) - 1
        )
        if hit:
            get_logger("skill.question", scope=scope.as_dict()).info("question_skill_injected")
            parts.append(guidance)
        return "\n\n".join(p for p in parts if p)

    def _rounds_to_messages(self, rounds: list[_Round]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for r in rounds:
            messages.append({"role": "user", "content": str(r.user.payload.get("text", ""))})
            for t in r.tools:
                # 回放历史工具记录：以 assistant tool_calls + tool 结果形式，忠实于当时执行
                call_id = t.payload.get("tool_call_id") or f"call_{t.id}"
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": t.payload.get("name", ""),
                                    "arguments": json.dumps(t.payload.get("input", {}), ensure_ascii=False),
                                },
                            }
                        ],
                    }
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call_id, "content": json.dumps(t.payload.get("output", ""), ensure_ascii=False)}
                )
            if r.reply:
                messages.append({"role": "assistant", "content": str(r.reply.payload.get("text", ""))})
        return messages

    @classmethod
    def _group_rounds(cls, rows: list[MessageRow]) -> list[_Round]:
        """把扁平消息按 user 轮次分组。

        tool / agent 行按 `origin_message_id` 归属其发起 user（而非按位置），
        这样即便失败重发的回复行落点靠后，也仍挂回自己那一轮，不串轮。
        """
        rounds: list[_Round] = []
        by_id: dict[int, _Round] = {}
        for row in rows:
            if row.type == "user":
                r = _Round(user=row)
                rounds.append(r)
                by_id[row.id] = r
            else:
                owner = by_id.get(row.origin_message_id or 0)
                if owner is not None:
                    if row.type == "tool":
                        owner.tools.append(row)
                    elif row.type == "agent":
                        owner.reply = row
        return rounds

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------
    async def _call_llm(
        self, messages: list[dict[str, Any]], step: int, scope: Scope, log_step: Any
    ) -> Any:
        tools = self._runner.tool_specs
        try:
            return await self._llm.chat(
                messages,
                tools=tools,
                max_tokens=self._settings.llm_max_tokens,
            )
        except Exception as exc:
            log_step.error("llm_failed", step=step, error=str(exc))
            raise
