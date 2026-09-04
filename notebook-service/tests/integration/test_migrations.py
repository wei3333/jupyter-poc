"""Alembic migration 测试（NS-D1-DELETE：0002 增加 nullable deleted_at）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

SERVICE_DIR = Path(__file__).resolve().parent.parent.parent


def _config(db_url: str) -> Config:
    config = Config(str(SERVICE_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", db_url)
    return config


def test_migration_0002_preserves_existing_rows(tmp_path):
    """存量数据在 0001 → head 升级后完整保留，deleted_at 为 NULL；可正常回滚。"""
    db_path = tmp_path / "v1" / "nb.sqlite3"
    db_path.parent.mkdir(parents=True)
    db_url = f"sqlite:///{db_path}"
    config = _config(db_url)

    # 1) 先升级到 0001（0002 之前的 schema）
    command.upgrade(config, "20260903_0001")

    # 2) 用原生 sqlite 插入存量 Notebook（模拟升级前的真实数据）
    nb_id = "nb_" + "e" * 32
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO notebooks (id, title, current_revision,"
        " current_content_hash, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            nb_id,
            "Existing Notebook",
            7,
            "sha256:" + "0" * 64,
            "2026-09-03 08:00:00.000000",
            "2026-09-03 09:00:00.000000",
        ),
    )
    conn.commit()
    conn.close()

    # 3) 升级到 head（应用 0002）
    command.upgrade(config, "head")

    conn = sqlite3.connect(db_path)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(notebooks)")]
    assert "deleted_at" in columns

    # 4) 存量数据完整保留，deleted_at 为 NULL
    row = conn.execute(
        "SELECT id, title, current_revision, current_content_hash,"
        " created_at, updated_at, deleted_at FROM notebooks WHERE id = ?",
        (nb_id,),
    ).fetchone()
    assert row[:6] == (
        nb_id,
        "Existing Notebook",
        7,
        "sha256:" + "0" * 64,
        "2026-09-03 08:00:00.000000",
        "2026-09-03 09:00:00.000000",
    )
    assert row[6] is None

    # 5) 新列可写入非 NULL 值
    conn.execute(
        "UPDATE notebooks SET deleted_at = '2026-09-04 05:00:00.000000'"
        " WHERE id = ?",
        (nb_id,),
    )
    conn.commit()
    deleted = conn.execute(
        "SELECT deleted_at FROM notebooks WHERE id = ?", (nb_id,)
    ).fetchone()[0]
    assert deleted == "2026-09-04 05:00:00.000000"
    conn.close()

    # 6) 回滚到 0001：列被移除，其余列数据保留
    command.downgrade(config, "20260903_0001")
    conn = sqlite3.connect(db_path)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(notebooks)")]
    assert "deleted_at" not in columns
    row = conn.execute(
        "SELECT id, title, current_revision, current_content_hash,"
        " created_at, updated_at FROM notebooks WHERE id = ?",
        (nb_id,),
    ).fetchone()
    assert row == (
        nb_id,
        "Existing Notebook",
        7,
        "sha256:" + "0" * 64,
        "2026-09-03 08:00:00.000000",
        "2026-09-03 09:00:00.000000",
    )
    conn.close()


def test_migration_upgrade_on_empty_db(tmp_path):
    """空库直接升级到 head 可用（新列存在且为 nullable）。"""
    db_path = tmp_path / "v1" / "nb.sqlite3"
    db_path.parent.mkdir(parents=True)
    command.upgrade(_config(f"sqlite:///{db_path}"), "head")

    conn = sqlite3.connect(db_path)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(notebooks)")]
    assert "deleted_at" in columns
    # nullable：插入不带 deleted_at 的行仍然合法（走 ORM 时由应用保证）
    conn.execute(
        "INSERT INTO notebooks (id, title, current_revision,"
        " current_content_hash, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("nb_" + "a" * 32, "t", 1, "sha256:" + "0" * 64,
         "2026-09-03 08:00:00.000000", "2026-09-03 08:00:00.000000"),
    )
    conn.commit()
    conn.close()
