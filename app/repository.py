"""Repository —— 系统唯一的数据访问门面。

一切持久状态（用户、会话、消息、摘要、偶像知识）都经它读写；组件——尤其 Agent——
不直接持有任何状态。数据按 Scope 隔离：user/session 相关方法一律以 scope 为条件，
不存在无 scope 的裸访问；越界（session 不属于该 user）抛 SessionOutOfScopeError。

方法面分组（对应 spec）：
- worker_face：写用户消息、消息生命周期、结束事务内统一落库工具记录与回复。
- agent_face：读会话历史与摘要、压缩摘要写入、知识检索（RAG 读路径）。

所有方法都是 async（事件循环不阻塞）；SQL 为参数化 text()，防注入。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.config import Settings
from app.embeddings import Embedder
from app.errors import SessionOutOfScopeError
from app.records import (
    IdolInfoRow,
    LyricRow,
    MessageRow,
    SessionRow,
    SongRow,
    ToolRecord,
    UserRow,
)
from app.scope import Scope
from app.tokens import estimate_tokens
from app.vector_store import KIND_IDOL_INFO, KIND_LYRIC, KIND_SONG, VectorStore


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _j(d: dict[str, Any]) -> str:
    return json.dumps(d, ensure_ascii=False)


def _loads(s: Any) -> dict[str, Any]:
    if isinstance(s, (bytes, bytearray)):
        s = s.decode("utf-8")
    if isinstance(s, dict):
        return s
    return json.loads(s)


def _row_to_user(row: Any) -> UserRow | None:
    if row is None:
        return None
    return UserRow(
        id=row["id"],
        nickname=row["nickname"],
        token_hash=row["token_hash"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_login_at=row["last_login_at"],
    )


def _row_to_session(row: Any) -> SessionRow | None:
    if row is None:
        return None
    return SessionRow(
        id=row["id"],
        user_id=row["user_id"],
        status=row["status"],
        summary_text=row["summary_text"],
        summary_tokens=row["summary_tokens"],
        summary_seq=row["summary_seq"],
        last_active_at=row["last_active_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_message(row: Any) -> MessageRow:
    return MessageRow(
        id=row["id"],
        session_id=row["session_id"],
        seq=row["seq"],
        type=row["type"],
        payload=_loads(row["payload"]),
        token_count=row["token_count"],
        client_message_id=row["client_message_id"],
        status=row["status"],
        origin_message_id=row["origin_message_id"],
        created_at=row["created_at"],
    )


class Repository:
    def __init__(self, engine: AsyncEngine, settings: Settings, embedder: Embedder, vector: VectorStore):
        self._engine = engine
        self._settings = settings
        self._embedder = embedder
        self._vector = vector

    # =====================================================================
    # 工具 —— scope 数据层兜底
    # =====================================================================
    async def assert_session_owned(self, scope: Scope) -> None:
        """数据层兜底：校验 scope 里的 session 确实属于 scope.user_id。

        每条写入/读取会话数据的入口（worker、历史读取）都先过此闸，
        使隔离不单靠上层鉴权自觉（plan §4 / spec 故事 16）。
        """
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT 1 FROM sessions WHERE id = :sid AND user_id = :uid AND status = 1"
                ),
                {"sid": scope.session_id, "uid": scope.user_id},
            )
            if res.scalar_one_or_none() is None:
                raise SessionOutOfScopeError(
                    "会话越界：尝试访问不属于当前用户的会话", scope=scope.as_dict()
                )

    # =====================================================================
    # worker_face —— 身份
    # =====================================================================
    async def create_user(self, nickname: str, token_hash: str) -> UserRow:
        async with self._engine.begin() as conn:
            now = _now()
            res = await conn.execute(
                text(
                    "INSERT INTO users (nickname, token_hash, status, created_at, updated_at) "
                    "VALUES (:nickname, :token_hash, 1, :now, :now)"
                ),
                {"nickname": nickname, "token_hash": token_hash, "now": now},
            )
            uid = res.lastrowid
        return UserRow(
            id=uid,
            nickname=nickname,
            token_hash=token_hash,
            status=1,
            created_at=now,
            updated_at=now,
        )

    async def get_user_by_token_hash(self, token_hash: str) -> UserRow | None:
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text("SELECT * FROM users WHERE token_hash = :h"), {"h": token_hash}
            )
            return _row_to_user(res.mappings().first())

    async def get_user_by_nickname(self, nickname: str) -> UserRow | None:
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text("SELECT * FROM users WHERE nickname = :n"), {"n": nickname}
            )
            return _row_to_user(res.mappings().first())

    async def touch_login(self, user_id: int) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET last_login_at = :t, updated_at = :t WHERE id = :id"),
                {"t": _now(), "id": user_id},
            )

    async def get_or_create_session(self, user_id: int) -> SessionRow:
        """一用户一会话：惰性创建 + 结构保证（sessions.user_id 唯一）。

        并发首建下唯一键冲突：捕获后重查（另一请求已建）并返回既有会话。
        """
        from sqlalchemy.exc import IntegrityError

        async with self._engine.begin() as conn:
            session = await self._fetch_session_by_user(conn, user_id)
            if session is not None:
                return session
            now = _now()
            try:
                await conn.execute(
                    text(
                        "INSERT INTO sessions (user_id, status, created_at, updated_at) "
                        "VALUES (:user_id, 1, :now, :now)"
                    ),
                    {"user_id": user_id, "now": now},
                )
            except IntegrityError:
                # 另一请求并发首建成功 → 返回其会话
                return await self._fetch_session_by_user(conn, user_id) or SessionRow(
                    id=0,
                    user_id=user_id,
                    status=1,
                    summary_text=None,
                    summary_tokens=None,
                    summary_seq=None,
                    last_active_at=None,
                    created_at=now,
                    updated_at=now,
                )
            fetched = await self._fetch_session_by_user(conn, user_id)
            if fetched is not None:
                return fetched
            return SessionRow(
                id=0,  # 正常不会走到：插入后必可查回；此兜底避免引用未初始化 id
                user_id=user_id,
                status=1,
                summary_text=None,
                summary_tokens=None,
                summary_seq=None,
                last_active_at=None,
                created_at=now,
                updated_at=now,
            )

    async def _fetch_session_by_user(self, conn: AsyncConnection, user_id: int) -> SessionRow | None:
        res = await conn.execute(
            text("SELECT * FROM sessions WHERE user_id = :user_id"), {"user_id": user_id}
        )
        return _row_to_session(res.mappings().first())

    async def get_session(self, session_id: int) -> SessionRow | None:
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text("SELECT * FROM sessions WHERE id = :id"), {"id": session_id}
            )
            return _row_to_session(res.mappings().first())

    # =====================================================================
    # worker_face —— 消息与生命周期
    # =====================================================================
    async def append_user_message(
        self,
        scope: Scope,
        text_: str,
        client_message_id: str | None,
    ) -> MessageRow:
        """写入 user 消息（status=received）。seq 取会话内下一个单调号。

        调用方（worker）保证在同一会话的 mailbox 锁内，故 seq 分配无竞争。
        """
        token_count = estimate_tokens(text_, self._settings)
        async with self._engine.begin() as conn:
            seq = await self._next_seq(conn, scope.session_id)
            payload = _j({"text": text_})
            res = await conn.execute(
                text(
                    "INSERT INTO session_messages "
                    "(session_id, seq, type, payload, token_count, client_message_id, status, created_at) "
                    "VALUES (:sid, :seq, 'user', :payload, :tok, :cmid, 'received', :now)"
                ),
                {
                    "sid": scope.session_id,
                    "seq": seq,
                    "payload": payload,
                    "tok": token_count,
                    "cmid": client_message_id,
                    "now": _now(),
                },
            )
        return MessageRow(
            id=res.lastrowid,
            session_id=scope.session_id,
            seq=seq,
            type="user",
            payload={"text": text_},
            token_count=token_count,
            client_message_id=client_message_id,
            status="received",
            origin_message_id=None,
            created_at=_now(),
        )

    async def find_user_message_by_client_id(
        self, scope: Scope, client_message_id: str
    ) -> MessageRow | None:
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT * FROM session_messages WHERE session_id = :sid "
                    "AND client_message_id = :cmid AND type = 'user'"
                ),
                {"sid": scope.session_id, "cmid": client_message_id},
            )
            row = res.mappings().first()
            return _row_to_message(row) if row else None

    async def mark_processing(self, scope: Scope, message_id: int) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE session_messages SET status = 'processing' "
                    "WHERE id = :id AND session_id = :sid"
                ),
                {"id": message_id, "sid": scope.session_id},
            )

    async def revert_to_received(self, scope: Scope, message_id: int) -> None:
        """处理中途失败：从 processing 回退到 received，供同 client_message_id 重发续跑。"""
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE session_messages SET status = 'received' "
                    "WHERE id = :id AND session_id = :sid AND status = 'processing'"
                ),
                {"id": message_id, "sid": scope.session_id},
            )

    async def finalize_message(
        self,
        scope: Scope,
        user_message_id: int,
        reply_text: str,
        tool_records: Iterable[ToolRecord],
    ) -> MessageRow:
        """结束事务：写 tool 记录 + agent 回复行 + 置 user 消息 replied，一并原子提交。

        工具记录与回复行的 origin_message_id 都指向发起它们的 user 消息。
        """
        now = _now()
        reply_token = estimate_tokens(reply_text, self._settings)
        async with self._engine.begin() as conn:
            seq = await self._next_seq(conn, scope.session_id)
            rows_written = 0
            for rec in tool_records:
                payload = _j(
                    {"name": rec.name, "input": rec.input or {}, "output": rec.output}
                )
                tok = estimate_tokens(
                    f"{rec.name} {json.dumps(rec.input, ensure_ascii=False)} "
                    f"{json.dumps(rec.output, ensure_ascii=False)}",
                    self._settings,
                )
                await conn.execute(
                    text(
                        "INSERT INTO session_messages "
                        "(session_id, seq, type, payload, token_count, origin_message_id, created_at) "
                        "VALUES (:sid, :seq, 'tool', :payload, :tok, :origin, :now)"
                    ),
                    {
                        "sid": scope.session_id,
                        "seq": seq + rows_written,
                        "payload": payload,
                        "tok": tok,
                        "origin": user_message_id,
                        "now": now,
                    },
                )
                rows_written += 1
            reply_payload = _j({"text": reply_text})
            reply_res = await conn.execute(
                text(
                    "INSERT INTO session_messages "
                    "(session_id, seq, type, payload, token_count, origin_message_id, created_at) "
                    "VALUES (:sid, :seq, 'agent', :payload, :tok, :origin, :now)"
                ),
                {
                    "sid": scope.session_id,
                    "seq": seq + rows_written,
                    "payload": reply_payload,
                    "tok": reply_token,
                    "origin": user_message_id,
                    "now": now,
                },
            )
            reply_id = reply_res.lastrowid
            await conn.execute(
                text(
                    "UPDATE session_messages SET status = 'replied' "
                    "WHERE id = :id AND session_id = :sid"
                ),
                {"id": user_message_id, "sid": scope.session_id},
            )
            await conn.execute(
                text(
                    "UPDATE sessions SET last_active_at = :now, updated_at = :now WHERE id = :sid"
                ),
                {"now": now, "sid": scope.session_id},
            )
        return MessageRow(
            id=reply_id,
            session_id=scope.session_id,
            seq=seq + rows_written,
            type="agent",
            payload={"text": reply_text},
            token_count=reply_token,
            client_message_id=None,
            status=None,
            origin_message_id=user_message_id,
            created_at=now,
        )

    async def get_message(self, scope: Scope, message_id: int) -> MessageRow | None:
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT * FROM session_messages WHERE id = :id AND session_id = :sid"
                ),
                {"id": message_id, "sid": scope.session_id},
            )
            row = res.mappings().first()
            return _row_to_message(row) if row else None

    async def get_agent_reply_of(self, scope: Scope, user_message_id: int) -> MessageRow | None:
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT * FROM session_messages WHERE session_id = :sid "
                    "AND origin_message_id = :oid AND type = 'agent' ORDER BY seq LIMIT 1"
                ),
                {"sid": scope.session_id, "oid": user_message_id},
            )
            row = res.mappings().first()
            return _row_to_message(row) if row else None

    async def _next_seq(self, conn: AsyncConnection, session_id: int) -> int:
        res = await conn.execute(
            text("SELECT COALESCE(MAX(seq), 0) AS m FROM session_messages WHERE session_id = :sid"),
            {"sid": session_id},
        )
        return int(res.scalar_one()) + 1

    # =====================================================================
    # agent_face —— 上下文组装所需
    # =====================================================================
    async def list_messages_since(
        self, scope: Scope, min_seq: int, max_seq: int | None = None, limit: int = 2000
    ) -> list[MessageRow]:
        """按 seq 升序返回会话内消息（min_seq 不包含边界）。用于历史分页前向拉取。"""
        params: dict[str, Any] = {"sid": scope.session_id, "min_seq": min_seq, "limit": limit}
        cond = "session_id = :sid AND seq > :min_seq"
        if max_seq is not None:
            cond += " AND seq <= :max_seq"
            params["max_seq"] = max_seq
        cond += " ORDER BY seq LIMIT :limit"
        async with self._engine.connect() as conn:
            res = await conn.execute(text(f"SELECT * FROM session_messages WHERE {cond}"), params)
            return [_row_to_message(r) for r in res.mappings().all()]

    async def list_messages_window_tail(
        self, scope: Scope, min_seq: int, max_seq: int | None = None, limit: int = 2000
    ) -> list[MessageRow]:
        """上下文窗口读取：取 (min_seq, max_seq] 内**最新**的 limit 条，升序返回。

        压缩前窗口可能远超大小时，超限应保「最新的内容」而非最旧，否则会把正在回答的
        消息本身挤出上下文。
        """
        params: dict[str, Any] = {"sid": scope.session_id, "min_seq": min_seq, "limit": limit}
        cond = "session_id = :sid AND seq > :min_seq"
        if max_seq is not None:
            cond += " AND seq <= :max_seq"
            params["max_seq"] = max_seq
        cond += " ORDER BY seq DESC LIMIT :limit"
        async with self._engine.connect() as conn:
            res = await conn.execute(text(f"SELECT * FROM session_messages WHERE {cond}"), params)
            rows = [_row_to_message(r) for r in res.mappings().all()]
        rows.reverse()
        return rows

    async def write_summary(
        self, scope: Scope, summary_text: str | None, summary_seq: int | None
    ) -> None:
        """压缩后回写 summary_* 字段。summary_seq 界定已并入摘要的旧消息。"""
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE sessions SET summary_text = :t, summary_tokens = :tok, "
                    "summary_seq = :seq, updated_at = :now WHERE id = :sid"
                ),
                {
                    "t": summary_text,
                    "tok": estimate_tokens(summary_text or "", self._settings),
                    "seq": summary_seq,
                    "sid": scope.session_id,
                    "now": _now(),
                },
            )

    # =====================================================================
    # agent_face —— 知识检索（RAG 读路径：ANN 候选 → 回查校验）
    # =====================================================================
    async def search_knowledge(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """检索偶像知识/歌曲介绍/歌词段。只回传「回查校验通过」的内容。

        读路径：qdrant ANN 返回候选 id → 回 MySQL 按 id 校验（行存在、hash 匹配、status 上架）。
        """
        vec = (await self._embedder.embed([query]))[0]
        hits = await self._vector.search(vec, limit=limit)
        results: list[dict[str, Any]] = []
        async with self._engine.connect() as conn:
            for hit in hits:
                payload = hit["payload"] or {}
                kind = payload.get("kind")
                rid = payload.get("row_id")
                stored_hash = payload.get("content_hash")
                if not kind or rid is None:
                    continue
                ok = await self._verify(conn, kind, int(rid), stored_hash)
                if ok:
                    results.append(ok)
        return results

    async def _verify(
        self, conn: AsyncConnection, kind: str, rid: int, stored_hash: str | None
    ) -> dict[str, Any] | None:
        if kind == KIND_IDOL_INFO:
            res = await conn.execute(
                text("SELECT id, tag, content, status, content_hash FROM idol_infos WHERE id = :id"),
                {"id": rid},
            )
            row = res.mappings().first()
            if not row or row["status"] != 1:
                return None
            if stored_hash and stored_hash != row["content_hash"]:
                return None
            return {"kind": KIND_IDOL_INFO, "id": row["id"], "text": f"[{row['tag']}] {row['content']}"}
        if kind == KIND_SONG:
            res = await conn.execute(
                text(
                    "SELECT id, song_title, intro, status, content_hash FROM songs WHERE id = :id"
                ),
                {"id": rid},
            )
            row = res.mappings().first()
            if not row or row["status"] != 1:
                return None
            if stored_hash and stored_hash != row["content_hash"]:
                return None
            return {
                "kind": KIND_SONG,
                "id": row["id"],
                "text": f"歌曲《{row['song_title']}》介绍：{row['intro'] or ''}",
            }
        if kind == KIND_LYRIC:
            res = await conn.execute(
                text(
                    "SELECT l.id, l.content, l.status, l.content_hash, s.song_title "
                    "FROM idol_lyrics l JOIN songs s ON s.id = l.song_id WHERE l.id = :id"
                ),
                {"id": rid},
            )
            row = res.mappings().first()
            if not row or row["status"] != 1:
                return None
            if stored_hash and stored_hash != row["content_hash"]:
                return None
            return {
                "kind": KIND_LYRIC,
                "id": row["id"],
                "text": f"歌曲《{row['song_title']}》歌词段落：{row['content']}",
            }
        return None

    # =====================================================================
    # 运营/摄入侧（非请求路径）：知识写库
    # =====================================================================
    async def upsert_idol_infos(
        self, items: list[dict[str, Any]]
    ) -> list[IdolInfoRow]:
        """按 content_hash 幂等写 idol_infos；返回需同步向量的行（新增/重新上架）。

        content_hash 是同一条知识的稳定身份：命中已上架行 → 跳过；命中已下架行 →
        重新上架（复用原 id，避免重复行）；否则插入。
        """
        from app.hashing import sha256_hex

        changed: list[IdolInfoRow] = []
        async with self._engine.begin() as conn:
            now = _now()
            for it in items:
                c_hash = sha256_hex(it["content"])
                res = await conn.execute(
                    text(
                        "SELECT id, status FROM idol_infos WHERE content_hash = :h LIMIT 1"
                    ),
                    {"h": c_hash},
                )
                existing = res.mappings().first()
                if existing is not None and existing["status"] == 1:
                    continue  # 已上架 → 幂等跳过
                if existing is not None:
                    # 下架后重新摄入 → 复用原行，重新上架并标记待同步向量
                    await conn.execute(
                        text(
                            "UPDATE idol_infos SET tag = :tag, event_time = :et, status = 1, "
                            "vector_synced = 0, updated_at = :now WHERE id = :id"
                        ),
                        {
                            "tag": it["tag"],
                            "et": str(it.get("event_time") or "")[:32] or None,
                            "now": now,
                            "id": existing["id"],
                        },
                    )
                    row_id = existing["id"]
                else:
                    ins = await conn.execute(
                        text(
                            "INSERT INTO idol_infos (tag, content, event_time, content_hash, "
                            "vector_synced, status, created_at, updated_at) "
                            "VALUES (:tag, :content, :event_time, :h, 0, 1, :now, :now)"
                        ),
                        {
                            "tag": it["tag"],
                            "content": it["content"],
                            "event_time": str(it.get("event_time") or "")[:32] or None,
                            "h": c_hash,
                            "now": now,
                        },
                    )
                    row_id = ins.lastrowid
                changed.append(
                    IdolInfoRow(
                        id=row_id,
                        tag=it["tag"],
                        content=it["content"],
                        event_time=str(it.get("event_time") or "") or None,
                        content_hash=c_hash,
                        status=1,
                    )
                )
        return changed

    async def upsert_songs(self, items: list[dict[str, Any]]) -> list[SongRow]:
        """song_title 为归并键；intro 有变更则改行并标记需重新向量化。"""
        from app.hashing import sha256_hex

        changed: list[SongRow] = []
        async with self._engine.begin() as conn:
            for it in items:
                intro = it.get("intro") or ""
                c_hash = sha256_hex(intro)
                now = _now()
                res = await conn.execute(
                    text("SELECT id FROM songs WHERE song_title = :t"), {"t": it["song_title"]}
                )
                row = res.mappings().first()
                if row is None:
                    ins = await conn.execute(
                        text(
                            "INSERT INTO songs (song_title, album, release_time, company, creators, "
                            "collaboration, intro, content_hash, vector_synced, status, created_at, updated_at) "
                            "VALUES (:t, :album, :rt, :company, :creators, :collab, :intro, :h, 0, 1, :now, :now)"
                        ),
                        {
                            "t": it["song_title"],
                            "album": it.get("album"),
                            "rt": it.get("release_time"),
                            "company": it.get("company"),
                            "creators": it.get("creators"),
                            "collab": it.get("collaboration"),
                            "intro": intro or None,
                            "h": c_hash,
                            "now": now,
                        },
                    )
                    sid = ins.lastrowid
                else:
                    sid = row["id"]
                    upd = await conn.execute(
                        text(
                            "UPDATE songs SET album=:album, release_time=:rt, company=:company, "
                            "creators=:creators, collaboration=:collab, intro=:intro, content_hash=:h, "
                            "status=1, vector_synced=0, updated_at=:now WHERE id=:id AND "
                            "(intro IS NULL OR intro<>:intro OR content_hash<>:h)"
                        ),
                        {
                            "album": it.get("album"),
                            "rt": it.get("release_time"),
                            "company": it.get("company"),
                            "creators": it.get("creators"),
                            "collab": it.get("collaboration"),
                            "intro": intro or None,
                            "h": c_hash,
                            "id": sid,
                            "now": now,
                        },
                    )
                    if upd.rowcount == 0:
                        continue
                changed.append(
                    SongRow(
                        id=sid,
                        song_title=it["song_title"],
                        album=it.get("album"),
                        release_time=it.get("release_time"),
                        company=it.get("company"),
                        creators=it.get("creators"),
                        collaboration=it.get("collaboration"),
                        intro=intro or None,
                        content_hash=c_hash,
                        status=1,
                    )
                )
        return changed

    async def replace_lyrics_for_song(self, song_id: int, segments: list[str]) -> list[LyricRow]:
        """以歌为粒度对齐歌词段（幂等）：按 (song_id, seg_no) 稳定 upsert。

        段内容变化才更新（content_hash 变则需重新向量化）；超出新段数的旧段下架
        （status=0，向量点由摄入脚本清扫删除）。重跑不产生重复行、段 id 保持稳定。
        """
        from app.hashing import sha256_hex

        async with self._engine.begin() as conn:
            res = await conn.execute(
                text(
                    "SELECT id, seg_no, content_hash, status FROM idol_lyrics "
                    "WHERE song_id = :sid"
                ),
                {"sid": song_id},
            )
            existing = {r["seg_no"]: r for r in res.mappings().all()}
            out: list[LyricRow] = []
            now = _now()
            for idx, seg in enumerate(segments, start=1):
                seg = seg.strip()
                if not seg:
                    continue
                c_hash = sha256_hex(seg)
                prev = existing.get(idx)
                if prev is not None and prev["content_hash"] == c_hash and prev["status"] == 1:
                    out.append(
                        LyricRow(
                            id=prev["id"],
                            song_id=song_id,
                            seg_no=idx,
                            content=seg,
                            content_hash=c_hash,
                            status=1,
                        )
                    )
                    existing.pop(idx)
                    continue
                if prev is not None:
                    ins_id = prev["id"]
                    await conn.execute(
                        text(
                            "UPDATE idol_lyrics SET content=:content, content_hash=:h, "
                            "vector_synced=0, status=1, updated_at=:now WHERE id=:id"
                        ),
                        {"content": seg, "h": c_hash, "id": prev["id"], "now": now},
                    )
                    existing.pop(idx)
                else:
                    ins = await conn.execute(
                        text(
                            "INSERT INTO idol_lyrics (song_id, seg_no, content, content_hash, "
                            "vector_synced, status, created_at, updated_at) "
                            "VALUES (:sid, :seg_no, :content, :h, 0, 1, :now, :now)"
                        ),
                        {"sid": song_id, "seg_no": idx, "content": seg, "h": c_hash, "now": now},
                    )
                    ins_id = ins.lastrowid
                out.append(
                    LyricRow(
                        id=ins_id,
                        song_id=song_id,
                        seg_no=idx,
                        content=seg,
                        content_hash=c_hash,
                        status=1,
                    )
                )
            stale_ids = [v["id"] for k, v in existing.items() if v["status"] == 1]
            for sid in stale_ids:
                await conn.execute(
                    text(
                        "UPDATE idol_lyrics SET status=0, vector_synced=0, updated_at=:now "
                        "WHERE id=:id"
                    ),
                    {"now": now, "id": sid},
                )
        return out

    async def count_idol_infos(self) -> int:
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text("SELECT COUNT(*) FROM idol_infos WHERE status = 1")
            )
            return int(res.scalar_one())

    async def list_all_idol_infos(self) -> list[IdolInfoRow]:
        async with self._engine.connect() as conn:
            res = await conn.execute(text("SELECT * FROM idol_infos WHERE status = 1"))
            return [
                IdolInfoRow(
                    id=r["id"],
                    tag=r["tag"],
                    content=r["content"],
                    event_time=r["event_time"],
                    content_hash=r["content_hash"],
                    status=r["status"],
                )
                for r in res.mappings().all()
            ]

    async def list_all_songs(self) -> list[SongRow]:
        async with self._engine.connect() as conn:
            res = await conn.execute(text("SELECT * FROM songs WHERE status = 1"))
            return [
                SongRow(
                    id=r["id"],
                    song_title=r["song_title"],
                    album=r["album"],
                    release_time=r["release_time"],
                    company=r["company"],
                    creators=r["creators"],
                    collaboration=r["collaboration"],
                    intro=r["intro"],
                    content_hash=r["content_hash"],
                    status=r["status"],
                )
                for r in res.mappings().all()
            ]

    async def list_lyrics_of_song(self, song_id: int) -> list[LyricRow]:
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text("SELECT * FROM idol_lyrics WHERE song_id = :sid AND status = 1 ORDER BY seg_no"),
                {"sid": song_id},
            )
            return [
                LyricRow(
                    id=r["id"],
                    song_id=r["song_id"],
                    seg_no=r["seg_no"],
                    content=r["content"],
                    content_hash=r["content_hash"],
                    status=r["status"],
                )
                for r in res.mappings().all()
            ]

    # =====================================================================
    # 摄入辅助：向量同步状态标记 / 待清扫
    # =====================================================================
    async def set_vector_synced(self, table: str, row_ids: list[int], synced: bool = True) -> None:
        if not row_ids:
            return
        async with self._engine.begin() as conn:
            for rid in row_ids:
                await conn.execute(
                    text(f"UPDATE {table} SET vector_synced = :v, updated_at = :now WHERE id = :id"),
                    {"v": 1 if synced else 0, "now": _now(), "id": rid},
                )

    async def mark_row_removed(self, table: str, row_ids: list[int]) -> None:
        """摄入替换时下架旧行（status=0），向量点由清扫流程删除。"""
        if not row_ids:
            return
        async with self._engine.begin() as conn:
            for rid in row_ids:
                await conn.execute(
                    text(f"UPDATE {table} SET status = 0, updated_at = :now WHERE id = :id"),
                    {"now": _now(), "id": rid},
                )
