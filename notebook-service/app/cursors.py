"""列表分页 cursor 编解码（NS-D1-LIST）。

cursor 为带版本号的 Base64URL JSON，客户端只能透传、不得解析或自行构造：

    {"v": 1, "updatedAt": "2026-09-04T05:08:00.000000Z", "notebookId": "nb_..."}

排序字段与 repository 查询条件严格一致：updated_at DESC, id DESC。不引入签名、
缓存或额外依赖；Base64URL、JSON、版本、时间或 Notebook ID 任一无效都抛
`InvalidCursor`（HTTP 400 INVALID_CURSOR）。
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .errors import InvalidCursor
from .schemas import NOTEBOOK_ID_PATTERN

CURSOR_VERSION = 1

# 仅允许 Base64URL 字母表（可带 0～2 个尾部填充符）。
_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")


@dataclass(frozen=True)
class CursorValue:
    """解码后的 cursor：keyset 分页的排序列值。"""

    updated_at: datetime  # timezone-aware UTC
    notebook_id: str


def encode_cursor(updated_at: datetime, notebook_id: str) -> str:
    payload = json.dumps(
        {
            "v": CURSOR_VERSION,
            "updatedAt": updated_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "notebookId": notebook_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> CursorValue:
    if not isinstance(cursor, str) or not _BASE64URL_PATTERN.match(cursor):
        raise InvalidCursor()
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError):
        raise InvalidCursor() from None

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InvalidCursor() from None
    if not isinstance(payload, dict):
        raise InvalidCursor()

    version = payload.get("v")
    if not isinstance(version, int) or isinstance(version, bool):
        raise InvalidCursor()
    if version != CURSOR_VERSION:
        raise InvalidCursor()

    updated_at_raw = payload.get("updatedAt")
    notebook_id = payload.get("notebookId")
    if not isinstance(updated_at_raw, str) or not isinstance(notebook_id, str):
        raise InvalidCursor()
    try:
        updated_at = datetime.fromisoformat(updated_at_raw)
    except ValueError:
        raise InvalidCursor() from None
    if updated_at.tzinfo is None:
        raise InvalidCursor()
    if not re.fullmatch(NOTEBOOK_ID_PATTERN, notebook_id):
        raise InvalidCursor()

    return CursorValue(
        updated_at=updated_at.astimezone(timezone.utc),
        notebook_id=notebook_id,
    )
