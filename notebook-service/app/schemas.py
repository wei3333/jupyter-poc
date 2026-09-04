"""v1 Pydantic request/response schema（信封级）。

`content` 字段以 dict 透传：文档结构（Cell/output 等）由 nbformat_codec 依据
官方 nbformat schema 做最终校验（OpenAPI 不取代 nbformat schema）。请求/响应
信封字段、required 与状态码与 docs/api/notebook-service-v1.openapi.yaml 一致。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

NOTEBOOK_ID_PATTERN = r"^nb_[A-Za-z0-9_-]{12,64}$"
CONTENT_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"

NotebookId = Annotated[str, Field(pattern=NOTEBOOK_ID_PATTERN)]
Revision = Annotated[int, Field(ge=1)]
ContentHash = Annotated[str, Field(pattern=CONTENT_HASH_PATTERN)]


def _utc_z(value: datetime) -> str:
    """RFC 3339 UTC `Z` 格式输出。"""
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class _ContractModel(BaseModel):
    """v1 契约对象统一 additionalProperties: false。"""

    model_config = ConfigDict(extra="forbid")


class CreateNotebookRequest(_ContractModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    content: dict[str, Any] | None = None


class SaveNotebookRequest(_ContractModel):
    baseRevision: Revision
    content: dict[str, Any]


class NotebookDocumentResponse(_ContractModel):
    notebookId: NotebookId
    title: str
    revision: Revision
    contentHash: ContentHash
    createdAt: datetime
    updatedAt: datetime
    content: dict[str, Any]

    @field_serializer("createdAt", "updatedAt")
    def _serialize_dt(self, value: datetime) -> str:
        return _utc_z(value)


class SaveNotebookResponse(_ContractModel):
    notebookId: NotebookId
    revision: Revision
    contentHash: ContentHash
    updatedAt: datetime
    unchanged: bool

    @field_serializer("updatedAt")
    def _serialize_dt(self, value: datetime) -> str:
        return _utc_z(value)


class HealthResponse(_ContractModel):
    status: Literal["ok"]


class ErrorBody(_ContractModel):
    code: str
    message: str
    requestId: str
    details: dict[str, Any] | None = None


class ErrorResponse(_ContractModel):
    error: ErrorBody
