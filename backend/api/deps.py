"""FastAPI 依赖：运行时访问、鉴权 → Scope。"""

from __future__ import annotations

from fastapi import Depends, Header, Request

from backend.api.security import hash_token
from backend.config import Settings, get_settings
from backend.errors import UnauthorizedError
from backend.repository.scope import Scope


async def get_runtime(request: Request):
    return request.app.state.runtime


async def get_settings_dep() -> Settings:
    return get_settings()


async def require_scope(
    authorization: str | None = Header(default=None),
    runtime=Depends(get_runtime),
    settings: Settings = Depends(get_settings_dep),
) -> Scope:
    """Bearer 令牌 → 用户 → 唯一会话（惰性创建）→ 身份层 Scope。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("缺少 Bearer 令牌", scope=None)
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise UnauthorizedError("令牌为空", scope=None)
    user = await runtime.repo.get_user_by_token_hash(hash_token(token))
    if user is None or user.status != 1:
        raise UnauthorizedError("令牌无效或用户已停用", scope=None)
    session = await runtime.repo.get_or_create_session(user.id)
    return Scope(env=settings.env, user_id=user.id, session_id=session.id)
