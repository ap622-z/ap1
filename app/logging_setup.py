"""结构化日志：每条记录携带完整 scope（env/user/session/run/step）与事件字段。

trace / 日志是组件唯一的“内部可见面”——服务于观察，不服务于断言。
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from app.config import Settings

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName", "asctime",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, val in getattr(record, "_fields", {}).items():
            if isinstance(val, (str, int, float, bool)) or val is None:
                base[key] = val
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False)


class BoundLogger:
    """带绑定字段的 logger：调用时额外的关键字参数都进结构化字段。

    get_logger('worker', scope=...) 绑定 scope；logger.info('x', step=1)
    则事件字段含 step=1。字段统一走 extra，避免与 LogRecord 保留名冲突。
    """

    def __init__(self, logger: logging.Logger, bound: dict[str, Any] | None = None):
        self._logger = logger
        self._bound = bound or {}

    def _log(self, level: int, msg: str, fields: dict[str, Any] | None, exc_info: bool = False) -> None:
        merged = dict(self._bound)
        if fields:
            merged.update(fields)
        self._logger.log(
            level,
            msg,
            extra={"_fields": merged},
            exc_info=exc_info if exc_info else None,
        )

    def debug(self, msg: str, **fields: Any) -> None:
        self._log(logging.DEBUG, msg, fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._log(logging.INFO, msg, fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._log(logging.WARNING, msg, fields)

    def error(self, msg: str, **fields: Any) -> None:
        # exc_info=True 作为专用关键字，不进结构化字段
        exc = fields.pop("exc_info", False)
        self._log(logging.ERROR, msg, fields, exc_info=bool(exc))

    def exception(self, msg: str, **fields: Any) -> None:
        self._log(logging.ERROR, msg, fields, exc_info=True)


def setup_logging(settings: Settings) -> None:
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    handlers: list[logging.Handler] = []
    if getattr(settings, "log_file", None):
        # 指定了日志文件则落文件（测试/观察用），不再重复打到 stdout，避免管道积压
        handlers.append(logging.FileHandler(settings.log_file, encoding="utf-8"))
    else:
        handlers.append(logging.StreamHandler(sys.stdout))
    for h in handlers:
        h.setFormatter(JsonFormatter())
        root.addHandler(h)
    for noisy in ("httpx", "httpcore", "uvicorn.access", "qdrant_client", "sqlalchemy", "aiomysql"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str, *, scope: dict[str, int | str] | None = None, **fields: Any) -> BoundLogger:
    logger = logging.getLogger(f"ap1.{name}")
    bound: dict[str, Any] = {}
    if scope:
        bound.update(scope)
    if fields:
        bound.update(fields)
    return BoundLogger(logger, bound)
