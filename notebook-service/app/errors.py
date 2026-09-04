"""Domain error 与 HTTP 映射。

Service/repository 层只抛出本模块的异常；API 层统一映射为 OpenAPI 中的
状态码与错误 envelope。不在 route 中直接抛 HTTPException（旧 POC route 除外）。
"""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """所有可预期业务错误的基类。"""

    code = "INTERNAL_ERROR"
    http_status = 500
    default_message = "An unexpected internal error occurred"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message
        self.details = details


class MalformedJson(DomainError):
    code = "MALFORMED_JSON"
    http_status = 400
    default_message = "Request body is not valid JSON"


class InvalidRequest(DomainError):
    code = "INVALID_REQUEST"
    http_status = 422
    default_message = "Request does not match the API contract"


class InvalidNotebook(DomainError):
    code = "INVALID_NOTEBOOK"
    http_status = 422
    default_message = "Notebook content is not valid nbformat 4"

    @classmethod
    def at(cls, path: str, reason: str) -> "InvalidNotebook":
        """携带 JSON Pointer 风格 details 的便捷构造，例如 /content/cells/2/id。"""
        return cls(details={"path": path, "reason": reason})


class NotebookNotFound(DomainError):
    code = "NOTEBOOK_NOT_FOUND"
    http_status = 404
    default_message = "Notebook was not found"

    @classmethod
    def for_id(cls, notebook_id: str) -> "NotebookNotFound":
        return cls(details={"notebookId": notebook_id})


class RevisionNotFound(DomainError):
    code = "REVISION_NOT_FOUND"
    http_status = 404
    default_message = "Notebook revision was not found"

    @classmethod
    def for_revision(cls, notebook_id: str, revision: int) -> "RevisionNotFound":
        return cls(
            details={"notebookId": notebook_id, "revision": revision},
        )


class RevisionConflict(DomainError):
    code = "REVISION_CONFLICT"
    http_status = 409
    default_message = "Notebook has been modified"

    def __init__(
        self,
        base_revision: int,
        current_revision: int,
        current_content_hash: str,
    ) -> None:
        super().__init__(
            details={
                "baseRevision": base_revision,
                "currentRevision": current_revision,
                "currentContentHash": current_content_hash,
            }
        )


class IdempotencyKeyReused(DomainError):
    code = "IDEMPOTENCY_KEY_REUSED"
    http_status = 409
    default_message = "Idempotency key was already used for a different request"


class PayloadTooLarge(DomainError):
    code = "PAYLOAD_TOO_LARGE"
    http_status = 413
    default_message = "Notebook exceeds the configured size limit"

    def __init__(self, limit_bytes: int) -> None:
        super().__init__(details={"limitBytes": limit_bytes})


class StorageUnavailable(DomainError):
    code = "STORAGE_UNAVAILABLE"
    http_status = 503
    default_message = "Notebook storage is temporarily unavailable"

    def __init__(self, dependency: str | None = None) -> None:
        super().__init__(
            details={"dependency": dependency} if dependency else None
        )


class InternalError(DomainError):
    """内部完整性故障（如 Blob 哈希不一致）。客户端只看到 500 INTERNAL_ERROR。"""

    code = "INTERNAL_ERROR"
    http_status = 500
    default_message = "An unexpected internal error occurred"
