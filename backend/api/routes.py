"""HTTP 层：身份 / 发消息 / 读历史 + 健康检查。只做协议与身份，不碰业务。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from backend.api.deps import require_scope
from backend.api.security import generate_nickname, generate_token, hash_token
from backend.errors import UnauthorizedError
from backend.repository.scope import Scope

router = APIRouter()


class RegisterIn(BaseModel):
    nickname: str | None = Field(default=None, max_length=24)


class LoginIn(BaseModel):
    nickname: str
    token: str


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    client_message_id: str = Field(min_length=1, max_length=64)


@router.post("/auth/register")
async def register(body: RegisterIn, request: Request):
    runtime = request.app.state.runtime
    settings = runtime.settings
    token = generate_token()
    token_hash = hash_token(token)
    # 昵称：未提供则自动生成；已占用则加随机后缀重试
    base = (body.nickname or "").strip() or generate_nickname(settings.register_nickname_prefix)
    nickname = base
    for _ in range(20):
        if await runtime.repo.get_user_by_nickname(nickname) is None:
            break
        nickname = f"{base}_{generate_nickname('')[-6:]}"
    user = await runtime.repo.create_user(nickname, token_hash)
    await runtime.repo.touch_login(user.id)
    # 建立唯一会话（一用户一会话，结构保证）
    session = await runtime.repo.get_or_create_session(user.id)
    return {"nickname": nickname, "token": token, "user_id": user.id, "session_id": session.id}


@router.post("/auth/login")
async def login(body: LoginIn, request: Request):
    runtime = request.app.state.runtime
    user = await runtime.repo.get_user_by_nickname(body.nickname)
    if user is None or user.status != 1 or user.token_hash != hash_token(body.token):
        raise UnauthorizedError("昵称或令牌不正确")
    await runtime.repo.touch_login(user.id)
    await runtime.repo.get_or_create_session(user.id)
    return {"ok": True, "nickname": user.nickname}


@router.post("/messages")
async def send_message(
    body: MessageIn,
    request: Request,
    scope: Scope = Depends(require_scope),
):
    runtime = request.app.state.runtime
    async with runtime.mailbox.run(scope.session_id):
        outcome = await runtime.worker.handle(scope, body.text, body.client_message_id)
    return {
        "user_message_id": outcome.user_message.id,
        "client_message_id": body.client_message_id,
        "reply": {
            "message_id": outcome.reply.id,
            "text": outcome.reply.payload.get("text", ""),
        },
        "reused": outcome.reused,
    }


@router.get("/messages")
async def list_messages(
    request: Request,
    scope: Scope = Depends(require_scope),
    after_seq: int | None = None,
    limit: int = 200,
):
    runtime = request.app.state.runtime
    limit = min(max(limit, 1), 500)
    repo = runtime.repo
    rows = await repo.list_messages_since(
        scope, min_seq=after_seq if after_seq is not None else 0, limit=limit + 1
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "messages": [
            {
                "id": r.id,
                "seq": r.seq,
                "type": r.type,
                "payload": r.payload,
                "status": r.status,
                "client_message_id": r.client_message_id,
                "origin_message_id": r.origin_message_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "has_more": has_more,
        "next_seq": rows[-1].seq if rows else (after_seq or 0),
    }


@router.get("/health")
async def health(request: Request):
    return {"status": "ok", "env": request.app.state.runtime.settings.env}
