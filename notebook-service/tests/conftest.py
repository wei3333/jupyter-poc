"""共享 fixtures：临时 SQLite + 临时 Blob 根目录 + 迁移。

测试不读写仓库中的真实 notebook-service/data。
"""
from __future__ import annotations

from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parent.parent


def run_migrations(db_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(SERVICE_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(config, "head")


@pytest.fixture()
def migrated_env(tmp_path, monkeypatch):
    """临时目录环境变量 + 已迁移的临时 SQLite 数据库。"""
    db_path = tmp_path / "v1" / "notebooks.sqlite3"
    blob_root = tmp_path / "v1" / "blobs"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("NOTEBOOK_DATABASE_URL", db_url)
    monkeypatch.setenv("NOTEBOOK_BLOB_ROOT", str(blob_root))
    run_migrations(db_url)
    return {
        "db_url": db_url,
        "db_path": db_path,
        "blob_root": blob_root,
    }


@pytest.fixture()
def settings(migrated_env):
    from app.config import Settings

    return Settings(
        database_url=migrated_env["db_url"],
        blob_root=migrated_env["blob_root"],
        max_request_bytes=20 * 1024 * 1024,
        idempotency_ttl_seconds=86400,
    )


@pytest.fixture()
def app(settings):
    from app.main import create_app

    return create_app(settings)


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def context(app):
    return app.state.context


@pytest.fixture()
def quiet_client(app):
    """未处理异常经 ServerErrorMiddleware 处理后总是会 re-raise（Starlette
    语义，即使已发送 500 响应）；这里关闭 TestClient 的异常重抛。"""
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# -- 常用构造 ---------------------------------------------------------------


def make_code_cell(cell_id: str = "cell_1", source="print(1)") -> dict:
    return {
        "id": cell_id,
        "cell_type": "code",
        "metadata": {},
        "source": source,
        "execution_count": None,
        "outputs": [],
    }


def make_notebook(cells: list | None = None, minor: int = 5, **extra) -> dict:
    doc = {
        "nbformat": 4,
        "nbformat_minor": minor,
        "metadata": {},
        "cells": [] if cells is None else cells,
    }
    doc.update(extra)
    return doc


@pytest.fixture()
def code_cell():
    return make_code_cell


@pytest.fixture()
def notebook_doc():
    return make_notebook
