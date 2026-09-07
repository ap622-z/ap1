"""Worker —— 一条消息从「被收到」到「回复就绪」的全部编排。

它只把 scope + 最新用户消息交给 Agent；上下文组装/压缩/检索在 Agent 内部经 Repository。
工具记录与回复在结束事务内由 worker 统一落库（与置 replied 同批）。
幂等：同会话同 client_message_id 命中已有结果则复用，绝不重复执行 Agent。
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.agent.agent import Agent
from backend.errors import IdempotencyConflictError, ProcessingFailedError
from backend.logging_setup import get_logger
from backend.repository.records import MessageRow
from backend.repository.repository import Repository
from backend.repository.scope import Scope


@dataclass
class MessageOutcome:
    user_message: MessageRow
    reply: MessageRow
    reused: bool = False


class Worker:
    def __init__(self, repo: Repository, agent: Agent) -> None:
        self._repo = repo
        self._agent = agent

    async def handle(
        self, scope: Scope, text: str, client_message_id: str
    ) -> MessageOutcome:
        """幂等 + 生命周期 + 调 agent + 结束事务落库。调用方须已持有本会话 mailbox 锁。"""
        log = get_logger("worker", scope=scope.as_dict())
        await self._repo.assert_session_owned(scope)

        existing = await self._repo.find_user_message_by_client_id(scope, client_message_id)
        if existing is not None:
            reply = await self._repo.get_agent_reply_of(scope, existing.id)
            if reply is not None:
                log.info("idempotent_reuse", message_id=existing.id)
                return MessageOutcome(user_message=existing, reply=reply, reused=True)
            # 存在但尚无回复 → 上一次处理中断，本次视为重试续跑（同内容）
            if (existing.payload.get("text") or "") != text:
                raise IdempotencyConflictError(
                    "同 client_message_id 已存在且内容不同，拒绝重复投递",
                    scope=scope.as_dict(),
                )
            user_message = existing
            log.info("message_retry_resume", message_id=existing.id)
        else:
            user_message = await self._repo.append_user_message(scope, text, client_message_id)
            log.info("message_received", message_id=user_message.id, seq=user_message.seq)

        await self._repo.mark_processing(scope, user_message.id)
        try:
            result = await self._agent.run(scope, user_message)
        except Exception:
            # 处理中途失败：消息保持可见（回退到 received），供同一 client_message_id 重发续跑
            log.error("message_processing_failed", message_id=user_message.id, exc_info=True)
            await self._repo.revert_to_received(scope, user_message.id)
            raise ProcessingFailedError(
                "消息处理中途失败，请稍后重发", scope=scope.as_dict()
            ) from None

        reply = await self._repo.finalize_message(
            scope, user_message.id, result.reply_text, result.tool_records
        )
        log.info("message_replied", message_id=user_message.id, reply_id=reply.id)
        return MessageOutcome(user_message=user_message, reply=reply, reused=False)
