"""领域错误与错误码。

所有错误都带有稳定的机器可读 ``code``，供 HTTP 层映射状态码，
也供重复回执场景精确判断"返回原决定"。
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """全部领域错误的基类。"""

    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class ConflictError(DomainError):
    """命令与账本当前状态冲突。

    业务上的"内容冲突"不会以本错误长期存在：除明确的状态冲突
    （如终态安排再次修改）外，冲突事实应登记人工复核。
    """

    code = "conflict"
    http_status = 409


class ValidationError(DomainError):
    code = "invalid_request"
    http_status = 422


class PermissionDeniedError(DomainError):
    code = "forbidden"
    http_status = 403


class AuthenticationError(DomainError):
    code = "unauthorized"
    http_status = 401


class IntegrityError(DomainError):
    """哈希链或重放不一致——账本被破坏，拒绝继续写入。"""

    code = "ledger_integrity"
    http_status = 500
