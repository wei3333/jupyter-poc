"""旧 POC route 保留测试：响应结构不变、不出现在 v1 OpenAPI、不触碰 POC 数据。"""
from __future__ import annotations

import pytest

from app.repository import LocalNotebookRepository


@pytest.fixture()
def legacy_repo(tmp_path, monkeypatch):
    """把旧 route 的模块级 repository 指向临时目录，避免写真实 POC 数据。"""
    import app.api.legacy as legacy_module

    repo = LocalNotebookRepository(tmp_path / "legacy-notebooks")
    monkeypatch.setattr(legacy_module, "repository", repo)
    return repo


def test_legacy_create_and_get_preserve_old_response_shape(client, legacy_repo):
    content = {"nbformat": 4, "cells": []}
    response = client.post("/api/notebooks", json={"content": content})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"notebookId", "revision", "content"}
    assert body["revision"] == 1
    assert body["content"] == content

    response = client.get(f"/api/notebooks/{body['notebookId']}")
    assert response.status_code == 200
    assert response.json() == body


def test_legacy_get_not_found_keeps_old_error_shape(client):
    response = client.get("/api/notebooks/nb_000000000000")
    assert response.status_code == 404
    assert response.json() == {"detail": "Notebook not found"}


def test_legacy_put_conflict_keeps_old_error_shape(client, legacy_repo):
    content = {"nbformat": 4, "cells": []}
    nb_id = client.post("/api/notebooks", json={"content": content}).json()["notebookId"]
    client.put(f"/api/notebooks/{nb_id}", json={"baseRevision": 1, "content": content})

    response = client.put(
        f"/api/notebooks/{nb_id}",
        json={"baseRevision": 1, "content": content},
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": {"message": "Revision conflict", "currentRevision": 2}
    }


def test_legacy_validation_error_keeps_default_422_shape(client):
    response = client.post("/api/notebooks", json={})
    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"detail"}
    assert isinstance(body["detail"], list)


def test_legacy_routes_not_in_openapi(client):
    spec = client.get("/openapi.json").json()
    assert "/api/notebooks" not in spec["paths"]
    assert "/api/notebooks/{notebook_id}" not in spec["paths"]
