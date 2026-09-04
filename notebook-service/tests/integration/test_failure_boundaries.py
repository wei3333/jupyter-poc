"""失败边界测试（指引 §15.6）。"""
from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from app.config import Settings
from tests.conftest import make_code_cell, make_notebook


def _post(client, body, key="k"):
    return client.post(
        "/api/v1/notebooks", json=body, headers={"Idempotency-Key": key}
    )


def _db_counts(context):
    with context.engine.connect() as conn:
        return {
            "notebooks": conn.execute(text("SELECT count(*) FROM notebooks")).scalar(),
            "revisions": conn.execute(text("SELECT count(*) FROM notebook_revisions")).scalar(),
            "idem": conn.execute(text("SELECT count(*) FROM idempotency_records")).scalar(),
        }


# -- Blob 写失败 -----------------------------------------------------------------


def test_blob_write_failure_creates_nothing(client, context, monkeypatch):
    def failing_put(digest, data):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(context.blob_store, "put", failing_put)
    response = _post(client, {"title": "x"}, key="k")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STORAGE_UNAVAILABLE"
    assert response.headers["retry-after"]
    assert _db_counts(context) == {"notebooks": 0, "revisions": 0, "idem": 0}


def test_db_failure_after_blob_leaves_invisible_orphan(client, context, monkeypatch):
    """Blob 成功而数据库失败：对象可以成为孤儿，但 GET 不可见，head 不改变。"""
    from app.errors import StorageUnavailable

    def failing_create(**kwargs):
        raise StorageUnavailable("database")

    monkeypatch.setattr(context.repository, "create_notebook", failing_create)
    response = _post(client, {"title": "x"}, key="k")
    assert response.status_code == 503
    assert _db_counts(context)["notebooks"] == 0

    # 孤儿 Blob 已写入磁盘
    blobs = list((context.settings.blob_root).rglob("*.ipynb"))
    assert len(blobs) == 1

    # 任何 GET 都看不到它
    response = client.get("/api/v1/notebooks/nb_" + "f" * 32)
    assert response.status_code == 404


def test_revision_insert_failure_rolls_back_head(quiet_client, context, monkeypatch):
    """revision 插入失败：head UPDATE 回滚。"""
    client = quiet_client

    class FailingRepo(context.repository.__class__):
        def _insert_revision(self, session, **kwargs):
            if kwargs["revision"] > 1:
                raise RuntimeError("simulated revision insert failure")
            return super()._insert_revision(session, **kwargs)

    failing = FailingRepo(context.session_factory, 86400)
    monkeypatch.setattr(context, "repository", failing)
    monkeypatch.setattr(context.service, "repository", failing)

    response = _post(client, {"title": "x"}, key="create")
    nb_id = response.json()["notebookId"]

    response = client.put(
        f"/api/v1/notebooks/{nb_id}",
        json={"baseRevision": 1, "content": make_notebook([make_code_cell()])},
        headers={"Idempotency-Key": "save"},
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"

    head = client.get(f"/api/v1/notebooks/{nb_id}")
    assert head.json()["revision"] == 1
    counts = _db_counts(context)
    assert counts["revisions"] == 1
    assert counts["idem"] == 1  # 无 save 幂等记录


def test_idempotency_insert_failure_rolls_back_head(quiet_client, context, monkeypatch):
    client = quiet_client

    class FailingRepo(context.repository.__class__):
        def _insert_idempotency(self, session, **kwargs):
            if kwargs["revision"] > 1:
                raise RuntimeError("simulated idempotency insert failure")
            return super()._insert_idempotency(session, **kwargs)

    failing = FailingRepo(context.session_factory, 86400)
    monkeypatch.setattr(context, "repository", failing)
    monkeypatch.setattr(context.service, "repository", failing)

    response = _post(client, {"title": "x"}, key="create")
    nb_id = response.json()["notebookId"]

    response = client.put(
        f"/api/v1/notebooks/{nb_id}",
        json={"baseRevision": 1, "content": make_notebook([make_code_cell()])},
        headers={"Idempotency-Key": "save"},
    )
    assert response.status_code == 500
    head = client.get(f"/api/v1/notebooks/{nb_id}")
    assert head.json()["revision"] == 1
    assert _db_counts(context)["revisions"] == 1


# -- Blob 篡改 / 缺失 ------------------------------------------------------------


def test_tampered_blob_detected_on_get(client, context):
    response = _post(client, {"title": "x"}, key="k")
    nb_id = response.json()["notebookId"]

    blob = next((context.settings.blob_root).rglob("*.ipynb"))
    blob.write_bytes(b'{"nbformat": 4, "tampered": true}')

    response = client.get(f"/api/v1/notebooks/{nb_id}")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["requestId"] == response.headers["x-request-id"]


def test_missing_blob_detected_on_get(client, context):
    response = _post(client, {"title": "x"}, key="k")
    nb_id = response.json()["notebookId"]

    blob = next((context.settings.blob_root).rglob("*.ipynb"))
    blob.unlink()

    response = client.get(f"/api/v1/notebooks/{nb_id}")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


def test_blob_read_oserror_maps_to_storage_unavailable(client, context, monkeypatch):
    response = _post(client, {"title": "x"}, key="k")
    nb_id = response.json()["notebookId"]

    def failing_get(key):
        raise OSError("simulated I/O error")

    monkeypatch.setattr(context.blob_store, "get", failing_get)
    response = client.get(f"/api/v1/notebooks/{nb_id}")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STORAGE_UNAVAILABLE"
    assert response.headers["retry-after"]


# -- 413 ------------------------------------------------------------------------


@pytest.fixture()
def small_app(migrated_env):
    from app.main import create_app

    settings = Settings(
        database_url=migrated_env["db_url"],
        blob_root=migrated_env["blob_root"],
        max_request_bytes=200,
        idempotency_ttl_seconds=86400,
    )
    return create_app(settings)


def test_oversized_request_rejected_before_any_processing(
    small_app, monkeypatch, migrated_env
):
    from fastapi.testclient import TestClient

    with TestClient(small_app) as client:
        context = small_app.state.context

        called = {"service": False}

        def spy(**kwargs):
            called["service"] = True
            raise AssertionError("service 不应被调用")

        monkeypatch.setattr(context.service, "create", spy)

        big_body = {"title": "x" * 400}
        response = client.post(
            "/api/v1/notebooks",
            json=big_body,
            headers={"Idempotency-Key": "k"},
        )
        assert response.status_code == 413
        envelope = response.json()["error"]
        assert envelope["code"] == "PAYLOAD_TOO_LARGE"
        assert envelope["details"] == {"limitBytes": 200}
        assert envelope["requestId"] == response.headers["x-request-id"]
        assert called == {"service": False}

        with context.engine.connect() as conn:
            count = conn.execute(
                text("SELECT count(*) FROM notebooks")
            ).scalar()
        assert count == 0


def test_request_under_limit_succeeds(small_app):
    from fastapi.testclient import TestClient

    with TestClient(small_app) as client:
        response = client.post(
            "/api/v1/notebooks",
            json={"title": "small"},
            headers={"Idempotency-Key": "k"},
        )
        assert response.status_code == 201
