"""cursor 编解码单元测试（NS-D1-LIST）。"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.cursors import CursorValue, decode_cursor, encode_cursor
from app.errors import InvalidCursor

UTC = timezone.utc


def _b64url(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_encode_decode_round_trip():
    updated_at = datetime(2026, 9, 4, 5, 8, 0, 123456, tzinfo=UTC)
    cursor = encode_cursor(updated_at, "nb_" + "a" * 32)
    value = decode_cursor(cursor)
    assert value == CursorValue(updated_at=updated_at, notebook_id="nb_" + "a" * 32)
    assert value.updated_at.tzinfo is not None


def test_encode_is_base64url_without_padding():
    updated_at = datetime(2026, 9, 4, 5, 8, tzinfo=UTC)
    cursor = encode_cursor(updated_at, "nb_" + "a" * 32)
    assert "=" not in cursor
    assert all(ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for ch in cursor)
    payload = json.loads(
        base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    )
    assert payload["v"] == 1
    assert payload["updatedAt"].endswith("Z")
    assert payload["notebookId"] == "nb_" + "a" * 32


def test_decode_accepts_padded_cursor():
    updated_at = datetime(2026, 9, 4, 5, 8, tzinfo=UTC)
    cursor = encode_cursor(updated_at, "nb_" + "a" * 32)
    padded = cursor + "=" * (-len(cursor) % 4)
    assert decode_cursor(padded).notebook_id == "nb_" + "a" * 32


def test_decode_accepts_offset_timestamp():
    cursor = _b64url({
        "v": 1,
        "updatedAt": "2026-09-04T05:08:00.000000+08:00",
        "notebookId": "nb_" + "a" * 32,
    })
    value = decode_cursor(cursor)
    # 归一到 UTC
    assert value.updated_at.utcoffset() == timedelta(0)
    assert value.updated_at == datetime(2026, 9, 3, 21, 8, tzinfo=UTC)


@pytest.mark.parametrize("cursor", [
    "!!!",                       # 非 Base64URL
    "abc def",                   # 含非法字符
    "§§§",                       # 非 ASCII
    _b64url("not json"),         # Base64URL 合法但内容不是 JSON
    _b64url([1, 2]),             # JSON 数组
    _b64url({"v": 2, "updatedAt": "2026-09-04T05:08:00Z", "notebookId": "nb_" + "a" * 32}),   # 未知版本
    _b64url({"v": "1", "updatedAt": "2026-09-04T05:08:00Z", "notebookId": "nb_" + "a" * 32}),  # 版本类型错误
    _b64url({"v": True, "updatedAt": "2026-09-04T05:08:00Z", "notebookId": "nb_" + "a" * 32}),
    _b64url({"updatedAt": "2026-09-04T05:08:00Z", "notebookId": "nb_" + "a" * 32}),            # 缺 v
    _b64url({"v": 1, "notebookId": "nb_" + "a" * 32}),                                          # 缺 updatedAt
    _b64url({"v": 1, "updatedAt": "2026-09-04T05:08:00Z"}),                                     # 缺 notebookId
    _b64url({"v": 1, "updatedAt": "not-a-time", "notebookId": "nb_" + "a" * 32}),               # 非法时间
    _b64url({"v": 1, "updatedAt": "2026-09-04T05:08:00", "notebookId": "nb_" + "a" * 32}),      # 无时区
    _b64url({"v": 1, "updatedAt": "2026-09-04T05:08:00Z", "notebookId": "not-an-id"}),          # 非法 Notebook ID
    _b64url({"v": 1, "updatedAt": 123, "notebookId": "nb_" + "a" * 32}),                        # 时间类型错误
])
def test_decode_rejects_invalid_cursors(cursor):
    with pytest.raises(InvalidCursor):
        decode_cursor(cursor)


def test_decode_allows_extra_fields():
    cursor = _b64url({
        "v": 1,
        "updatedAt": "2026-09-04T05:08:00.000000Z",
        "notebookId": "nb_" + "a" * 32,
        "junk": "extra",
    })
    assert decode_cursor(cursor).notebook_id == "nb_" + "a" * 32
