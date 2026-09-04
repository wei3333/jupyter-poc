"""Alembic 迁移环境。

- sqlalchemy.url 来自 alembic.ini（或调用方覆盖）；相对 sqlite 路径相对于
  alembic.ini 所在目录解析，不依赖启动目录。
- 迁移连接复用应用的 SQLite PRAGMA（WAL / foreign_keys / synchronous）。
"""
from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, event, pool

from app.database import _configure_sqlite_connection

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _resolve_url(url: str) -> str:
    prefix = "sqlite:///"
    if url.startswith(prefix):
        path = url[len(prefix):]
        if path and not path.startswith("/"):
            ini_dir = Path(config.config_file_name).resolve().parent
            path = str((ini_dir / path).resolve())
        return f"{prefix}{path}"
    return url


def _get_url() -> str:
    return _resolve_url(config.get_main_option("sqlalchemy.url"))


def run_migrations_offline() -> None:
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_get_url(), poolclass=pool.NullPool)
    event.listen(engine, "connect", _configure_sqlite_connection)

    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )
        with context.begin_transaction():
            context.run_migrations()

    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
