"""SHA-256 工具：内容指纹。"""

from __future__ import annotations

import hashlib


def sha256_hex(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
