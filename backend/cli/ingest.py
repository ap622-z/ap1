"""偶像知识摄入（种子/脚本级，非请求路径）。

读 doc/asset/ 三份素材 → 6 表（3 张知识表）+ qdrant 向量。
协议：同 id 幂等 + content_hash + vector_synced + 清扫。可安全重跑：
- MySQL 侧按 content_hash / (song_id, seg_no) 幂等；
- qdrant 侧以「DB 活跃集合 ↔ 集合存量」全量对账：活跃行重写向量，孤儿/过期点删除。

用法：uv run python -m backend.cli.ingest
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

from backend.agent.embeddings import build_embedder
from backend.config import Settings, get_settings
from backend.repository.db import create_async_engine_for, migrate
from backend.repository.repository import Repository
from backend.repository.vector_store import (
    KIND_IDOL_INFO,
    KIND_LYRIC,
    KIND_SONG,
    VectorStore,
    point_id,
)

ASSET_DIR = Path(__file__).resolve().parents[2] / "doc" / "asset"
XLSX = ASSET_DIR / "idolinfo-清洗后.xlsx"
SONG_INTRO_MD = ASSET_DIR / "歌曲介绍.md"
LYRICS_MD = ASSET_DIR / "lhw歌词新.md"

_SONG_HEADER_RE = re.compile(r"^\*\*\s*\d+\s*\.\s*《(.+?)》\s*\*\*$")
_INTRO_HEADER_RE = re.compile(r"^\*\*\s*\d+\s*\.\s*(.+?)\s*\*\*$")
_FIELD_RE = re.compile(r"^-\s*\*\*(.+?)\*\*\s*[:：]\s*(.*)$")


@dataclass
class SongAsset:
    song_title: str
    lyric_meta: dict[str, str] = field(default_factory=dict)
    lyric_segments: list[str] = field(default_factory=list)
    intro: str = ""


def _norm_title(t: str) -> str:
    """标题归一化：去空白、全半角统一、转小写，用于两素材归并。"""
    s = t.replace(" ", "").replace("　", "").replace("（", "(").replace("）", ")")
    return s.lower()


def parse_xlsx(path: Path) -> list[dict]:
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb["Sheet1"]
    items: list[dict] = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue
        seq, tag, content, event_time = (list(row) + [None, None, None, None])[:4]
        if not content or not str(content).strip():
            continue
        items.append(
            {
                "tag": str(tag or "").strip(),
                "content": str(content).strip(),
                "event_time": None if event_time in (None, "") else str(event_time).strip(),
            }
        )
    return items


def parse_lyrics_md(path: Path) -> list[SongAsset]:
    text = path.read_text(encoding="utf-8")
    songs: list[SongAsset] = []
    cur: SongAsset | None = None
    lyric_lines: list[str] = []
    in_lyrics = False
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _SONG_HEADER_RE.match(line.strip())
        if m:
            if cur is not None:
                cur.lyric_segments = _group_segments(lyric_lines)
            cur = SongAsset(song_title=m.group(1).strip())
            songs.append(cur)
            lyric_lines = []
            in_lyrics = False
            continue
        fm = _FIELD_RE.match(line.strip())
        if fm and cur is not None:
            key, val = fm.group(1).strip(), fm.group(2).strip()
            cur.lyric_meta[key] = val
            in_lyrics = key == "歌词"
            continue
        if cur is not None and (in_lyrics or not line.strip()):
            if in_lyrics:
                lyric_lines.append(line.strip())
    if cur is not None:
        cur.lyric_segments = _group_segments(lyric_lines)
    return songs


def _group_segments(lines: list[str]) -> list[str]:
    """正文按段（空行=段落边界）切成行；素材已按空行分段。"""
    segs: list[str] = []
    buf: list[str] = []
    for ln in lines:
        if not ln:
            if buf:
                segs.append("\n".join(buf))
                buf = []
        else:
            buf.append(ln)
    if buf:
        segs.append("\n".join(buf))
    return segs


def parse_intro_md(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    title_to_intro: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("#"):
            continue
        m = _INTRO_HEADER_RE.match(line.strip())
        if m:
            if cur and buf:
                title_to_intro[cur] = "\n".join(buf).strip()
            cur = m.group(1).strip()
            buf = []
            continue
        if cur and line.strip():
            buf.append(line.strip())
    if cur and buf:
        title_to_intro[cur] = "\n".join(buf).strip()
    return title_to_intro


def merge_song_assets() -> list[SongAsset]:
    """歌词 md 与歌曲介绍 md 按 song_title 归并；歌目有出入，无歌词的歌保留歌曲行。"""
    lyrics = parse_lyrics_md(LYRICS_MD)
    intro = parse_intro_md(SONG_INTRO_MD)
    by_norm: dict[str, SongAsset] = {}
    for s in lyrics:
        by_norm[_norm_title(s.song_title)] = s
    for title, it in intro.items():
        n = _norm_title(title)
        if n in by_norm:
            by_norm[n].intro = it
        else:
            by_norm[n] = SongAsset(song_title=title, intro=it)
    return list(by_norm.values())


async def _reconcile_vectors(repo: Repository, vector: VectorStore, embedder) -> tuple[int, int]:
    """DB 活跃集合 ↔ qdrant 全量对账：重写活跃向量 + 删除孤儿/过期点。

    文本先收集、再批量向量化（远程提供方按批次请求），返回 (upsert_points, deleted_points)。
    """
    # pid -> (kind, content_hash, text)
    active: dict[int, tuple[str, str, str]] = {}

    for r in await repo.list_all_idol_infos():
        active[point_id(KIND_IDOL_INFO, r.id)] = (KIND_IDOL_INFO, r.content_hash, r.content)
    for s in await repo.list_all_songs():
        if s.intro:
            active[point_id(KIND_SONG, s.id)] = (KIND_SONG, s.content_hash, s.intro)
        for lr in await repo.list_lyrics_of_song(s.id):
            active[point_id(KIND_LYRIC, lr.id)] = (KIND_LYRIC, lr.content_hash, lr.content)

    # 批量向量化：一次取一批文本，保持与 pid 同序
    pids = list(active.keys())
    texts = [active[p][2] for p in pids]
    vectors = await embedder.embed(texts) if texts else []

    points = [
        (
            pid,
            vectors[i],
            {
                "kind": active[pid][0],
                "row_id": pid - point_id(active[pid][0], 0),
                "content_hash": active[pid][1],
            },
        )
        for i, pid in enumerate(pids)
    ]
    await vector.upsert_vectors(points)

    existing = await vector.scroll_all_ids()
    existing_ids = {int(p["id"]) for p in existing}
    orphan = [pid for pid in existing_ids if pid not in active]
    await vector.delete_by_ids(orphan)

    # 维护 vector_synced 标记（pid - base = 该 kind 下的 row_id）
    info_ids = [pid - point_id(KIND_IDOL_INFO, 0) for pid in active if point_id(KIND_IDOL_INFO, 0) <= pid < point_id(KIND_SONG, 0)]
    song_ids = [pid - point_id(KIND_SONG, 0) for pid in active if point_id(KIND_SONG, 0) <= pid < point_id(KIND_LYRIC, 0)]
    lyric_ids = [pid - point_id(KIND_LYRIC, 0) for pid in active if pid >= point_id(KIND_LYRIC, 0)]
    if info_ids:
        await repo.set_vector_synced("idol_infos", info_ids, True)
    if song_ids:
        await repo.set_vector_synced("songs", song_ids, True)
    if lyric_ids:
        await repo.set_vector_synced("idol_lyrics", lyric_ids, True)
    return len(points), len(orphan)


async def run() -> None:
    settings = get_settings()
    migrate(settings)
    engine = create_async_engine_for(settings)
    embedder = build_embedder(settings)
    vector = VectorStore(settings)
    try:
        await vector.recreate_collection(await embedder.ensure_dim())
        await _run_body(engine, embedder, vector, settings)
    finally:
        await engine.dispose()
        await vector.close()


async def ingest_knowledge(engine, settings: Settings, *, log=None) -> None:
    """在既有进程/引擎内执行摄入（容器 boot、运维脚本复用）。

    幂等：DB 行按 content_hash / (song_id, seg_no) 幂等；qdrant 全量对账。
    """
    embedder = build_embedder(settings)
    vector = VectorStore(settings)
    say = log.info if log is not None else print
    try:
        await vector.recreate_collection(await embedder.ensure_dim())
        await _run_body(engine, embedder, vector, settings, say=say)
    finally:
        await vector.close()


async def _run_body(engine, embedder, vector, settings: Settings, say=print) -> None:
    repo = Repository(engine, settings, embedder, vector)

    # 1) 偶像信息
    items = parse_xlsx(XLSX)
    changed = await repo.upsert_idol_infos(items)
    say(f"idol_infos：解析 {len(items)} 条，新增/变更 {len(changed)} 条")

    # 2) 歌曲 + 歌词
    songs = merge_song_assets()
    say(f"songs：归并后 {len(songs)} 首")
    song_items = [
        {
            "song_title": s.song_title,
            "album": _meta(s, "所属专辑") or None,
            "release_time": _meta(s, "发行时间") or None,
            "company": _meta(s, "发行公司") or None,
            "creators": _meta(s, "创作者") or None,
            "collaboration": _meta(s, "合作/独立发行") or None,
            "intro": s.intro,
        }
        for s in songs
    ]
    await repo.upsert_songs(song_items)
    all_songs = await repo.list_all_songs()
    norm_to_id = {_norm_title(s.song_title): s.id for s in all_songs}
    lyric_total = 0
    for s in songs:
        sid = norm_to_id.get(_norm_title(s.song_title))
        if sid is None:
            say(f"  警告：未找到歌曲行 {s.song_title}")
            continue
        if s.lyric_segments:
            lyric_total += len(await repo.replace_lyrics_for_song(sid, s.lyric_segments))
    say(f"idol_lyrics：歌词段落 {lyric_total} 段（累计 active）")

    # 3) 向量对账
    ups, dele = await _reconcile_vectors(repo, vector, embedder)
    say(f"qdrant：写入 {ups} 点，删除孤儿 {dele} 点")
    say("知识摄入完成 ✅")


def _meta(s: SongAsset, key: str) -> str:
    return s.lyric_meta.get(key, "").strip() or ""


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
