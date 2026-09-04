"""Notebook 应用服务（use case 编排）。

负责创建/读取/保存用例的步骤顺序（见指引 §11），不依赖 FastAPI Request/Response
类型。并发正确性由 repository 的写事务 + CAS 保证，本层不做进程内加锁。
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from ..errors import (
    IdempotencyKeyReused,
    InternalError,
    InvalidRequest,
    NotebookNotFound,
    RevisionConflict,
    RevisionNotFound,
    StorageUnavailable,
)
from ..etags import format_etag
from ..nbformat_codec import (
    canonical_json_bytes,
    compute_content_hash,
    default_notebook,
    normalize_for_create,
    sha256_hex,
    validate_for_save,
)
from ..repositories.base import NotebookRepository, RevisionRow
from ..storage.base import BlobNotFoundError, BlobStore

logger = logging.getLogger("notebook_service")

# scope = 调用方 + method + normalized path；POC 调用方为 anonymous。
CALLER = "anonymous"
DEFAULT_TITLE = "Untitled Notebook"
MAX_TITLE_LENGTH = 255

CREATE_PATH_TEMPLATE = "/api/v1/notebooks"
NOTEBOOK_PATH_TEMPLATE = "/api/v1/notebooks/{notebookId}"


@dataclass(frozen=True)
class CreateResult:
    status_code: int
    notebook_id: str
    title: str
    revision: int
    content_hash: str
    created_at: datetime
    updated_at: datetime
    content: dict
    headers: dict[str, str]  # Location + ETag


@dataclass(frozen=True)
class GetResult:
    kind: Literal["ok", "not_modified"]
    notebook_id: str
    title: str
    revision: int
    content_hash: str
    created_at: datetime
    updated_at: datetime
    content: dict | None
    etag: str


@dataclass(frozen=True)
class SaveResult:
    notebook_id: str
    revision: int
    content_hash: str
    updated_at: datetime
    unchanged: bool
    etag: str


class NotebookService:
    def __init__(self, repository: NotebookRepository, blob_store: BlobStore):
        self.repository = repository
        self.blob_store = blob_store

    # -- create ----------------------------------------------------------

    def create(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        title: str | None,
        content: dict | None,
    ) -> CreateResult:
        now = _utcnow()
        resolved_title = self._resolve_title(title)

        doc = (
            normalize_for_create(content)
            if content is not None
            else default_notebook()
        )
        content_bytes = canonical_json_bytes(doc)
        content_hash = compute_content_hash(doc)

        # Blob 先于 metadata 发布；数据库事务失败只会留下不可见孤儿 Blob。
        try:
            blob_key = self.blob_store.put(
                sha256_hex(content_bytes), content_bytes
            )
        except OSError as error:
            raise StorageUnavailable("blob") from error

        notebook_id = f"nb_{uuid.uuid4().hex}"
        outcome = self.repository.create_notebook(
            scope=self._scope("POST", CREATE_PATH_TEMPLATE),
            key=idempotency_key,
            request_hash=request_hash,
            notebook_id=notebook_id,
            title=resolved_title,
            content_hash=content_hash,
            blob_key=blob_key,
            size_bytes=len(content_bytes),
            now=now,
        )

        if outcome.kind == "key_conflict":
            raise IdempotencyKeyReused()
        if outcome.kind == "replayed":
            # 按保存的 Notebook ID/revision 读取不可变 revision content，
            # 与 result_metadata 组合成原成功响应（含原 ETag）。
            replay = outcome.replay
            assert outcome.notebook is not None and outcome.revision is not None
            content = self._read_content(
                outcome.notebook.id, outcome.revision
            )
            return CreateResult(
                status_code=replay.status_code,
                notebook_id=outcome.notebook.id,
                title=outcome.notebook.title,
                revision=outcome.revision.revision,
                content_hash=outcome.revision.content_hash,
                created_at=outcome.notebook.created_at,
                updated_at=outcome.revision.created_at,
                content=content,
                headers=dict(replay.response_headers),
            )

        assert outcome.notebook is not None and outcome.revision is not None
        return CreateResult(
            status_code=201,
            notebook_id=outcome.notebook.id,
            title=resolved_title,
            revision=1,
            content_hash=content_hash,
            created_at=now,
            updated_at=now,
            content=doc,
            headers={
                "Location": f"/api/v1/notebooks/{outcome.notebook.id}",
                "ETag": format_etag(outcome.notebook.id, 1),
            },
        )

    # -- read ------------------------------------------------------------

    def get(
        self, notebook_id: str, if_none_match: str | None
    ) -> GetResult:
        notebook = self.repository.get_notebook(notebook_id)
        if notebook is None:
            raise NotebookNotFound.for_id(notebook_id)

        revision = self.repository.get_revision(
            notebook_id, notebook.current_revision
        )
        if revision is None:
            raise InternalError("head revision 行缺失：数据库完整性故障")

        etag = format_etag(notebook_id, revision.revision)
        if self._etag_matches(if_none_match, etag):
            return GetResult(
                kind="not_modified",
                notebook_id=notebook.id,
                title=notebook.title,
                revision=revision.revision,
                content_hash=revision.content_hash,
                created_at=notebook.created_at,
                updated_at=revision.created_at,
                content=None,
                etag=etag,
            )

        content = self._read_content(notebook_id, revision)
        return GetResult(
            kind="ok",
            notebook_id=notebook.id,
            title=notebook.title,
            revision=revision.revision,
            content_hash=revision.content_hash,
            created_at=notebook.created_at,
            updated_at=revision.created_at,
            content=content,
            etag=etag,
        )

    def get_revision(
        self,
        notebook_id: str,
        revision_number: int,
        if_none_match: str | None,
    ) -> GetResult:
        notebook = self.repository.get_notebook(notebook_id)
        if notebook is None:
            raise NotebookNotFound.for_id(notebook_id)

        revision = self.repository.get_revision(notebook_id, revision_number)
        if revision is None:
            raise RevisionNotFound.for_revision(notebook_id, revision_number)

        etag = format_etag(notebook_id, revision.revision)
        if self._etag_matches(if_none_match, etag):
            return GetResult(
                kind="not_modified",
                notebook_id=notebook.id,
                title=notebook.title,
                revision=revision.revision,
                content_hash=revision.content_hash,
                created_at=notebook.created_at,
                # 历史文档的 updatedAt 是该历史 revision 的创建时间。
                updated_at=revision.created_at,
                content=None,
                etag=etag,
            )

        content = self._read_content(notebook_id, revision)
        return GetResult(
            kind="ok",
            notebook_id=notebook.id,
            title=notebook.title,
            revision=revision.revision,
            content_hash=revision.content_hash,
            created_at=notebook.created_at,
            updated_at=revision.created_at,
            content=content,
            etag=etag,
        )

    # -- save ------------------------------------------------------------

    def save(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        notebook_id: str,
        base_revision: int,
        content: dict,
    ) -> SaveResult:
        now = _utcnow()

        # PUT 严格校验：不执行任何规范化。
        doc = validate_for_save(content)
        content_bytes = canonical_json_bytes(doc)
        digest = sha256_hex(content_bytes)
        content_hash = f"sha256:{digest}"

        # 内容变化时才写 Blob；同 key 已存在（no-op 或去重）时跳过重写。
        blob_key = self.blob_store.key_for(digest)
        if not self.blob_store.exists(blob_key):
            try:
                self.blob_store.put(digest, content_bytes)
            except OSError as error:
                raise StorageUnavailable("blob") from error

        outcome = self.repository.save_notebook(
            scope=self._scope("PUT", NOTEBOOK_PATH_TEMPLATE),
            key=idempotency_key,
            request_hash=request_hash,
            notebook_id=notebook_id,
            base_revision=base_revision,
            content_hash=content_hash,
            blob_key=blob_key,
            size_bytes=len(content_bytes),
            now=now,
        )

        if outcome.kind == "key_conflict":
            raise IdempotencyKeyReused()
        if outcome.kind == "not_found":
            raise NotebookNotFound.for_id(notebook_id)
        if outcome.kind == "revision_conflict":
            raise RevisionConflict(
                base_revision,
                outcome.current_revision,
                outcome.current_content_hash,
            )
        if outcome.kind == "replayed":
            # PUT 响应不含 content，可直接由 result_metadata 重建；返回原 ETag，
            # 即使当前 head 已经继续前进。
            replay = outcome.replay
            metadata = replay.result_metadata
            return SaveResult(
                notebook_id=replay.notebook_id,
                revision=replay.revision,
                content_hash=metadata["contentHash"],
                updated_at=datetime.fromisoformat(metadata["updatedAt"]),
                unchanged=metadata["unchanged"],
                etag=replay.response_headers["ETag"],
            )
        if outcome.kind == "unchanged":
            assert outcome.notebook is not None
            assert outcome.revision is not None
            return SaveResult(
                notebook_id=outcome.notebook.id,
                revision=outcome.notebook.current_revision,
                content_hash=outcome.notebook.current_content_hash,
                # no-op 保存不更新 updatedAt：保持当前 head 的原保存时间。
                updated_at=outcome.revision.created_at,
                unchanged=True,
                etag=format_etag(
                    outcome.notebook.id, outcome.notebook.current_revision
                ),
            )

        assert outcome.revision is not None
        return SaveResult(
            notebook_id=notebook_id,
            revision=outcome.revision.revision,
            content_hash=outcome.revision.content_hash,
            updated_at=outcome.revision.created_at,
            unchanged=False,
            etag=format_etag(notebook_id, outcome.revision.revision),
        )

    # -- 内部 ------------------------------------------------------------

    @staticmethod
    def _scope(method: str, path_template: str) -> str:
        return f"{CALLER}:{method}:{path_template}"

    @staticmethod
    def _etag_matches(if_none_match: str | None, etag: str) -> bool:
        return (
            if_none_match is not None
            and if_none_match.strip() == etag
        )

    @staticmethod
    def _resolve_title(title: str | None) -> str:
        if title is None:
            return DEFAULT_TITLE
        trimmed = title.strip()
        if not trimmed:
            raise InvalidRequest(
                details={
                    "path": "/title",
                    "reason": "title must not be blank after trimming",
                }
            )
        if len(trimmed) > MAX_TITLE_LENGTH:
            raise InvalidRequest(
                details={
                    "path": "/title",
                    "reason": f"title must be at most {MAX_TITLE_LENGTH} characters",
                }
            )
        return trimmed

    def _read_content(
        self, notebook_id: str, revision: RevisionRow
    ) -> dict:
        """读取 Blob 并校验实际字节 SHA-256 == revision.content_hash。

        哈希不符属于存储完整性故障：日志告警并返回 500 INTERNAL_ERROR。
        """
        try:
            data = self.blob_store.get(revision.blob_key)
        except BlobNotFoundError as error:
            logger.warning(
                "Blob 缺失（完整性故障）notebook_id=%s revision=%d key=%s",
                notebook_id, revision.revision, revision.blob_key,
            )
            raise InternalError("Blob 缺失：存储完整性故障") from error
        except OSError as error:
            raise StorageUnavailable("blob") from error

        if sha256_hex(data) != revision.content_hash.removeprefix("sha256:"):
            logger.warning(
                "Blob 哈希不一致（完整性故障）notebook_id=%s revision=%d",
                notebook_id, revision.revision,
            )
            raise InternalError("Blob 哈希不一致：存储完整性故障")

        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            logger.warning(
                "Blob 无法解码 notebook_id=%s revision=%d",
                notebook_id, revision.revision,
            )
            raise InternalError("Blob 无法解码：存储完整性故障") from error


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
