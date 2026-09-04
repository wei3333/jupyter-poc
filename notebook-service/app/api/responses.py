"""OpenAPI responses 声明助手：让 FastAPI 生成的 /openapi.json 与提交契约对齐。

状态码集合、必需响应头和响应 schema 以 docs/api/notebook-service-v1.openapi.yaml
为准；描述文本不作为契约差异。
"""
from __future__ import annotations

from ..schemas import ErrorResponse

REQUEST_ID_HEADER = {
    "X-Request-ID": {
        "description": "用于日志关联和故障排查的请求 ID",
        "schema": {"type": "string"},
    }
}

ETAG_HEADER = {
    "ETag": {
        "description": "当前响应中 Notebook revision 的实体标签",
        "schema": {"type": "string"},
    }
}


def error_docs(description: str, *, retry_after: bool = False) -> dict:
    headers = dict(REQUEST_ID_HEADER)
    if retry_after:
        headers["Retry-After"] = {
            "description": "建议客户端等待的秒数",
            "schema": {"type": "integer", "minimum": 0},
        }
    return {
        "description": description,
        "headers": headers,
        "model": ErrorResponse,
    }


def success_docs(
    description: str, model, *, headers: dict | None = None
) -> dict:
    return {
        "description": description,
        "headers": headers,
        "model": model,
    }
