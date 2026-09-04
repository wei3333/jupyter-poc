"""Request ID 与请求体体积限制中间件的单元测试（直接 ASGI 调用）。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.middleware import RequestIdMiddleware, SizeLimitMiddleware


def make_receive(messages):
    def receive():
        async def _receive():
            if not messages:
                return {"type": "http.disconnect"}
            return messages.pop(0)
        return _receive()
    return receive


def make_app():
    async def endpoint(scope, receive, send):
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body += message.get("body", b"")
                if not message.get("more_body", False):
                    break
            else:
                break
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({
            "type": "http.response.body",
            "body": json.dumps({"got": len(body)}).encode(),
        })
    return endpoint


def build_stack(limit_bytes):
    app = make_app()
    app = SizeLimitMiddleware(app, limit_bytes=limit_bytes)
    app = RequestIdMiddleware(app)
    return app


def call(app, path="/api/v1/notebooks", headers=None, chunks=None):
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "headers": [
            (name.lower().encode(), value.encode())
            for name, value in (headers or {}).items()
        ],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 1),
        "root_path": "",
    }
    chunk_list = chunks or [b""]
    messages = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunk_list) - 1}
        for i, chunk in enumerate(chunk_list)
    ]
    events = []

    async def send(message):
        events.append(message)

    asyncio.run(app(scope, make_receive(messages), send))
    start = next(e for e in events if e["type"] == "http.response.start")
    body = b"".join(
        e.get("body", b"") for e in events if e["type"] == "http.response.body"
    )
    return start["status"], start["headers"], body


def _header(headers, name):
    for key, value in headers:
        if key == name.encode():
            return value.decode()
    return None


# -- Request ID --------------------------------------------------------------


def test_request_id_generated_with_req_prefix():
    status, headers, _ = call(build_stack(1000))
    assert status == 200
    assert _header(headers, "x-request-id").startswith("req_")


def test_request_id_honors_safe_client_value():
    _, headers, _ = call(
        build_stack(1000), headers={"X-Request-ID": "client-abc_123.xyz"}
    )
    assert _header(headers, "x-request-id") == "client-abc_123.xyz"


@pytest.mark.parametrize(
    "unsafe", ["bad\nid", "bad id", "x" * 200, "résumé", "a" * 128 + "b"]
)
def test_request_id_rejects_unsafe_or_oversized_client_value(unsafe):
    _, headers, _ = call(build_stack(1000), headers={"X-Request-ID": unsafe})
    assert _header(headers, "x-request-id").startswith("req_")


def test_request_id_present_on_error_response():
    status, headers, body = call(
        build_stack(10), headers={"Content-Length": "200"}
    )
    assert status == 413
    envelope = json.loads(body)
    assert envelope["error"]["requestId"] == _header(headers, "x-request-id")
    assert _header(headers, "x-request-id").startswith("req_")


# -- 体积限制 ------------------------------------------------------------------


def test_content_length_over_limit_rejected_with_413():
    status, _, body = call(
        build_stack(100), headers={"Content-Length": "200"}
    )
    assert status == 413
    envelope = json.loads(body)
    assert envelope["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert envelope["error"]["details"] == {"limitBytes": 100}


def test_chunked_body_over_limit_rejected_with_413():
    # 无 Content-Length：在 receive 层累计字节并在超限时停止。
    status, _, body = call(build_stack(10), chunks=[b"x" * 6, b"y" * 6])
    assert status == 413
    envelope = json.loads(body)
    assert envelope["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert envelope["error"]["details"] == {"limitBytes": 10}


def test_body_within_limit_passes_through():
    status, _, body = call(build_stack(100), chunks=[b"x" * 50])
    assert status == 200
    assert json.loads(body)["got"] == 50


def test_chunked_body_at_limit_boundary_passes():
    status, _, body = call(build_stack(10), chunks=[b"x" * 10])
    assert status == 200
    assert json.loads(body)["got"] == 10


def test_non_v1_paths_are_not_limited():
    # 旧 POC route 路径不受 v1 体积限制影响。
    status, _, body = call(build_stack(10), path="/api/notebooks", chunks=[b"x" * 50])
    assert status == 200
    assert json.loads(body)["got"] == 50
