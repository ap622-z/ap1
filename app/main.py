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

from app.api import router as api_router
from app.config import get_settings
from app.context import build_runtime
from app.db import create_async_engine_for, migrate
from app.errors import AppError
from app.logging_setup import setup_logging

_FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


_log = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _log
    settings = get_settings()
    setup_logging(settings)
    from app.logging_setup import get_logger

    _log = get_logger("startup")
    # 骨架启动：幂等 DDL（dev 直接应用，与 docker compose 初始化同步）
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, migrate, settings)
    engine = create_async_engine_for(settings)
    runtime = build_runtime(settings, engine)
    await runtime.vector.ensure_collection(await runtime.embedder.ensure_dim())
    # 容器自包含：知识库为空时自动摄入（幂等），做到 docker compose up 即开即用
    if settings.seed_on_startup and await runtime.repo.count_idol_infos() == 0:
        from app.cli.ingest import ingest_knowledge

        _log.info("knowledge_empty_seed_start")
        try:
            await ingest_knowledge(engine, settings, log=_log)
            _log.info("knowledge_seeded")
        except Exception:
            _log.exception("knowledge_seed_failed_continue_booting")
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
