"""SQLAlchemy repository 集成测试：真实 SQLite + 迁移，覆盖事务/幂等/CAS/no-op。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.database import build_engine, build_session_factory
from app.repositories.sqlalchemy import SqlAlchemyNotebookRepository

CREATE_SCOPE = "anonymous:POST:/api/v1/notebooks"
SAVE_SCOPE = "anonymous:PUT:/api/v1/notebooks/{notebookId}"


@pytest.fixture()
def engine(migrated_env):
    return build_engine(migrated_env["db_url"])


@pytest.fixture()
def repo(engine):
    return SqlAlchemyNotebookRepository(
        build_session_factory(engine), idempotency_ttl_seconds=86400
    )


def _now():
    return datetime.now(timezone.utc)


def _nb_id():
    return f"nb_{uuid.uuid4().hex}"


def _create(
    repo,
    *,
    key="create-key",
    request_hash="req-hash",
    notebook_id=None,
    title="My Notebook",
    content_hash="sha256:" + "1" * 64,
    blob_key=None,
    size_bytes=100,
):
    return repo.create_notebook(
        scope=CREATE_SCOPE,
        key=key,
        request_hash=request_hash,
        notebook_id=notebook_id or _nb_id(),
        title=title,
        content_hash=content_hash,
        blob_key=blob_key or f"sha256/11/{'1' * 64}.ipynb",
        size_bytes=size_bytes,
        now=_now(),
    )


def _count(engine, table):
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()


def _idem_row(engine, scope, key):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT request_hash, status_code, result_notebook_id,"
                " result_revision, result_metadata, response_headers,"
                " created_at, expires_at"
                " FROM idempotency_records WHERE scope=:s AND key=:k"
            ),
            {"s": scope, "k": key},
        ).first()


# -- 迁移 ----------------------------------------------------------------------


def test_migration_creates_expected_tables(engine):
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert {"notebooks", "notebook_revisions", "idempotency_records"} <= tables


# -- 创建 ----------------------------------------------------------------------


def test_create_inserts_notebook_revision_and_idempotency(engine, repo):
    outcome = _create(repo, key="k1")
    assert outcome.kind == "created"
    assert outcome.notebook.current_revision == 1
    assert outcome.revision.revision == 1

    assert _count(engine, "notebooks") == 1
    assert _count(engine, "notebook_revisions") == 1
    assert _count(engine, "idempotency_records") == 1

    row = _idem_row(engine, CREATE_SCOPE, "k1")
    assert row.request_hash == "req-hash"
    assert row.status_code == 201
    assert row.result_revision == 1
    assert '"notebookId"' in row.result_metadata
    assert '"title"' in row.result_metadata
    metadata = json.loads(row.result_metadata)
    assert "content" not in metadata  # 不复制 Notebook content
    assert set(metadata) == {
        "notebookId", "title", "revision", "contentHash",
        "createdAt", "updatedAt",
    }
    headers = json.loads(row.response_headers)
    assert "Location" in headers
    assert "ETag" in headers
    # expires_at 不早于创建后 24 小时（原生 SQL 读出为字符串，需解析）
    assert (
        datetime.fromisoformat(row.expires_at)
        - datetime.fromisoformat(row.created_at)
        == timedelta(seconds=86400)
    )


def test_create_replay_same_key_same_hash(engine, repo):
    notebook_id = _nb_id()
    first = _create(repo, key="k", notebook_id=notebook_id)
    second = _create(repo, key="k", notebook_id=notebook_id)

    assert second.kind == "replayed"
    assert second.replay.notebook_id == first.notebook.id
    assert second.replay.status_code == 201
    assert second.replay.response_headers["ETag"].endswith("-r1\"")
    assert _count(engine, "notebooks") == 1
    assert _count(engine, "notebook_revisions") == 1


def test_create_same_key_different_hash_conflicts(engine, repo):
    _create(repo, key="k", request_hash="hash-1")
    outcome = _create(repo, key="k", request_hash="hash-2")
    assert outcome.kind == "key_conflict"
    assert _count(engine, "notebooks") == 1


def test_create_different_keys_create_different_notebooks(engine, repo):
    _create(repo, key="k1")
    _create(repo, key="k2")
    assert _count(engine, "notebooks") == 2


def test_timestamps_are_utc_aware(engine, repo):
    outcome = _create(repo)
    assert outcome.notebook.created_at.tzinfo is not None
    assert outcome.notebook.updated_at.tzinfo is not None


# -- 读取 ----------------------------------------------------------------------


def test_get_notebook_and_revision(engine, repo):
    outcome = _create(repo, content_hash="sha256:" + "1" * 64)
    nb_id = outcome.notebook.id

    notebook = repo.get_notebook(nb_id)
    assert notebook.title == "My Notebook"
    assert notebook.current_revision == 1

    revision = repo.get_revision(nb_id, 1)
    assert revision.content_hash == "sha256:" + "1" * 64
    assert repo.get_revision(nb_id, 2) is None
    assert repo.get_notebook(_nb_id()) is None


# -- 保存 ----------------------------------------------------------------------


def _save(
    repo,
    notebook_id,
    *,
    key,
    request_hash,
    base_revision,
    content_hash="sha256:" + "2" * 64,
    blob_key=None,
    size_bytes=100,
):
    return repo.save_notebook(
        scope=SAVE_SCOPE,
        key=key,
        request_hash=request_hash,
        notebook_id=notebook_id,
        base_revision=base_revision,
        content_hash=content_hash,
        blob_key=blob_key or f"sha256/22/{'2' * 64}.ipynb",
        size_bytes=size_bytes,
        now=_now(),
    )


def test_save_new_content_advances_head(engine, repo):
    notebook_id = _create(repo, content_hash="sha256:" + "1" * 64).notebook.id

    outcome = _save(repo, notebook_id, key="s1", request_hash="rh1", base_revision=1)
    assert outcome.kind == "saved"
    assert outcome.revision.revision == 2
    assert outcome.revision.content_hash == "sha256:" + "2" * 64

    head = repo.get_notebook(notebook_id)
    assert head.current_revision == 2
    assert head.current_content_hash == "sha256:" + "2" * 64
    assert _count(engine, "notebook_revisions") == 2

    row = _idem_row(engine, SAVE_SCOPE, "s1")
    assert row.status_code == 200
    assert '"unchanged": false' in row.result_metadata
    assert '"ETag"' in row.response_headers


def test_save_same_content_is_noop(engine, repo):
    notebook_id = _create(repo, content_hash="sha256:" + "1" * 64).notebook.id
    before = repo.get_notebook(notebook_id)

    outcome = _save(
        repo, notebook_id, key="s1", request_hash="rh1", base_revision=1,
        content_hash="sha256:" + "1" * 64,
    )
    assert outcome.kind == "unchanged"
    assert outcome.revision.revision == 1

    after = repo.get_notebook(notebook_id)
    assert after.current_revision == 1
    # no-op 不更新 updatedAt
    assert after.updated_at == before.updated_at
    assert _count(engine, "notebook_revisions") == 1

    row = _idem_row(engine, SAVE_SCOPE, "s1")
    assert '"unchanged": true' in row.result_metadata


def test_save_stale_base_is_revision_conflict(engine, repo):
    notebook_id = _create(repo, content_hash="sha256:" + "1" * 64).notebook.id
    _save(repo, notebook_id, key="s1", request_hash="rh1", base_revision=1)

    outcome = _save(
        repo, notebook_id, key="s2", request_hash="rh2", base_revision=1,
    )
    assert outcome.kind == "revision_conflict"
    assert outcome.current_revision == 2
    assert outcome.current_content_hash == "sha256:" + "2" * 64


def test_save_nonexistent_notebook(engine, repo):
    outcome = _save(repo, _nb_id(), key="s", request_hash="rh", base_revision=1)
    assert outcome.kind == "not_found"


def test_save_replay_returns_original_result_after_head_advanced(engine, repo):
    notebook_id = _create(repo, content_hash="sha256:" + "1" * 64).notebook.id

    first = _save(
        repo, notebook_id, key="s1", request_hash="rh1", base_revision=1,
        content_hash="sha256:" + "2" * 64,
    )
    # head 继续前进到 revision 3
    _save(
        repo, notebook_id, key="s2", request_hash="rh2", base_revision=2,
        content_hash="sha256:" + "3" * 64,
    )
    replay = _save(
        repo, notebook_id, key="s1", request_hash="rh1", base_revision=1,
        content_hash="sha256:" + "2" * 64,
    )
    assert replay.kind == "replayed"
    assert replay.replay.revision == 2  # 原成功结果，不是当前 head
    assert replay.replay.response_headers["ETag"].endswith("-r2\"")
    assert replay.replay.result_metadata["unchanged"] is False
    assert _count(engine, "notebook_revisions") == 3


def test_save_replay_unchanged_returns_original_result(engine, repo):
    notebook_id = _create(repo, content_hash="sha256:" + "1" * 64).notebook.id
    first = _save(
        repo, notebook_id, key="s1", request_hash="rh1", base_revision=1,
        content_hash="sha256:" + "1" * 64,
    )
    assert first.kind == "unchanged"

    replay = _save(
        repo, notebook_id, key="s1", request_hash="rh1", base_revision=1,
        content_hash="sha256:" + "1" * 64,
    )
    assert replay.kind == "replayed"
    assert replay.replay.result_metadata["unchanged"] is True


def test_save_same_key_different_hash_conflicts_before_revision_check(engine, repo):
    notebook_id = _create(repo, content_hash="sha256:" + "1" * 64).notebook.id
    _save(repo, notebook_id, key="s1", request_hash="rh1", base_revision=1)

    # 相同 key、不同请求，且 base 已经过期：幂等冲突优先于 revision 冲突
    outcome = _save(
        repo, notebook_id, key="s1", request_hash="rh-other", base_revision=1,
    )
    assert outcome.kind == "key_conflict"


def test_revision_rows_are_immutable_across_saves(engine, repo):
    notebook_id = _create(repo, content_hash="sha256:" + "1" * 64).notebook.id
    first_revision = repo.get_revision(notebook_id, 1)

    _save(repo, notebook_id, key="s1", request_hash="rh1", base_revision=1)

    revision = repo.get_revision(notebook_id, 1)
    assert revision.content_hash == first_revision.content_hash
    assert revision.blob_key == first_revision.blob_key
    assert revision.created_at == first_revision.created_at


def test_save_failure_rolls_back_head_and_idempotency(engine, repo):
    """revision 插入失败：head UPDATE 回滚，不留半提交状态。"""

    class FailingRepo(SqlAlchemyNotebookRepository):
        def _insert_revision(self, session, **kwargs):
            raise RuntimeError("simulated revision insert failure")

    failing = FailingRepo(build_session_factory(engine), 86400)
    notebook_id = _create(repo, content_hash="sha256:" + "1" * 64).notebook.id

    with pytest.raises(RuntimeError):
        _save(failing, notebook_id, key="s1", request_hash="rh1", base_revision=1)

    head = repo.get_notebook(notebook_id)
    assert head.current_revision == 1
    assert head.current_content_hash == "sha256:" + "1" * 64
    assert _count(engine, "notebook_revisions") == 1
    assert _count(engine, "idempotency_records") == 1  # 仅创建的记录


def test_idempotency_insert_failure_rolls_back_head_and_revision(engine, repo):
    """成功幂等记录必须与业务写入同事务提交。"""

    class FailingRepo(SqlAlchemyNotebookRepository):
        def _insert_idempotency(self, session, **kwargs):
            raise RuntimeError("simulated idempotency insert failure")

    failing = FailingRepo(build_session_factory(engine), 86400)
    notebook_id = _create(repo, content_hash="sha256:" + "1" * 64).notebook.id

    with pytest.raises(RuntimeError):
        _save(failing, notebook_id, key="s1", request_hash="rh1", base_revision=1)

    head = repo.get_notebook(notebook_id)
    assert head.current_revision == 1
    assert _count(engine, "notebook_revisions") == 1
    assert _count(engine, "idempotency_records") == 1


def test_create_failure_rolls_back_all_rows(engine, repo):
    class FailingRepo(SqlAlchemyNotebookRepository):
        def _insert_idempotency(self, session, **kwargs):
            raise RuntimeError("simulated failure")

    failing = FailingRepo(build_session_factory(engine), 86400)
    with pytest.raises(RuntimeError):
        _create(failing, key="k1")
    assert _count(engine, "notebooks") == 0
    assert _count(engine, "notebook_revisions") == 0
    assert _count(engine, "idempotency_records") == 0
