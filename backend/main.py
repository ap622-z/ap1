"""FastAPI 应用工厂：web + worker + agent 单例同驻单进程。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api import router as api_router
from backend.config import get_settings
from backend.config.checks import collect_startup_problems
from backend.context import build_runtime
from backend.errors import AppError
from backend.logging_setup import setup_logging
from backend.repository.db import create_async_engine_for, migrate

_FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


_log = None


async def _ensure_knowledge_ready(runtime, engine, settings) -> None:
    """知识就绪：空库首启自动灌入（失败中止）；有行但向量空则自动补灌（失败中止）。"""
    from backend.cli.ingest import ingest_knowledge

    rows = await runtime.repo.count_idol_infos()
    if rows == 0:
        if not settings.seed_on_startup:
            _log.warning("knowledge_seed_disabled_degraded")
            return
        _log.info("knowledge_empty_seed_start")
        try:
            await ingest_knowledge(engine, settings, log=_log)
        except Exception as exc:
            _log.error("knowledge_seed_failed", error=str(exc))
            raise RuntimeError(
                "首次启动知识灌入失败，已中止以避免空知识库运行。"
                "请检查网络与嵌入配置后重试；确需回到空基线请执行 docker compose down -v。"
            ) from exc
        _log.info("knowledge_seeded")
        return
    # DB 已有知识：核对向量集合非空（防「仅 qdrant 卷丢失 → 检索静默失效」）
    if await runtime.vector.count() == 0:
        _log.warning("knowledge_vectors_empty_heal_start")
        try:
            await ingest_knowledge(engine, settings, log=_log)
        except Exception as exc:
            _log.error("knowledge_heal_failed", error=str(exc))
            raise RuntimeError("知识库有数据但向量集合为空，自动补灌失败，已中止以避免检索失效。") from exc
        _log.info("knowledge_healed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _log
    settings = get_settings()
    setup_logging(settings)
    from backend.logging_setup import get_logger

    _log = get_logger("startup")
    # 真实运行形态：先做启动配置校验，缺 key 即阻止启动（test 形态跳过）
    if settings.env.lower() != "test":
        problems = collect_startup_problems(settings)
        if problems:
            for p in problems:
                _log.error("startup_missing_config", var=p)
            raise RuntimeError(
                "启动配置缺失：" + "、".join(problems)
                + "。请补全 .env（模板见 .env.example）。"
                "确需降级运行：嵌入可设 EMBED_PROVIDER=local，自动灌入可关 SEED_ON_STARTUP=false。"
            )
    # 骨架启动：幂等 DDL（dev 直接应用，与 docker compose 初始化同步）
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, migrate, settings)
    engine = create_async_engine_for(settings)
    runtime = build_runtime(settings, engine)
    try:
        await runtime.vector.ensure_collection(await runtime.embedder.ensure_dim())
        await _ensure_knowledge_ready(runtime, engine, settings)
    except Exception:
        await runtime.aclose()
        raise
    app.state.runtime = runtime
    try:
        yield
    finally:
        await runtime.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="AP1 偶像 Agent", lifespan=lifespan, docs_url="/api/docs")

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.to_payload()})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "error_type": "validation_error",
                    "detail": "请求参数不合法",
                    "errors": exc.errors(),
                }
            },
        )

    app.include_router(api_router, prefix="/api")

    # 前端静态（微信式单会话网页）由同一进程托管
    if _FRONTEND_DIST.is_dir():
        _dist = _FRONTEND_DIST.resolve()
        app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(_dist / "index.html")

        @app.get("/{path:path}")
        async def spa_fallback(path: str) -> FileResponse:
            # 防目录穿越：解析后必须仍落在 dist 内，否则回退 index
            candidate = (_dist / path).resolve()
            if candidate.is_relative_to(_dist) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(_dist / "index.html")
    else:
        @app.get("/")
        async def root() -> dict[str, Any]:
            return {"message": "AP1 偶像 Agent — 前端未构建，访问 /api 文档"}

    return app


app = create_app()
