"""领域错误：携带稳定错误码与 HTTP 状态，便于 API 层统一处理。"""

from __future__ import annotations


class ReliefError(Exception):
    """所有可预期业务错误的基类。"""

    code = "domain_error"
    status = 400

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class ValidationError(ReliefError):
    code = "invalid_request"
    status = 400


class NotFoundError(ReliefError):
    code = "not_found"
    status = 404


class StateConflictError(ReliefError):
    """对象存在但当前状态不允许该操作（如已完成的安排再被修改）。"""

    code = "state_conflict"
    status = 409


class PermissionDeniedError(ReliefError):
    code = "forbidden"
    status = 403
