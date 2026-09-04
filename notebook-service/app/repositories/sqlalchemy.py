"""SQLAlchemy 2.x（同步）元数据仓储实现。

职责：
- 数据库事务：每个方法开启独立 Session，写路径在 BEGIN IMMEDIATE 下执行；
- 幂等权威检查必须位于同一写事务内，且先于 revision 判断；
- 成功幂等记录与业务写入同事务提交；
- head 推进使用条件 UPDATE（CAS），不是"先查再更新"；
- 只持久化成功的 2xx 写结果，不持久化 4xx/5xx。

SQLAlchemy model 仅在本模块内使用，不直接作为响应模型返回。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import BigInteger, DateTime, Integer, Text, TypeDecorator, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..errors import InternalError, StorageUnavailable
from ..etags import format_etag
from .base import (
    CreateOutcome,
    IdempotencyReplay,
    NotebookRow,
    RevisionRow,
    SaveOutcome,
)

class UTCDateTime(TypeDecorator):
    """以 UTC 存储 datetime；写入必须是 timezone-aware，读取时补回 UTC。"""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime 不允许写入")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class NotebookModel(Base):
    __tablename__ = "notebooks"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    current_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    current_content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class NotebookRevisionModel(Base):
    __tablename__ = "notebook_revisions"

    notebook_id: Mapped[str] = mapped_column(Text, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    blob_key: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class IdempotencyRecordModel(Base):
    __tablename__ = "idempotency_records"

    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    result_notebook_id: Mapped[str] = mapped_column(Text, nullable=False)
    result_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    result_metadata: Mapped[str] = mapped_column(Text, nullable=False)
    response_headers: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


def _notebook_row(model: NotebookModel) -> NotebookRow:
    return NotebookRow(
        id=model.id,
        title=model.title,
        current_revision=model.current_revision,
        current_content_hash=model.current_content_hash,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _revision_row(model: NotebookRevisionModel) -> RevisionRow:
    return RevisionRow(
        notebook_id=model.notebook_id,
        revision=model.revision,
        content_hash=model.content_hash,
        blob_key=model.blob_key,
        size_bytes=model.size_bytes,
        created_at=model.created_at,
    )


def create_result_metadata(
    notebook_id: str,
    title: str,
    revision: int,
    content_hash: str,
    created_at: datetime,
    updated_at: datetime,
) -> dict:
    """POST 成功结果的小型 JSON（不含 content），与幂等表存储一致。"""
    return {
        "notebookId": notebook_id,
        "title": title,
        "revision": revision,
        "contentHash": content_hash,
        "createdAt": created_at.isoformat(),
        "updatedAt": updated_at.isoformat(),
    }


def save_result_metadata(
    notebook_id: str,
    revision: int,
    content_hash: str,
    updated_at: datetime,
    unchanged: bool,
) -> dict:
    """PUT 成功结果 JSON（本身不含 content）。"""
    return {
        "notebookId": notebook_id,
        "revision": revision,
        "contentHash": content_hash,
        "updatedAt": updated_at.isoformat(),
        "unchanged": unchanged,
    }


class SqlAlchemyNotebookRepository:
    def __init__(self, session_factory, idempotency_ttl_seconds: int) -> None:
        self.session_factory = session_factory
        self.idempotency_ttl_seconds = idempotency_ttl_seconds

    # -- create ----------------------------------------------------------

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
        session = self.session_factory()
        try:
            existing = _get_idempotency(session, scope, key)
            if existing is not None:
                return self._resolve_idempotency(
                    session, existing, request_hash, CreateOutcome
                )

            notebook = NotebookModel(
                id=notebook_id,
                title=title,
                current_revision=1,
                current_content_hash=content_hash,
                created_at=now,
                updated_at=now,
            )
            revision = NotebookRevisionModel(
                notebook_id=notebook_id,
                revision=1,
                content_hash=content_hash,
                blob_key=blob_key,
                size_bytes=size_bytes,
                created_at=now,
            )
            session.add(notebook)
            self._insert_revision(
                session,
                notebook_id=notebook_id,
                revision=1,
                content_hash=content_hash,
                blob_key=blob_key,
                size_bytes=size_bytes,
                created_at=now,
            )
            self._insert_idempotency(
                session,
                scope=scope,
                key=key,
                request_hash=request_hash,
                status_code=201,
                notebook_id=notebook_id,
                revision=1,
                result_metadata=create_result_metadata(
                    notebook_id, title, 1, content_hash, now, now
                ),
                response_headers={
                    "Location": f"/api/v1/notebooks/{notebook_id}",
                    "ETag": format_etag(notebook_id, 1),
                },
                now=now,
            )
            session.commit()
            return CreateOutcome(
                kind="created",
                notebook=_notebook_row(notebook),
                revision=_revision_row(revision),
            )
        except OperationalError as error:
            session.rollback()
            raise StorageUnavailable("database") from error
        finally:
            session.close()

    # -- read ------------------------------------------------------------

    def get_notebook(self, notebook_id: str) -> NotebookRow | None:
        session = self.session_factory()
        try:
            model = session.get(NotebookModel, notebook_id)
            return _notebook_row(model) if model is not None else None
        except OperationalError as error:
            raise StorageUnavailable("database") from error
        finally:
            session.close()

    def get_revision(
        self, notebook_id: str, revision: int
    ) -> RevisionRow | None:
        session = self.session_factory()
        try:
            model = session.get(
                NotebookRevisionModel, (notebook_id, revision)
            )
            return _revision_row(model) if model is not None else None
        except OperationalError as error:
            raise StorageUnavailable("database") from error
        finally:
            session.close()

    # -- save ------------------------------------------------------------

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
        session = self.session_factory()
        try:
            # 权威幂等检查：必须位于同一写事务内，且先于 revision 判断。
            existing = _get_idempotency(session, scope, key)
            if existing is not None:
                return self._resolve_idempotency(
                    session, existing, request_hash, SaveOutcome
                )

            notebook = session.get(NotebookModel, notebook_id)
            if notebook is None:
                return SaveOutcome(kind="not_found")

            if notebook.current_revision != base_revision:
                return SaveOutcome(
                    kind="revision_conflict",
                    current_revision=notebook.current_revision,
                    current_content_hash=notebook.current_content_hash,
                )

            head_revision = session.get(
                NotebookRevisionModel,
                (notebook_id, notebook.current_revision),
            )
            if head_revision is None:
                raise InternalError(
                    "head revision 行缺失：数据库完整性故障"
                )

            if notebook.current_content_hash == content_hash:
                # 相同内容 no-op：不移动 head、不更新 updated_at，但记录
                # 成功幂等结果，重试窗口内保持相同语义。
                self._insert_idempotency(
                    session,
                    scope=scope,
                    key=key,
                    request_hash=request_hash,
                    status_code=200,
                    notebook_id=notebook_id,
                    revision=notebook.current_revision,
                    result_metadata=save_result_metadata(
                        notebook_id,
                        notebook.current_revision,
                        notebook.current_content_hash,
                        head_revision.created_at,
                        unchanged=True,
                    ),
                    response_headers={
                        "ETag": format_etag(
                            notebook_id, notebook.current_revision
                        )
                    },
                    now=now,
                )
                session.commit()
                return SaveOutcome(
                    kind="unchanged",
                    notebook=_notebook_row(notebook),
                    revision=_revision_row(head_revision),
                )

            new_revision = notebook.current_revision + 1

            # CAS：条件 UPDATE，同事务内推进 head；无返回行说明并发失败。
            result = session.execute(
                update(NotebookModel)
                .where(
                    NotebookModel.id == notebook_id,
                    NotebookModel.current_revision == base_revision,
                )
                .values(
                    current_revision=new_revision,
                    current_content_hash=content_hash,
                    updated_at=now,
                )
                .returning(NotebookModel.current_revision)
            )
            if result.first() is None:
                # BEGIN IMMEDIATE 串行化下不应发生；防御性路径。
                session.rollback()
                current = session.get(NotebookModel, notebook_id)
                return SaveOutcome(
                    kind="revision_conflict",
                    current_revision=current.current_revision,
                    current_content_hash=current.current_content_hash,
                )

            self._insert_revision(
                session,
                notebook_id=notebook_id,
                revision=new_revision,
                content_hash=content_hash,
                blob_key=blob_key,
                size_bytes=size_bytes,
                created_at=now,
            )
            self._insert_idempotency(
                session,
                scope=scope,
                key=key,
                request_hash=request_hash,
                status_code=200,
                notebook_id=notebook_id,
                revision=new_revision,
                result_metadata=save_result_metadata(
                    notebook_id, new_revision, content_hash, now,
                    unchanged=False,
                ),
                response_headers={
                    "ETag": format_etag(notebook_id, new_revision)
                },
                now=now,
            )
            session.commit()
            return SaveOutcome(
                kind="saved",
                revision=RevisionRow(
                    notebook_id=notebook_id,
                    revision=new_revision,
                    content_hash=content_hash,
                    blob_key=blob_key,
                    size_bytes=size_bytes,
                    created_at=now,
                ),
            )
        except OperationalError as error:
            session.rollback()
            raise StorageUnavailable("database") from error
        finally:
            session.close()

    # -- 内部 ------------------------------------------------------------

    def _resolve_idempotency(
        self, session, record, request_hash: str, outcome_cls
    ):
        """相同 key 的权威幂等判断：

        - 相同 request hash：重放原成功结果（含原 ETag，即使 head 已前进）；
        - 不同 request hash：返回 key_conflict（API 层映射为 409）。
        """
        if record.request_hash != request_hash:
            return outcome_cls(kind="key_conflict")

        notebook = session.get(NotebookModel, record.result_notebook_id)
        revision = session.get(
            NotebookRevisionModel,
            (record.result_notebook_id, record.result_revision),
        )
        if notebook is None or revision is None:
            # 与成功幂等记录同事务写入的数据不可能缺失；属于完整性故障。
            raise InternalError("幂等记录引用的 Notebook/revision 不存在")

        replay = IdempotencyReplay(
            status_code=record.status_code,
            notebook_id=record.result_notebook_id,
            revision=record.result_revision,
            result_metadata=json.loads(record.result_metadata),
            response_headers=json.loads(record.response_headers),
        )
        return outcome_cls(
            kind="replayed",
            notebook=_notebook_row(notebook),
            revision=_revision_row(revision),
            replay=replay,
        )

    def _insert_revision(
        self,
        session,
        *,
        notebook_id: str,
        revision: int,
        content_hash: str,
        blob_key: str,
        size_bytes: int,
        created_at: datetime,
    ) -> None:
        """revision 行只允许 INSERT（不可变历史）。独立方法便于失败边界测试。"""
        session.add(
            NotebookRevisionModel(
                notebook_id=notebook_id,
                revision=revision,
                content_hash=content_hash,
                blob_key=blob_key,
                size_bytes=size_bytes,
                created_at=created_at,
            )
        )

    def _insert_idempotency(
        self,
        session,
        *,
        scope: str,
        key: str,
        request_hash: str,
        status_code: int,
        notebook_id: str,
        revision: int,
        result_metadata: dict,
        response_headers: dict,
        now: datetime,
    ) -> None:
        session.add(
            IdempotencyRecordModel(
                scope=scope,
                key=key,
                request_hash=request_hash,
                status_code=status_code,
                result_notebook_id=notebook_id,
                result_revision=revision,
                result_metadata=json.dumps(
                    result_metadata, ensure_ascii=False, sort_keys=True
                ),
                response_headers=json.dumps(
                    response_headers, ensure_ascii=False, sort_keys=True
                ),
                created_at=now,
                expires_at=now
                + timedelta(seconds=self.idempotency_ttl_seconds),
            )
        )


def _get_idempotency(session, scope: str, key: str):
    return session.get(IdempotencyRecordModel, (scope, key))
