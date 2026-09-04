"""ASGI middleware：Request ID 与请求体体积限制。

安装顺序决定执行顺序：先 add_middleware(SizeLimit)，后 add_middleware(RequestId)，
RequestId 位于最外层，能保证所有响应（包括 413 提前拒绝）都带 X-Request-ID，
且 413 错误体的 requestId 与外层分配的一致。
"""
from __future__ import annotations

import contextvars
import json
import re
import uuid

from .errors import PayloadTooLarge

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)

# 格式安全：仅可见 ASCII 字母/数字/._-，长度 1..128。禁止把换行、控制字符或
# 超长客户端 Request ID 原样写入日志/响应头。
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_LIMIT_SCOPED_PREFIXES = ("/api/v1", "/health")


def _scope_state(scope: dict) -> dict:
    state = scope.get("state")
    if state is None:
        state = {}
        scope["state"] = state
    return state


def error_body(code: str, message: str, request_id: str, details=None) -> dict:
    """统一错误 envelope 的 JSON 结构。"""
    body: dict = {
        "error": {
            "code": code,
            "message": message,
            "requestId": request_id,
        }
    }
    if details:
        body["error"]["details"] = details
    return body


class RequestIdMiddleware:
    """为每个请求分配/沿用 Request ID，并写入响应头与 request context。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client_id = _header_value(scope, "x-request-id")
        request_id = (
            client_id if client_id and _SAFE_REQUEST_ID.match(client_id)
            else f"req_{uuid.uuid4().hex}"
        )
        _scope_state(scope)["request_id"] = request_id
        token = request_id_var.set(request_id)

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                if not _has_header(headers, "x-request-id"):
                    headers.append(
                        (b"x-request-id", request_id.encode("ascii"))
                    )
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            request_id_var.reset(token)


class SizeLimitMiddleware:
    """在 ASGI receive 层限制 /api/v1 与 /health 请求体大小。

    - Content-Length 已知且超限：立即 413，不读取 body；
    - 无 Content-Length（分块）请求：累计字节，超限即停止读取并返回 413。

    超限请求不会进入 JSON 解析、nbformat 验证、Blob 写入或数据库事务。
    """

    def __init__(self, app, limit_bytes: int) -> None:
        self.app = app
        self.limit_bytes = limit_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(
            _LIMIT_SCOPED_PREFIXES
        ):
            await self.app(scope, receive, send)
            return

        request_id = _scope_state(scope).get(
            "request_id", f"req_{uuid.uuid4().hex}"
        )

        content_length = _content_length(scope)
        if content_length is not None and content_length > self.limit_bytes:
            await _send_error(send, PayloadTooLarge(self.limit_bytes), request_id)
            return

        buffered: list[dict] = []
        total = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.limit_bytes:
                    await _send_error(
                        send, PayloadTooLarge(self.limit_bytes), request_id
                    )
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break

        replay_index = 0

        async def replaying_receive():
            # ASGI receive 必须是可调用对象：先重放已缓冲的消息，
            # 之后透传底层 receive。
            nonlocal replay_index
            if replay_index < len(buffered):
                message = buffered[replay_index]
                replay_index += 1
                return message
            return await receive()

        await self.app(scope, replaying_receive, send)


async def _send_error(send, error: PayloadTooLarge, request_id: str):
    body = json.dumps(
        error_body(error.code, error.message, request_id, error.details),
        ensure_ascii=False,
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": error.http_status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _header_value(scope: dict, name: str) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name.encode("ascii"):
            return value.decode("latin-1")
    return None


def _content_length(scope: dict) -> int | None:
    values = [
        value
        for key, value in scope.get("headers") or []
        if key.lower() == b"content-length"
    ]
    if len(values) != 1:
        return None
    try:
        return int(values[0])
    except (ValueError, TypeError):
        return None


def _has_header(headers: list[tuple[bytes, bytes]], name: str) -> bool:
    lowered = name.encode("ascii").lower()
    return any(key.lower() == lowered for key, _ in headers)
