"""NotebookService 集成测试（列表用例：cursor 解码、nextCursor 生成、不读 Blob）。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.database import build_engine, build_session_factory
from app.errors import InvalidCursor
from app.repositories.sqlalchemy import SqlAlchemyNotebookRepository
from app.services.notebooks import NotebookService
from app.storage.local import LocalBlobStore

CREATE_SCOPE = "anonymous:POST:/api/v1/notebooks"


@pytest.fixture()
def service(migrated_env, tmp_path):
    engine = build_engine(migrated_env["db_url"])
    repository = SqlAlchemyNotebookRepository(
        build_session_factory(engine), idempotency_ttl_seconds=86400
    )
    blob_store = LocalBlobStore(tmp_path / "blobs")
    return NotebookService(repository, blob_store), repository


def _fixed_time(hour: int) -> datetime:
    return datetime(2026, 9, 4, hour, 0, 0, tzinfo=timezone.utc)


def _insert(repo, notebook_id: str, now: datetime, title: str = "t") -> None:
    repo.create_notebook(
        scope=CREATE_SCOPE,
        key=f"create-{uuid.uuid4().hex}",
        request_hash="req-hash",
        notebook_id=notebook_id,
        title=title,
        content_hash="sha256:" + "1" * 64,
        blob_key=f"sha256/11/{'1' * 64}.ipynb",
        size_bytes=10,
        now=now,
    )


def _ids(result) -> list[str]:
    return [row.notebook_id for row in result.items]


def test_list_empty(service):
    svc, _ = service
    result = svc.list(limit=20, cursor=None)
    assert result.items == []
    assert result.next_cursor is None


def test_list_pagination_no_duplicates_no_misses(service):
    svc, repo = service
    ids = [f"nb_{'f' * 31}{i}" for i in range(5)]
    for index, nb_id in enumerate(ids):
        _insert(repo, nb_id, _fixed_time(index))  # ids[4] 最新

    page1 = svc.list(limit=2, cursor=None)
    assert _ids(page1) == [ids[4], ids[3]]
    assert page1.next_cursor is not None

    page2 = svc.list(limit=2, cursor=page1.next_cursor)
    assert _ids(page2) == [ids[2], ids[1]]
    assert page2.next_cursor is not None

    page3 = svc.list(limit=2, cursor=page2.next_cursor)
    assert _ids(page3) == [ids[0]]
    assert page3.next_cursor is None

    collected = _ids(page1) + _ids(page2) + _ids(page3)
    assert len(collected) == len(set(collected)) == 5
    assert set(collected) == set(ids)


def test_list_cursor_beyond_tail_returns_empty(service):
    svc, repo = service
    _insert(repo, f"nb_{'f' * 31}0", _fixed_time(5))

    # keyset DESC：指向比所有行更早位置的合法 cursor → 空页
    from app.cursors import encode_cursor

    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    cursor = encode_cursor(past, "nb_" + "z" * 32)
    page = svc.list(limit=20, cursor=cursor)
    assert page.items == []
    assert page.next_cursor is None


def test_list_stable_when_updated_at_equal(service):
    svc, repo = service
    t = _fixed_time(5)
    _insert(repo, "nb_" + "c" * 32, t)
    _insert(repo, "nb_" + "a" * 32, t)
    _insert(repo, "nb_" + "b" * 32, t)

    page1 = svc.list(limit=2, cursor=None)
    assert _ids(page1) == ["nb_" + "c" * 32, "nb_" + "b" * 32]
    page2 = svc.list(limit=2, cursor=page1.next_cursor)
    assert _ids(page2) == ["nb_" + "a" * 32]
    assert page2.next_cursor is None


def test_list_invalid_cursor_raises_domain_error(service):
    svc, repo = service
    _insert(repo, f"nb_{'f' * 31}0", _fixed_time(5))
    for bad in ["!!!", "bm90IGpzb24=", "eyJ2IjoyfQ=="]:
        with pytest.raises(InvalidCursor):
            svc.list(limit=20, cursor=bad)


def test_list_never_touches_blob_store(service, monkeypatch):
    svc, repo = service
    _insert(repo, f"nb_{'f' * 31}0", _fixed_time(5))
    _insert(repo, f"nb_{'f' * 31}1", _fixed_time(6))

    # 列表期间对 BlobStore 的任何调用都算失败
    def forbidden(*args, **kwargs):
        raise AssertionError("列表路径不得访问 BlobStore")

    monkeypatch.setattr(svc.blob_store, "get", forbidden)
    monkeypatch.setattr(svc.blob_store, "put", forbidden)
    monkeypatch.setattr(svc.blob_store, "exists", forbidden)

    result = svc.list(limit=20, cursor=None)
    assert len(result.items) == 2

    # 分页路径同样不访问 BlobStore
    page1 = svc.list(limit=1, cursor=None)
    assert page1.next_cursor is not None
    svc.list(limit=1, cursor=page1.next_cursor)


# -- 删除（NS-D1-DELETE） -----------------------------------------------------------


def test_delete_then_reads_are_not_found(service):
    svc, repo = service
    nb_id = f"nb_{'f' * 31}0"
    _insert(repo, nb_id, _fixed_time(5))

    svc.delete(nb_id)

    from app.errors import NotebookNotFound
    with pytest.raises(NotebookNotFound):
        svc.get(nb_id, None)
    with pytest.raises(NotebookNotFound):
        svc.get_revision(nb_id, 1, None)


def test_delete_repeat_and_nonexistent(service):
    svc, repo = service
    nb_id = f"nb_{'f' * 31}0"
    _insert(repo, nb_id, _fixed_time(5))

    svc.delete(nb_id)
    svc.delete(nb_id)  # 重复删除不抛错

    from app.errors import NotebookNotFound
    with pytest.raises(NotebookNotFound):
        svc.delete(f"nb_{'a' * 32}")


def test_delete_excludes_from_list(service):
    svc, repo = service
    keep = f"nb_{'a' * 32}"
    removed = f"nb_{'b' * 32}"
    _insert(repo, keep, _fixed_time(5))
    _insert(repo, removed, _fixed_time(5))

    svc.delete(removed)
    result = svc.list(limit=20, cursor=None)
    assert _ids(result) == [keep]


def test_delete_does_not_change_head_state(service):
    svc, repo = service
    nb_id = f"nb_{'f' * 31}0"
    _insert(repo, nb_id, _fixed_time(5))
    before = repo.get_notebook(nb_id)

    svc.delete(nb_id)
    after = repo.get_notebook(nb_id)

    assert after.deleted_at is not None
    assert after.current_revision == before.current_revision
    assert after.current_content_hash == before.current_content_hash
    assert after.updated_at == before.updated_at
    assert repo.get_revision(nb_id, 1) is not None


def test_delete_never_touches_blob_store(service, monkeypatch):
    svc, repo = service
    nb_id = f"nb_{'f' * 31}0"
    _insert(repo, nb_id, _fixed_time(5))

    def forbidden(*args, **kwargs):
        raise AssertionError("删除路径不得访问 BlobStore")

    monkeypatch.setattr(svc.blob_store, "get", forbidden)
    monkeypatch.setattr(svc.blob_store, "put", forbidden)
    monkeypatch.setattr(svc.blob_store, "exists", forbidden)

    svc.delete(nb_id)
    svc.delete(nb_id)  # 重复删除同样不访问
