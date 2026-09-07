"""E2E 测试夹具：独立测试库/集合 + 外部桩服务 + 真实 uvicorn 子进程。

测试只走用户可见入口（HTTP），不触碰组件内部接口；桩服务说真实协议。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_MYSQL_DB = "ap1_test"
TEST_QDRANT_COLLECTION = "idol_knowledge_test"
TEST_ENV = "test"

_SEED_INFOS = [
    ("基础信息", "连淮伟的生日是1998年1月8日，出生于中国山东省，是一名歌手与偶像艺人。"),
    ("音乐作品", "连淮伟的代表作有《Jazz Man》《理想画》《Vibrate》等歌曲。"),
    ("粉丝互动", "连淮伟喜欢和粉丝分享日常，常说希望大家开心做自己。"),
]
_SEED_SONG = ("Love In The Mirror | 理想画", "温柔浪漫的情歌，表达对心仪之人的专一眷恋。")
_SEED_LYRIC = "想和你一起去到宇宙尽头 也不回头"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _prepare_test_db(env: dict) -> None:
    """建测试库并授权；调用真实迁移。"""
    from backend.config import Settings
    from backend.repository.db import migrate

    s = Settings(
        ENV=TEST_ENV,
        MYSQL_DB=TEST_MYSQL_DB,
        MYSQL_HOST=env["MYSQL_HOST"],
        MYSQL_PORT=int(env["MYSQL_PORT"]),
        MYSQL_USER=env["MYSQL_USER"],
        MYSQL_PASSWORD=env["MYSQL_PASSWORD"],
    )
    # ap1 用户无全局 CREATE：用 root 借 docker exec 建库
    subprocess.run(
        [
            "docker", "exec", "ap1-mysql", "mysql", "-uroot", "-proot", "-e",
            f"CREATE DATABASE IF NOT EXISTS {TEST_MYSQL_DB} "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; "
            f"GRANT ALL PRIVILEGES ON {TEST_MYSQL_DB}.* TO 'ap1'@'%'; "
            f"GRANT ALL PRIVILEGES ON {TEST_MYSQL_DB}.* TO 'ap1'@'localhost'; "
            "FLUSH PRIVILEGES;",
        ],
        capture_output=True,
    )
    migrate(s)


def _seed_knowledge(env: dict) -> None:
    """向测试库/集合灌入少量真实数据（走 repository/vector 真实代码路径）。"""
    import asyncio

    from backend.agent.embeddings import build_embedder
    from backend.config import Settings
    from backend.repository.db import create_async_engine_for
    from backend.repository.repository import Repository
    from backend.repository.vector_store import KIND_IDOL_INFO, KIND_LYRIC, KIND_SONG, VectorStore, point_id

    s = Settings(
        ENV=TEST_ENV,
        MYSQL_DB=TEST_MYSQL_DB,
        MYSQL_HOST=env["MYSQL_HOST"],
        MYSQL_PORT=int(env["MYSQL_PORT"]),
        MYSQL_USER=env["MYSQL_USER"],
        MYSQL_PASSWORD=env["MYSQL_PASSWORD"],
        QDRANT_URL=env["QDRANT_URL"],
        QDRANT_COLLECTION=TEST_QDRANT_COLLECTION,
        EMBED_PROVIDER="local",
        EMBED_DIM=512,
    )

    async def _run() -> None:
        eng = create_async_engine_for(s)
        emb = build_embedder(s)
        vec = VectorStore(s)
        await vec.recreate_collection(await emb.ensure_dim())
        repo = Repository(eng, s, emb, vec)
        # DB 行（幂等 upsert，重跑不重复）
        await repo.upsert_idol_infos([{"tag": t, "content": c} for t, c in _SEED_INFOS])
        await repo.upsert_songs([{"song_title": _SEED_SONG[0], "intro": _SEED_SONG[1]}])
        songs = await repo.list_all_songs()
        sid = songs[0].id if songs else None
        if sid is not None:
            await repo.replace_lyrics_for_song(sid, [_SEED_LYRIC])
        # 向量：以 DB 活跃集合为准全量写（批量向量化，等价生产 reconcile）
        plan: list[tuple[int, str, str]] = []  # (pid, kind, content_hash) — text 存临时对照
        text_by_pid: dict[int, str] = {}
        for r in await repo.list_all_idol_infos():
            pid = point_id(KIND_IDOL_INFO, r.id)
            plan.append((pid, KIND_IDOL_INFO, r.content_hash))
            text_by_pid[pid] = r.content
        for srow in await repo.list_all_songs():
            if srow.intro:
                pid = point_id(KIND_SONG, srow.id)
                plan.append((pid, KIND_SONG, srow.content_hash))
                text_by_pid[pid] = srow.intro
            for lr in await repo.list_lyrics_of_song(srow.id):
                pid = point_id(KIND_LYRIC, lr.id)
                plan.append((pid, KIND_LYRIC, lr.content_hash))
                text_by_pid[pid] = lr.content
        vectors = await emb.embed([text_by_pid[p] for p, _, _ in plan])
        await vec.upsert_vectors(
            [
                (
                    pid,
                    vectors[i],
                    {"kind": kind, "row_id": pid - point_id(kind, 0), "content_hash": ch},
                )
                for i, (pid, kind, ch) in enumerate(plan)
            ]
        )
        await eng.dispose()
        await vec.close()

    asyncio.run(_run())


@pytest.fixture(scope="session")
def e2e(tmp_path_factory):
    """启动模型桩 + 搜索桩 + 真实 uvicorn（含 lifespan 迁移），返回 base_url。"""
    from tests import stub_servers

    model_server = stub_servers.StubServer(stub_servers.ModelHandler)
    search_server = stub_servers.StubServer(stub_servers.SearchHandler)
    model_server.start()
    search_server.start()
    app_port = _free_port()
    log_file = tmp_path_factory.mktemp("logs") / "ap1.log"

    env = {
        **os.environ,
        "ENV": TEST_ENV,
        "MYSQL_DB": TEST_MYSQL_DB,
        "MYSQL_HOST": "127.0.0.1",
        "MYSQL_PORT": "3307",
        "MYSQL_USER": "ap1",
        "MYSQL_PASSWORD": "ap1",
        "QDRANT_URL": "http://127.0.0.1:6333",
        "QDRANT_COLLECTION": TEST_QDRANT_COLLECTION,
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "LLM_BASE_URL": f"http://127.0.0.1:{model_server.port}",
        "LLM_API_KEY": "",
        "LLM_MODEL": "ds-v4-flash",
        "SEARCH_BASE_URL": f"http://127.0.0.1:{search_server.port}",
        "SEARCH_API_KEY": "",
        "EMBED_PROVIDER": "local",
        "EMBED_DIM": "512",
        "WINDOW_TOKEN_BUDGET": "600",
        "KEEP_RECENT_TURNS": "5",
        "MAX_RUN_STEPS": "6",
        "LOG_LEVEL": "INFO",
        "LOG_FILE": str(log_file),
    }
    _prepare_test_db(env)
    _seed_knowledge(env)

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "backend.main:app",
            "--host", "127.0.0.1", "--port", str(app_port), "--log-level", "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{app_port}"
    try:
        _wait_ready(base, proc)
        yield {"base": base, "log_file": log_file}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        model_server.stop()
        search_server.stop()


def _wait_ready(base: str, proc: subprocess.Popen, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"server exited early:\n{out}")
        try:
            r = httpx.get(f"{base}/api/health", timeout=2)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.4)
    raise RuntimeError("server not ready in time")


# 断言前的小工具
class Api:
    def __init__(self, base: str):
        self._base = base
        self._c = httpx.Client(base_url=base, timeout=120)

    def register(self, nickname: str | None = None) -> dict:
        r = self._c.post("/api/auth/register", json={"nickname": nickname or None})
        assert r.status_code == 200, r.text
        return r.json()

    def login(self, nickname: str, token: str) -> None:
        r = self._c.post("/api/auth/login", json={"nickname": nickname, "token": token})
        assert r.status_code == 200, r.text

    def send(self, token: str, text: str, client_message_id: str) -> dict:
        r = self._c.post(
            "/api/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": text, "client_message_id": client_message_id},
        )
        assert r.status_code == 200, r.text
        return r.json()

    def send_raw(self, token: str, text: str, client_message_id: str):
        return self._c.post(
            "/api/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": text, "client_message_id": client_message_id},
        )

    def history(self, token: str) -> list[dict]:
        r = self._c.get("/api/messages", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        return r.json()["messages"]

    def history_page(self, token: str, after_seq: int | None = None, limit: int = 200) -> dict:
        params = {}
        if after_seq is not None:
            params["after_seq"] = after_seq
        if limit is not None:
            params["limit"] = limit
        r = self._c.get("/api/messages", headers={"Authorization": f"Bearer {token}"}, params=params)
        assert r.status_code == 200, r.text
        return r.json()

    def history_all_paged(self, token: str, limit: int = 50) -> list[dict]:
        """逐页拉取全部历史，验证分页游标可完整恢复长会话。"""
        collected: list[dict] = []
        after = 0
        while True:
            page = self.history_page(token, after_seq=after, limit=limit)
            collected.extend(page["messages"])
            if not page["has_more"] or not page["messages"]:
                break
            after = page["next_seq"]
        return collected

    def close(self) -> None:
        self._c.close()


@pytest.fixture
def api(e2e) -> Api:
    a = Api(e2e["base"])
    yield a
    a.close()


@pytest.fixture
def brain():
    """让测试切换桩模型的确定性行为（在会话级全局 handler 上）。"""
    from tests import stub_servers

    b = stub_servers.ModelHandler.brain
    yield b
    b.fail_once = False
    b.malformed = False
    b.loop_forever = False


@pytest.fixture
def db():
    """测试期间直读 ap1_test（校验持久化副作用；非组件内部测试接口）。"""
    import pymysql

    conn = pymysql.connect(
        host="127.0.0.1", port=3307, user="ap1", password="ap1",
        database=TEST_MYSQL_DB, charset="utf8mb4",
    )

    def q(sql: str, params=None):
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()

    yield q
    conn.close()
