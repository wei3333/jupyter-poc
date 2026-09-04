"""LocalBlobStore：content-addressed 本地文件系统实现。

写入流程（不可变发布）：

1. 在目标目录创建临时文件；
2. 写入全部字节；
3. flush 并 fsync 文件；
4. 同文件系统内原子 rename/replace 发布目标文件；
5. 尽力 fsync 目标目录（macOS 对目录 fsync 返回 EINVAL，忽略）。

key 来自内容哈希，同 key 对应完全相同的字节；并发覆盖相同 key 语义安全，
读取者永远不会看到部分文件。
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from .base import BlobNotFoundError

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class LocalBlobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_for(digest: str) -> str:
        return f"sha256/{digest[:2]}/{digest}.ipynb"

    def _path(self, key: str) -> Path:
        return self.root / key

    def put(self, digest: str, data: bytes) -> str:
        if not _DIGEST_PATTERN.match(digest):
            raise ValueError(f"非法内容哈希摘要: {digest!r}")
        if hashlib.sha256(data).hexdigest() != digest:
            # 属于调用方 bug：Blob 字节必须与计算哈希的字节完全一致。
            raise ValueError("Blob 字节与内容哈希不一致")

        key = self.key_for(digest)
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=".tmp-", suffix=".ipynb"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

        try:
            dir_fd = os.open(target.parent, os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            except OSError:
                # macOS 等平台对目录 fsync 返回 EINVAL：尽力而为。
                pass
            finally:
                os.close(dir_fd)
        return key

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise BlobNotFoundError(key) from None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()
