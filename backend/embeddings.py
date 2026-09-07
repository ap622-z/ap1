"""向量化（嵌入）抽象与实现。

- LocalCharNgramEmbedder：确定性、离线、零密钥——开发/测试与无外部嵌入服务时使用。
  对中文做字符 n-gram 特征哈希，余弦相似度与 n-gram 重叠一致，足够支撑 MVP RAG。
- OpenAICompatEmbedder：任接 OpenAI 兼容 /embeddings 端点（默认指向硅基流动 SiliconFlow，
  其 BGE 系列与 Qwen3-Embedding 均 OpenAI 兼容）。真实输出维度以首个请求探测为准，
  批量提交、自动按 index 归位，避免逐行远程调用。

qdrant 仅存偶像知识向量；用户数据永不写向量库。
"""

from __future__ import annotations

import hashlib
import math

import httpx

from backend.config import Settings


class Embedder:
    """嵌入提供方接口。"""

    #: 每个文本允许的最大输入长度（字符）。None=不截断。
    max_input_chars: int | None = None

    async def ensure_dim(self) -> int:
        """返回该提供方实际输出向量的维度（远程以一次探测请求确定）。"""
        raise NotImplementedError

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化，返回与 texts 同序的向量列表。"""
        raise NotImplementedError

    def _maybe_truncate(self, texts: list[str]) -> list[str]:
        if not self.max_input_chars:
            return texts
        return [t[: self.max_input_chars] for t in texts]


def _ngrams(text: str, n_min: int = 1, n_max: int = 3) -> list[str]:
    """对文本取字符级 n-gram 特征。"""
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
        self._dim = dim

    async def ensure_dim(self) -> int:
        return self._dim

    def _vectorize(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for gram in _ngrams(text):
            h = hashlib.md5(gram.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "big") % self._dim
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vectorize(t) for t in texts]


class OpenAICompatEmbedder(Embedder):
    """OpenAI 兼容 embeddings（默认硅基流动 SiliconFlow）。

    端点：POST {base}/embeddings；请求体 {model, input:[...], encoding_format:"float"}；
    鉴权：Authorization: Bearer <key>。响应 data[].embedding，按 index 归位。
    真实维度由首个请求探测得到（避免 512/1024 等配置不符）。
    """

    def __init__(self, settings: Settings) -> None:
        self._base = settings.embed_base_url.rstrip("/")
        self._model = settings.embed_model
        self._key = settings.embed_api_key
        # BGE/Qwen3 均支持长文本；保守截断，避免超模型 token 上限
        self.max_input_chars = settings.embed_max_input_chars or None
        self._dim: int | None = None
        self._endpoint = f"{self._base}/embeddings"

    async def ensure_dim(self) -> int:
        if self._dim is None:
            if not self._key or not self._model:
                raise RuntimeError(
                    "openai_compatible 嵌入需配置 EMBED_API_KEY / EMBED_MODEL / EMBED_BASE_URL"
                )
            # 探针：一次小请求确定维度
            probe = await self._request(["[probe]"])
            self._dim = len(probe[0])
        return self._dim

    async def _request(self, texts: list[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self._key}"}
        payload = {"model": self._model, "input": texts, "encoding_format": "float"}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(self._endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        rows = sorted(data["data"], key=lambda d: d.get("index", 0))
        return [item["embedding"] for item in rows]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._dim is None:
            await self.ensure_dim()
        # 大批量按 ≤128 切片，避免单请求过大
        texts = self._maybe_truncate(texts)
        out: list[list[float]] = []
        for i in range(0, len(texts), 128):
            chunk = texts[i : i + 128]
            if not chunk:
                continue
            vecs = await self._request(chunk)
            out.extend(vecs)
        return out


def build_embedder(settings: Settings) -> Embedder:
    if settings.embed_provider == "openai_compatible":
        return OpenAICompatEmbedder(settings)
    return LocalCharNgramEmbedder(dim=settings.embed_dim)
