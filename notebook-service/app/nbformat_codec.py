"""nbformat 文档处理：规范化、验证、canonical JSON 与 SHA-256 内容哈希。

只负责文档格式，不访问数据库、HTTP 或 BlobStore。两个入口：

- `normalize_for_create`：创建/导入阶段规范化（补 Cell ID、提升 nbformat_minor）。
- `validate_for_save`：PUT 严格校验，不做任何"帮助性修复"。

canonical bytes 规则（等价于）：

    json.dumps(content, ensure_ascii=False, sort_keys=True,
               separators=(",", ":"), allow_nan=False).encode("utf-8")

- 无 UTF-8 BOM、无尾部换行；数组顺序保持不变；
- 不把 source: string 与 source: string[] 互相转换；
- 不删除未知标准 metadata、MIME bundle 或 metadata.lumen；
- 拒绝 NaN/Infinity/-Infinity。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from typing import Any

import nbformat
from nbformat.validator import NotebookValidationError

from .errors import InvalidNotebook

# v1 写入 Cell ID，因此保存后的文档至少声明 nbformat 4.5。
MIN_NBFORMAT_MINOR = 5

# 契约 CellId：1..64 位，仅 [A-Za-z0-9_-]。
CELL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_DEFAULT_NOTEBOOK = {
    "nbformat": 4,
    "nbformat_minor": 5,
    "metadata": {},
    "cells": [],
}


def default_notebook() -> dict:
    """POST 未提供 content 时的服务端最小合法 Notebook。"""
    return copy.deepcopy(_DEFAULT_NOTEBOOK)


def canonical_json_bytes(obj: Any) -> bytes:
    """稳定 JSON 序列化（canonical bytes）。NaN/Infinity 触发 ValueError。"""
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash_id(hex_digest: str) -> str:
    """`sha256:` 前缀内容哈希。"""
    return f"sha256:{hex_digest}"


def compute_content_hash(doc: Any) -> str:
    """canonical bytes 的 SHA-256，`sha256:<64 位小写十六进制>` 格式。"""
    return content_hash_id(sha256_hex(canonical_json_bytes(doc)))


def _to_plain(obj: Any) -> Any:
    """把 NotebookNode 等 dict 子类递归转换为普通 JSON 可序列化对象。"""
    if isinstance(obj, dict):
        return {key: _to_plain(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(value) for value in obj]
    return obj


def _check_toplevel(doc: dict) -> None:
    if not isinstance(doc, dict):
        raise InvalidNotebook.at(
            "/content", "Notebook content must be a JSON object"
        )
    if doc.get("nbformat") != 4:
        raise InvalidNotebook.at(
            "/content/nbformat", "Notebook must be nbformat 4"
        )


def _check_cells_list(doc: dict) -> None:
    cells = doc.get("cells")
    if not isinstance(cells, list):
        raise InvalidNotebook.at(
            "/content/cells", "cells must be an array"
        )
    for index, cell in enumerate(cells):
        if not isinstance(cell, dict):
            raise InvalidNotebook.at(
                f"/content/cells/{index}", "Cell must be a JSON object"
            )


def _validate_nbformat(doc: dict) -> None:
    """from_dict + 显式 validate；其他结构问题交给官方 nbformat schema。"""
    try:
        nb = nbformat.from_dict(doc)
    except Exception as error:  # from_dict 对异常结构的防御
        raise InvalidNotebook.at("/content", str(error)) from error
    try:
        nbformat.validate(nb)
    except NotebookValidationError as error:
        raise InvalidNotebook.at("/content", error.message) from error


def normalize_for_create(content: dict) -> dict:
    """创建/导入规范化。深拷贝请求 content，不原地修改。

    只规范化 Cell ID 与 nbformat_minor；其他缺失字段、非法 output、非法 Cell
    类型不猜测修复，交由 nbformat.validate 拒绝。
    """
    doc = copy.deepcopy(content)
    _check_toplevel(doc)

    minor = doc.get("nbformat_minor")
    if not isinstance(minor, int) or isinstance(minor, bool):
        raise InvalidNotebook.at(
            "/content/nbformat_minor", "nbformat_minor must be an integer"
        )
    doc["nbformat_minor"] = max(minor, MIN_NBFORMAT_MINOR)

    _check_cells_list(doc)

    seen: set[str] = set()
    for index, cell in enumerate(doc["cells"]):
        cell_id = cell.get("id")
        # ID 缺失、格式非法或重复时替换为 uuid4 hex（不把重复 ID 交给
        # nbformat.validate，其 DuplicateCellId 会自动改写文档）。
        if (
            not isinstance(cell_id, str)
            or not CELL_ID_PATTERN.match(cell_id)
            or cell_id in seen
        ):
            cell["id"] = uuid.uuid4().hex
        seen.add(cell["id"])

    # 先规范化再校验：nbformat 4.0 schema 不允许 Cell 携带 id，提升 minor 后
    # 才能用 4.5 schema 校验。
    _validate_nbformat(doc)
    return _to_plain(doc)


def validate_for_save(content: dict) -> dict:
    """PUT 严格校验。不规范化、不补 ID、不改内容，非法文档返回 INVALID_NOTEBOOK。

    若服务端在 PUT 中补 ID 或改变内容，而响应又不返回完整 content，浏览器状态
    会立即与服务端分叉。
    """
    doc = copy.deepcopy(content)
    _check_toplevel(doc)

    minor = doc.get("nbformat_minor")
    if not isinstance(minor, int) or isinstance(minor, bool):
        raise InvalidNotebook.at(
            "/content/nbformat_minor", "nbformat_minor must be an integer"
        )
    if minor < MIN_NBFORMAT_MINOR:
        raise InvalidNotebook.at(
            "/content/nbformat_minor",
            f"nbformat_minor must be at least {MIN_NBFORMAT_MINOR}",
        )

    _check_cells_list(doc)

    seen: set[str] = set()
    for index, cell in enumerate(doc["cells"]):
        cell_id = cell.get("id")
        if not isinstance(cell_id, str):
            raise InvalidNotebook.at(
                f"/content/cells/{index}/id", "Cell id is required"
            )
        if not CELL_ID_PATTERN.match(cell_id):
            raise InvalidNotebook.at(
                f"/content/cells/{index}/id",
                "Cell id must match ^[A-Za-z0-9_-]{1,64}$",
            )
        if cell_id in seen:
            raise InvalidNotebook.at(
                f"/content/cells/{index}/id", "Cell id must be unique"
            )
        seen.add(cell_id)

    _validate_nbformat(doc)

    try:
        canonical_json_bytes(doc)
    except (ValueError, TypeError) as error:
        raise InvalidNotebook.at(
            "/content", f"Content is not JSON serializable: {error}"
        ) from error

    return _to_plain(doc)
