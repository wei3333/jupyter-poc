"""环境配置。

相对路径一律相对于 notebook-service 包根（本文件的上上级目录）解析，
不依赖调用者从仓库根目录启动。配置错误在加载时立即抛错（启动快速失败）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# notebook-service/ 目录（app/ 的上上级）。
SERVICE_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_URL = "sqlite:///./data/v1/notebooks.sqlite3"
DEFAULT_BLOB_ROOT = "./data/v1/blobs"
DEFAULT_MAX_REQUEST_BYTES = 20 * 1024 * 1024  # 20 MiB
DEFAULT_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60  # 24 小时


@dataclass(frozen=True)
class Settings:
    database_url: str
    blob_root: Path
    max_request_bytes: int
    idempotency_ttl_seconds: int


def _resolve_sqlite_url(url: str) -> str:
    """把 sqlite:/// 相对路径解析为基于 SERVICE_ROOT 的绝对路径。

    形如 sqlite:////abs/path 的绝对路径原样保留；非 sqlite URL（例如未来
    PostgreSQL）原样透传。
    """
    prefix = "sqlite:///"
    if url.startswith(prefix):
        path = url[len(prefix):]
        if path and not path.startswith("/"):
            path = str((SERVICE_ROOT / path).resolve())
        return f"{prefix}{path}"
    return url


def _resolve_blob_root(raw: str) -> Path:
    root = Path(raw)
    if not root.is_absolute():
        root = (SERVICE_ROOT / root).resolve()
    return root


def _parse_positive_int(name: str, raw: str, default: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} 必须是整数，收到 {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{name} 必须为正整数，收到 {value}")
    return value


def load_settings() -> Settings:
    database_url = _resolve_sqlite_url(
        os.environ.get("NOTEBOOK_DATABASE_URL", DEFAULT_DATABASE_URL)
    )
    blob_root = _resolve_blob_root(
        os.environ.get("NOTEBOOK_BLOB_ROOT", DEFAULT_BLOB_ROOT)
    )
    max_request_bytes = _parse_positive_int(
        "NOTEBOOK_MAX_REQUEST_BYTES",
        os.environ.get("NOTEBOOK_MAX_REQUEST_BYTES"),
        DEFAULT_MAX_REQUEST_BYTES,
    )
    idempotency_ttl = _parse_positive_int(
        "NOTEBOOK_IDEMPOTENCY_TTL_SECONDS",
        os.environ.get("NOTEBOOK_IDEMPOTENCY_TTL_SECONDS"),
        DEFAULT_IDEMPOTENCY_TTL_SECONDS,
    )
    return Settings(
        database_url=database_url,
        blob_root=blob_root,
        max_request_bytes=max_request_bytes,
        idempotency_ttl_seconds=idempotency_ttl,
    )
