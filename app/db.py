"""MySQL 访问：DDL 迁移（同步，幂等）+ 运行时异步引擎。

表结构以 doc/constitution/database-schema.md 为唯一事实源。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import Settings

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _url(settings: Settings, *, async_: bool) -> str:
    driver = "mysql+aiomysql" if async_ else "mysql+pymysql"
    return (
        f"{driver}://{settings.mysql_user}:{settings.mysql_password}"
        f"@{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_db}"
        f"?charset=utf8mb4"
    )


def _admin_url(settings: Settings, *, async_: bool) -> str:
    driver = "mysql+aiomysql" if async_ else "mysql+pymysql"
    return (
        f"{driver}://{settings.mysql_user}:{settings.mysql_password}"
        f"@{settings.mysql_host}:{settings.mysql_port}/mysql?charset=utf8mb4"
    )


def create_sync_engine(settings: Settings, *, admin: bool = False) -> Engine:
    from sqlalchemy import create_engine

    return create_engine(
        make_url(_admin_url(settings, async_=False) if admin else _url(settings, async_=False)),
        pool_pre_ping=True,
    )


def create_async_engine_for(settings: Settings) -> AsyncEngine:
    # aiomysql 方言自带连接池；pool_pre_ping 由 aiomysql 的池参数负责
    return create_async_engine(make_url(_url(settings, async_=True)))


def _ensure_database_exists(settings: Settings) -> None:
    """目标库不存在时尝试创建（需要全局 CREATE 权限，如 root/管理员）。

    docker compose 场景 MYSQL_DATABASE 已建库，此处通常空操作；失败不阻塞
    （目标库缺失会由后续连接给出明确错误）。
    """
    try:
        engine = create_sync_engine(settings)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return  # 库已存在
    except Exception:
        pass
    try:
        admin = create_sync_engine(settings, admin=True)
        with admin.connect() as conn:
            conn.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS `{settings.mysql_db}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
            conn.commit()
    except Exception:
        # 权限不足则放弃自动建库，交由部署方保证目标库存在
        pass
    finally:
        try:
            admin.dispose()  # type: ignore[name-defined]
        except Exception:
            pass


# 素材驱动的列宽微调（真实素材字段超 schema 初版定义）：
# key = "表.列" -> (字符类型长度)。幂等：仅在现宽不足时 ALTER。
_COLUMN_MIN_WIDTH = {
    "songs.collaboration": 255,
    "songs.creators": 255,
}


def _align_columns(engine: Engine) -> None:
    with engine.connect() as conn:
        for colkey, need_width in _COLUMN_MIN_WIDTH.items():
            table, _, col = colkey.partition(".")
            res = conn.execute(
                text(
                    "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
                ),
                {"t": table, "c": col},
            )
            row = res.first()
            if row and (row[0] is None or int(row[0]) < need_width):
                conn.execute(
                    text(
                        f"ALTER TABLE `{table}` MODIFY COLUMN `{col}` VARCHAR({need_width}) NULL"
                    )
                )
        conn.commit()


def migrate(settings: Settings) -> None:
    """幂等迁移：CREATE TABLE IF NOT EXISTS + 素材驱动列宽对齐，可安全重跑。"""
    _ensure_database_exists(settings)
    engine = create_sync_engine(settings)
    ddl = _SCHEMA_PATH.read_text(encoding="utf-8")
    try:
        with engine.connect() as conn:
            for stmt in _split_sql(ddl):
                conn.execute(text(stmt))
            conn.commit()
        _align_columns(engine)
    finally:
        engine.dispose()


def _split_sql(sql: str) -> list[str]:
    """按分号拆分 SQL（忽略注释行），返回非空语句列表。"""
    statements: list[str] = []
    buf: list[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        buf.append(raw_line)
        if line.endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    return statements
