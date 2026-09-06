"""向量化（嵌入）抽象与实现。

- LocalCharNgramEmbedder：确定性、离线、零密钥——开发/测试与无外部嵌入服务时使用。
  对中文做字符 n-gram 特征哈希，余弦相似度与 n-gram 重叠一致，足够支撑 MVP RAG。
- OpenAICompatEmbedder：任接 OpenAI 兼容 /embeddings 端点（配置了 embed_* 时使用）。

qdrant 仅存偶像知识向量；用户数据永不写向量库。
"""

from __future__ import annotations

import hashlib
import math

import httpx

from app.config import Settings


class Embedder:
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


def _ngrams(text: str, n_min: int = 1, n_max: int = 3) -> list[str]:
    """对文本取字符级 n-gram 特征。中文整句做 1~3 gram 已能支撑近义词召回的基本重叠。"""
    # 归一化：统一小写、折叠空白（保留中文字符与字母数字）
    norm = "".join(ch.lower() if ch.isalnum() or "一" <= ch <= "鿿" else " " for ch in text)
    chars = "".join(norm.split())
    out: list[str] = []
    for n in range(n_min, n_max + 1):
        for i in range(len(chars) - n + 1):
            out.append(chars[i : i + n])
    return out


class LocalCharNgramEmbedder(Embedder):
    """字符 n-gram 特征哈希 + L2 归一化，维度固定可配置。确定性、离线。"""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def _vectorize(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for gram in _ngrams(text):
            h = hashlib.md5(gram.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "big") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vectorize(t) for t in texts]


class OpenAICompatEmbedder(Embedder):
    """OpenAI 兼容 embeddings 端点。"""

    def __init__(self, settings: Settings) -> None:
        self.dim = settings.embed_dim
        self._url = settings.embed_base_url.rstrip("/")
        self._model = settings.embed_model
        self._key = settings.embed_api_key

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._key or not self._model:
            raise RuntimeError("embed_* 未配置完整")
        headers = {"Authorization": f"Bearer {self._key}"}
        payload = {"model": self._model, "input": texts}
        with httpx.Client(timeout=30) as client:
            resp = client.post(f"{self._url}/embeddings", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return [item["embedding"] for item in data["data"]]


def build_embedder(settings: Settings) -> Embedder:
    if settings.embed_provider == "openai_compatible":
        return OpenAICompatEmbedder(settings)
    return LocalCharNgramEmbedder(dim=settings.embed_dim)
