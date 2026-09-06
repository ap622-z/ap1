"""qdrant 偶像知识向量存储。

只承载 idol_infos / songs / idol_lyrics 的向量，与规范记录同 id（同 id 幂等写入）。
知识一致性协议见 spec/plan：同 id 双写 + content_hash + vector_synced + 读时回查 + 清扫。

point id 采用整数；三种载体共用同一集合，以「分 kind 的 base 偏移 + row_id」编码，
避免 idol_infos / songs / idol_lyrics 各自自增 id 撞号，同时保持同 row 幂等。
payload 中带 kind / row_id / content_hash 供读时回查。
"""

from __future__ import annotations

from typing import Any

from qdrant_client import AsyncQdrantClient, models

from app.config import Settings

KIND_IDOL_INFO = "idol_info"
KIND_SONG = "song"
KIND_LYRIC = "lyric"

# 分 kind base：保证单集合内整数 id 不撞号
_BASE = {KIND_IDOL_INFO: 1_000_000_000, KIND_SONG: 2_000_000_000, KIND_LYRIC: 3_000_000_000}


def point_id(kind: str, row_id: int) -> int:
    return _BASE[kind] + row_id


class VectorStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncQdrantClient | None = None

    async def _client_or(self) -> AsyncQdrantClient:
        if self._client is None:
            self._client = AsyncQdrantClient(url=self._settings.qdrant_url)
        return self._client

    async def ensure_collection(self) -> None:
        c = await self._client_or()
        name = self._settings.qdrant_collection
        exists = await c.collection_exists(name)
        if not exists:
            await c.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(
                    size=self._settings.embed_dim,
                    distance=models.Distance.COSINE,
                ),
            )

    async def recreate_collection(self) -> None:
        """摄入前置：整库重建（种子级，幂等重跑前清空旧点）。"""
        c = await self._client_or()
        name = self._settings.qdrant_collection
        if await c.collection_exists(name):
            await c.delete_collection(collection_name=name)
        await c.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(
                size=self._settings.embed_dim,
                distance=models.Distance.COSINE,
            ),
        )

    async def upsert_vectors(
        self, points: list[tuple[int, list[float], dict[str, Any]]]
    ) -> None:
        """points: (point_id int, vector, payload)。同 id 覆盖写入，幂等。"""
        if not points:
            return
        c = await self._client_or()
        await c.upsert(
            collection_name=self._settings.qdrant_collection,
            points=[
                models.PointStruct(id=pid, vector=vec, payload=payload)
                for pid, vec, payload in points
            ],
        )

    async def delete_by_ids(self, point_ids: list[int]) -> None:
        if not point_ids:
            return
        c = await self._client_or()
        await c.delete(
            collection_name=self._settings.qdrant_collection,
            points_selector=models.PointIdsList(points=point_ids),
        )

    async def search(self, vector: list[float], limit: int) -> list[dict[str, Any]]:
        """ANN 只返回候选 id + payload；是否可对外由调用方回 MySQL 校验。"""
        c = await self._client_or()
        resp = await c.query_points(
            collection_name=self._settings.qdrant_collection,
            query=vector,
            limit=limit,
            with_payload=True,
        )
        return [
            {
                "id": hit.id,
                "score": hit.score,
                "payload": hit.payload or {},
            }
            for hit in resp.points
        ]

    async def scroll_all_ids(self) -> list[dict[str, Any]]:
        """返回集合内全部点的 id + payload（供摄入对账清扫）。"""
        c = await self._client_or()
        records, _ = await c.scroll(
            collection_name=self._settings.qdrant_collection,
            limit=10000,
            with_payload=True,
            with_vectors=False,
        )
        return [{"id": rec.id, "payload": rec.payload or {}} for rec in records]

    async def count(self) -> int:
        c = await self._client_or()
        info = await c.count(collection_name=self._settings.qdrant_collection, exact=True)
        return info.count

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
