"""Health 接口测试（指引 §15.7）。"""
from __future__ import annotations

import pytest

from app.config import Settings


@pytest.fixture()
def broken_db_app(migrated_env, tmp_path):
    from app.main import create_app

    # 把数据库路径指向一个已存在的"目录"：sqlite 无法把它作为数据库文件打开，
    # 每次连接都报 OperationalError，模拟数据库不可用。
    (tmp_path / "not-a-file-db").mkdir()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'not-a-file-db'}",
        blob_root=migrated_env["blob_root"],
        max_request_bytes=1024,
        idempotency_ttl_seconds=86400,
    )
    return create_app(settings)


def test_liveness_ok_when_healthy(client):
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"]


def test_liveness_ok_even_when_database_down(broken_db_app):
    from fastapi.testclient import TestClient

    with TestClient(broken_db_app) as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_readiness_ok_when_healthy(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_503_when_database_unreachable(broken_db_app):
    from fastapi.testclient import TestClient

    with TestClient(broken_db_app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        envelope = response.json()["error"]
        assert envelope["code"] == "STORAGE_UNAVAILABLE"
        assert response.headers["x-request-id"]


def test_readiness_503_when_blob_root_missing(client, settings):
    import shutil

    shutil.rmtree(settings.blob_root)
    response = client.get("/health/ready")
    assert response.status_code == 503
    envelope = response.json()["error"]
    assert envelope["code"] == "STORAGE_UNAVAILABLE"
    assert envelope["requestId"] == response.headers["x-request-id"]
    assert response.headers["retry-after"]


def test_readiness_recovers_when_blob_root_restored(client, settings):
    import shutil

    shutil.rmtree(settings.blob_root)
    assert client.get("/health/ready").status_code == 503

    settings.blob_root.mkdir(parents=True)
    assert client.get("/health/ready").status_code == 200
