"""Notebook 元数据 repository 协议与行对象。

Service 层只依赖本模块定义的行对象与 outcome；具体持久化（SQLite/PostgreSQL）
在 repositories/sqlalchemy.py 中实现。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class NotebookRow:
    id: str
    title: str
    current_revision: int
    current_content_hash: str
    created_at: datetime
    updated_at: datetime
    # NULL 表示正常；非 NULL 表示软删除。
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class NotebookSummaryRow:
    """列表项：只含 notebooks metadata，不读取 revision 行或 Blob。"""

    notebook_id: str
    title: str
    current_revision: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RevisionRow:
    notebook_id: str
    revision: int
    content_hash: str
    blob_key: str
    size_bytes: int
    created_at: datetime


@dataclass(frozen=True)
class IdempotencyReplay:
    """已保存的成功幂等结果：不含 Notebook content，按 §6.3 重组原响应。"""

    status_code: int
    notebook_id: str
    revision: int
    result_metadata: dict[str, Any]
    response_headers: dict[str, str]


@dataclass(frozen=True)
class CreateOutcome:
    kind: Literal["created", "replayed", "key_conflict"]
    notebook: NotebookRow | None = None
    revision: RevisionRow | None = None
    replay: IdempotencyReplay | None = None


@dataclass(frozen=True)
class SaveOutcome:
    kind: Literal[
        "saved", "unchanged", "replayed", "key_conflict",
        "not_found", "revision_conflict",
    ]
    notebook: NotebookRow | None = None
    revision: RevisionRow | None = None
    replay: IdempotencyReplay | None = None
    # revision_conflict 时的当前 head 信息
    current_revision: int | None = None
    current_content_hash: str | None = None


@dataclass(frozen=True)
class DeleteOutcome:
    kind: Literal["deleted", "already_deleted", "not_found"]


class NotebookRepository(Protocol):
    """元数据仓储。方法内部负责数据库事务、约束、幂等记录和 CAS。"""

    def create_notebook(
        self,
        *,
        scope: str,
        key: str,
        request_hash: str,
        notebook_id: str,
        title: str,
        content_hash: str,
        blob_key: str,
        size_bytes: int,
        now: datetime,
    ) -> CreateOutcome:
        """创建 Notebook + revision 1 + 成功幂等记录（同一写事务）。"""
        ...

    def get_notebook(self, notebook_id: str) -> NotebookRow | None:
        ...

    def get_revision(
        self, notebook_id: str, revision: int
    ) -> RevisionRow | None:
        ...

    def list_notebooks(
        self,
        *,
        limit: int,
        cursor_updated_at: datetime | None = None,
        cursor_notebook_id: str | None = None,
    ) -> list[NotebookSummaryRow]:
        """keyset 分页：按 updated_at DESC, id DESC 查询 limit + 1 条。

        - 只读 notebooks metadata；禁止读取 revision 行、Blob 或计算 content hash；
        - cursor_updated_at/cursor_notebook_id 为解码后的 cursor 值，两者要么
          同时为 None（第一页），要么同时给定；
        - 返回 limit + 1 条，由 service 截断并生成 nextCursor。
        """
        ...

    def save_notebook(
        self,
        *,
        scope: str,
        key: str,
        request_hash: str,
        notebook_id: str,
        base_revision: int,
        content_hash: str,
        blob_key: str,
        size_bytes: int,
        now: datetime,
    ) -> SaveOutcome:
        """保存：幂等权威检查 → notebook 存在性 → CAS → no-op/新 revision。"""
        ...

    def delete_notebook(
        self, *, notebook_id: str, now: datetime
    ) -> DeleteOutcome:
        """软删除：仅 SET deleted_at = now。

        - 从未存在：not_found；已有 deleted_at：already_deleted；否则 deleted；
        - 不推进 revision、不改 current_content_hash/updated_at；
        - 不删除 revision 行或 Blob，不访问 BlobStore，不调用 Runtime Plane。
        """
        ...
