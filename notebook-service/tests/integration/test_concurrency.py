"""真实 SQLite 并发测试（指引 §15.5）。

使用独立 session/connection 的线程 + barrier 同步，带超时防止死锁掩盖问题；
不通过串行调用来冒充并发。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.database import build_engine, build_session_factory
from app.repositories.sqlalchemy import SqlAlchemyNotebookRepository

CREATE_SCOPE = "anonymous:POST:/api/v1/notebooks"
SAVE_SCOPE = "anonymous:PUT:/api/v1/notebooks/{notebookId}"

ITERATIONS = 3  # 每类竞态重复多次，防时序抖动掩盖问题


@pytest.fixture()
def engine(migrated_env):
    return build_engine(migrated_env["db_url"])


@pytest.fixture()
def repo(engine):
    return SqlAlchemyNotebookRepository(build_session_factory(engine), 86400)


def _count(engine, table):
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()


def _create(repo, notebook_id, content_hash="sha256:" + "1" * 64):
    return repo.create_notebook(
        scope=CREATE_SCOPE,
        key=f"create-{uuid.uuid4().hex}",
        request_hash="create-hash",
        notebook_id=notebook_id,
        title="t",
        content_hash=content_hash,
        blob_key=f"sha256/{content_hash[7:9]}/{content_hash[7:]}.ipynb",
        size_bytes=10,
        now=datetime.now(timezone.utc),
    )


def _save(repo, key, request_hash, notebook_id, base_revision, content_hash):
    return repo.save_notebook(
        scope=SAVE_SCOPE,
        key=key,
        request_hash=request_hash,
        notebook_id=notebook_id,
        base_revision=base_revision,
        content_hash=content_hash,
        blob_key=f"sha256/{content_hash[7:9]}/{content_hash[7:]}.ipynb",
        size_bytes=10,
        now=datetime.now(timezone.utc),
    )


def _run_race(targets):
    """并发执行 targets，带超时；返回每个线程的结果或异常。"""
    results: list = []
    barrier = threading.Barrier(len(targets) + 1)

    def runner(func):
        barrier.wait()
        try:
            results.append(("ok", func()))
        except Exception as error:  # pragma: no cover - 超时/锁异常时报告
            results.append(("error", type(error).__name__, str(error)[:80]))

    threads = [threading.Thread(target=runner, args=(f,)) for f in targets]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=15)
    assert all(not t.is_alive() for t in threads), "并发测试死锁"
    return results


def test_concurrent_saves_different_keys_exactly_one_winner(engine, repo):
    """两个不同 key、相同 baseRevision、不同 content：恰好一个成功一个冲突。

    每轮使用全新 Notebook，避免上一轮的 head 前进影响下一轮 base 判断。
    """
    for _ in range(ITERATIONS):
        notebook_id = _create(repo, f"nb_{uuid.uuid4().hex}").notebook.id
        results = _run_race([
            lambda: _save(repo, uuid.uuid4().hex, "rh-a", notebook_id, 1, "sha256:" + "a" * 64),
            lambda: _save(repo, uuid.uuid4().hex, "rh-b", notebook_id, 1, "sha256:" + "b" * 64),
        ])
        kinds = sorted(r[1].kind for r in results if r[0] == "ok")
        assert kinds == ["revision_conflict", "saved"], results
        assert repo.get_notebook(notebook_id).current_revision == 2
        # 该 Notebook 恰好有两个 revision（1 个初始 + 1 个获胜写入）
        with engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM notebook_revisions"
                    " WHERE notebook_id = :id"
                ),
                {"id": notebook_id},
            ).scalar()
        assert count == 2


def test_concurrent_saves_same_key_same_request_both_succeed(engine, repo):
    """两个相同 key、相同请求：都成功且结果一致，只有一个新 revision。"""
    notebook_id = _create(repo, f"nb_{uuid.uuid4().hex}").notebook.id
    key = "same-key"
    request_hash = "same-request-hash"

    for _ in range(ITERATIONS):
        results = _run_race([
            lambda: _save(repo, key, request_hash, notebook_id, 1, "sha256:" + "c" * 64),
            lambda: _save(repo, key, request_hash, notebook_id, 1, "sha256:" + "c" * 64),
        ])
        assert all(r[0] == "ok" for r in results), results
        outcomes = [r[1] for r in results]
        assert {o.kind for o in outcomes} <= {"saved", "replayed"}, outcomes

        # 双方结果一致（replay 返回原 ETag/原 revision）
        revisions = {
            o.revision.revision if o.revision else o.replay.revision
            for o in outcomes
        }
        assert revisions == {2}
        assert _count(engine, "notebook_revisions") == 2


def test_concurrent_creates_same_key_same_request_single_notebook(engine, repo):
    """两个相同 key、相同创建请求：得到相同 notebookId，只有一个 Notebook。"""
    key = "same-create-key"
    request_hash = "same-create-request-hash"

    for _ in range(ITERATIONS):
        # 各线程生成各自的候选 ID；只有获胜事务的 ID 被持久化。
        results = _run_race([
            lambda: repo.create_notebook(
                scope=CREATE_SCOPE, key=key, request_hash=request_hash,
                notebook_id=f"nb_{uuid.uuid4().hex}", title="t",
                content_hash="sha256:" + "d" * 64,
                blob_key=f"sha256/dd/{'d' * 64}.ipynb", size_bytes=10,
                now=datetime.now(timezone.utc),
            ),
            lambda: repo.create_notebook(
                scope=CREATE_SCOPE, key=key, request_hash=request_hash,
                notebook_id=f"nb_{uuid.uuid4().hex}", title="t",
                content_hash="sha256:" + "d" * 64,
                blob_key=f"sha256/dd/{'d' * 64}.ipynb", size_bytes=10,
                now=datetime.now(timezone.utc),
            ),
        ])
        assert all(r[0] == "ok" for r in results), results
        outcomes = [r[1] for r in results]
        assert {o.kind for o in outcomes} <= {"created", "replayed"}
        ids = {
            o.notebook.id if o.notebook else o.replay.notebook_id
            for o in outcomes
        }
        assert len(ids) == 1, results
