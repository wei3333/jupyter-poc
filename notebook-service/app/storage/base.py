"""BlobStore 协议与存储错误。

BlobStore 只负责不可变字节对象，不理解 Notebook revision。key 来自内容哈希，
同 key 必须对应完全相同的字节；后续替换为 OSS 时保持同一协议。
"""
from __future__ import annotations

from typing import Protocol


class BlobNotFoundError(Exception):
    """Blob 缺失（存储完整性故障，区别于暂时不可用的 OSError）。"""


class BlobStore(Protocol):
    def put(self, digest: str, data: bytes) -> str:
        """写入 canonical bytes，返回逻辑 key。digest 为 64 位小写十六进制。"""
        ...

    def get(self, key: str) -> bytes:
        """按 key 读取全部字节；缺失抛 BlobNotFoundError。"""
        ...

    def exists(self, key: str) -> bool:
        """key 是否已存在（用于 no-op 保存跳过重写）。"""
        ...

    @staticmethod
    def key_for(digest: str) -> str:
        """sha256/{digest 前两位}/{64 位 digest}.ipynb"""
        return f"sha256/{digest[:2]}/{digest}.ipynb"
