"""身份凭据工具：令牌生成 + SHA-256 存储。

库中只存令牌哈希；明文令牌仅在注册时返回一次。
"""

from __future__ import annotations

import hashlib
import secrets


def generate_token() -> str:
    """生成一次性的持有型登录令牌（URL-safe 随机串）。"""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """令牌 → SHA-256 hex（库中仅存此值，登录按哈希反查）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_nickname(prefix: str) -> str:
    """生成一个大概率唯一的昵称（注册昵称的兜底/默认值）。"""
    suffix = secrets.token_hex(3)
    return f"{prefix}{suffix}"
