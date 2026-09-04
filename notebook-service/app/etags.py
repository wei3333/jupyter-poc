"""ETag 统一格式：`"{notebookId}-r{revision}"`（含 HTTP 双引号）。"""
from __future__ import annotations


def format_etag(notebook_id: str, revision: int) -> str:
    return f'"{notebook_id}-r{revision}"'
