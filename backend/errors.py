"""MVP 统一结构化错误：错误类型 + 关联 scope。"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """业务异常基类。所有 MVP 必要分支均继承自它。"""

    error_type = "internal_error"
    status_code = 500

    def __init__(self, detail: str = "", *, scope: dict[str, Any] | None = None, **extra: Any):
        super().__init__(detail)
        self.detail = detail
        self.scope = scope
        self.extra = extra

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error_type": self.error_type,
            "detail": self.detail,
        }
        if self.scope:
            payload["scope"] = self.scope
        payload.update(self.extra)
        return payload


class UnauthorizedError(AppError):
    error_type = "unauthorized"
    status_code = 401


class SessionOutOfScopeError(AppError):
    error_type = "session_out_of_scope"
    status_code = 403


class IdempotencyConflictError(AppError):
    """幂等冲突：同会话同 client_message_id 已存在且内容不一致等。"""

    error_type = "idempotency_conflict"
    status_code = 409


class CapabilityUnavailableError(AppError):
    error_type = "capability_unavailable"
    status_code = 503


class ProcessingFailedError(AppError):
    error_type = "processing_failed"
    status_code = 500


class NotFoundError(AppError):
    error_type = "not_found"
    status_code = 404
