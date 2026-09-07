"""token 估算：每条消息写入时估算 token_count，供上下文窗口预算与压缩触发。"""

from __future__ import annotations

import math

from backend.config import Settings


def estimate_tokens(text: str, settings: Settings) -> int:
    """粗略估算文本 token 数。

    中文为主内容的粗略系数由 config.token_estimate_per_char 控制；其它字符按单词估算。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿")
    others = len(text) - cjk
    # 中文字符按系数；非中文字符（英文/数字/符号）粗略按 4 字符 ≈ 1 token
    cjk_tokens = cjk * settings.token_estimate_per_char
    other_tokens = others / 4.0
    return max(1, math.ceil(cjk_tokens + other_tokens))
