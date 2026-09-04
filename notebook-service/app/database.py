"""数据库装配：同步 Engine/Session + SQLite PRAGMA + BEGIN IMMEDIATE 写事务。

写事务统一使用 BEGIN IMMEDIATE：配合 WAL 与 busy_timeout，在 SQLite 上可靠地
串行化写事务；最终正确性仍由数据库约束和条件 UPDATE（CAS）保证，不依赖进程内锁。

实现采用 SQLAlchemy 官方文档的 pysqlite 配方：
- connect 事件里设置 `isolation_level = None`，关闭 pysqlite 隐式 BEGIN；
- "begin" 事件里显式执行 `BEGIN IMMEDIATE`，替代默认的 deferred BEGIN。
"""
from __future__ import annotations

import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

SQLITE_PRAGMAS = (
    # busy_timeout 必须先于 journal_mode 设置：并发写入者持有写锁时，新建连接的
    # journal_mode PRAGMA 自身也可能遇到锁，需要 busy 等待而不是立即失败。
    "PRAGMA busy_timeout = 5000",
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = FULL",
    "PRAGMA foreign_keys = ON",
)


def _configure_sqlite_connection(dbapi_connection, connection_record) -> None:
    """每个新 DBAPI 连接执行一次：PRAGMA + 关闭 pysqlite 隐式事务管理。"""
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        for pragma in SQLITE_PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


def _begin_immediate(conn) -> None:
    """以 BEGIN IMMEDIATE 开始每个事务，替代 SQLAlchemy 的默认 BEGIN。"""
    dbapi_connection = conn.connection.dbapi_connection
    if isinstance(dbapi_connection, sqlite3.Connection):
        conn.exec_driver_sql("BEGIN IMMEDIATE")


def build_engine(database_url: str) -> Engine:
    engine = create_engine(
        database_url,
        # 多线程（多个 Uvicorn worker 或测试线程）下由连接池分发独立连接。
        pool_size=10,
        max_overflow=0,
        connect_args={
            "check_same_thread": False,
            # 连接建立瞬间即生效的 busy 等待（早于任何 PRAGMA）。
            "timeout": 5.0,
        },
    )
    event.listen(engine, "connect", _configure_sqlite_connection)
    event.listen(engine, "begin", _begin_immediate)
    return engine


def build_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
