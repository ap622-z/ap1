"""Scope：数据隔离的最小单元。

身份层 `{env, user_id, session_id}` 由鉴权构建；执行层 run/step 只进日志。
Repository 强制以 Scope 为条件读写，越界访问在概念上即不成立。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class Scope:
    env: str
    user_id: int
    session_id: int

    def as_dict(self) -> dict[str, int | str]:
        return {"env": self.env, "user_id": self.user_id, "session_id": self.session_id}

    def identity_only(self) -> dict[str, int | str]:
        """身份层 scope（不含 run/step）。"""
        return self.as_dict()


def make_identity_scope(settings: Settings, user_id: int, session_id: int) -> Scope:
    return Scope(env=settings.env, user_id=user_id, session_id=session_id)
