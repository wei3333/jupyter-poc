"""LocalBlobStore 单元测试：原子写、key 布局、哈希校验。"""
from __future__ import annotations

import hashlib
import threading

import pytest

from app.storage.base import BlobNotFoundError
from app.storage.local import LocalBlobStore


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture()
def store(tmp_path):
    return LocalBlobStore(tmp_path / "blobs")


def test_put_get_roundtrip(store):
    data = '{"nbformat": 4}'.encode()
    key = store.put(_digest(data), data)
    assert store.get(key) == data


def test_key_layout(store):
    data = b"x" * 32
    digest = _digest(data)
    key = store.put(digest, data)
    assert key == f"sha256/{digest[:2]}/{digest}.ipynb"
    assert (store.root / key).is_file()


def test_put_rejects_digest_mismatch(store):
    with pytest.raises(ValueError):
        store.put("0" * 64, b"data")
    with pytest.raises(ValueError):
        store.put("not-a-digest", b"data")


def test_get_missing_raises_blob_not_found(store):
    with pytest.raises(BlobNotFoundError):
        store.get(f"sha256/ab/{'a' * 64}.ipynb")


def test_exists(store):
    data = b"hello"
    digest = _digest(data)
    key = store.put(digest, data)
    assert store.exists(key)
    assert not store.exists(f"sha256/ff/{'f' * 64}.ipynb")


def test_no_temp_files_left_behind(store):
    data = b"atomic"
    digest = _digest(data)
    store.put(digest, data)
    leftovers = [
        p.name for p in (store.root / f"sha256/{digest[:2]}").iterdir()
        if p.name.startswith(".tmp-")
    ]
    assert leftovers == []


def test_overwrite_same_key_is_safe(store):
    data = b"same bytes"
    digest = _digest(data)
    key = store.put(digest, data)
    store.put(digest, data)  # 同 key 同字节：语义安全
    assert store.get(key) == data


def test_concurrent_puts_same_key_leave_intact_file(store):
    data = b"concurrent"
    digest = _digest(data)
    errors = []

    def put():
        try:
            store.put(digest, data)
        except Exception as error:  # pragma: no cover
            errors.append(error)

    threads = [threading.Thread(target=put) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert store.get(store.key_for(digest)) == data
    leftovers = [
        p.name for p in (store.root / f"sha256/{digest[:2]}").iterdir()
        if p.name.startswith(".tmp-")
    ]
    assert leftovers == []
