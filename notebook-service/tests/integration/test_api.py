"""v1 HTTP 接口集成测试：状态码、响应头、ETag、条件读取、幂等、错误 envelope。"""
from __future__ import annotations

import re

import pytest

from tests.conftest import make_code_cell, make_notebook

ETAG_RE = re.compile(r'^"nb_[A-Za-z0-9_-]{12,64}-r[1-9][0-9]*"$')


def _post(client, body=None, key="key-1", **kwargs):
    return client.post(
        "/api/v1/notebooks",
        json=body if body is not None else {"title": "Untitled"},
        headers={"Idempotency-Key": key, **kwargs.get("headers", {})},
    )


def _put(client, notebook_id, base_revision, content, key="key-1"):
    return client.put(
        f"/api/v1/notebooks/{notebook_id}",
        json={"baseRevision": base_revision, "content": content},
        headers={"Idempotency-Key": key},
    )


def _create_notebook(client, key="create-1", body=None):
    response = _post(client, body=body, key=key)
    assert response.status_code == 201, response.text
    return response


def _error(response):
    return response.json()["error"]


def _assert_error_envelope(response, code, status=None, request_id_consistent=True):
    if status is not None:
        assert response.status_code == status
    envelope = _error(response)
    assert envelope["code"] == code
    assert envelope["message"]
    if request_id_consistent:
        assert envelope["requestId"] == response.headers["x-request-id"]
    return envelope


# -- 创建 ----------------------------------------------------------------------


def test_create_minimal_notebook(client):
    response = _create_notebook(client)
    assert response.status_code == 201
    body = response.json()
    assert re.fullmatch(r"nb_[A-Za-z0-9_-]{12,64}", body["notebookId"])
    assert body["title"] == "Untitled"
    assert body["revision"] == 1
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", body["contentHash"])
    assert body["content"] == {
        "nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": []
    }
    assert body["createdAt"].endswith("Z")
    assert body["updatedAt"].endswith("Z")
    assert response.headers["location"] == f"/api/v1/notebooks/{body['notebookId']}"
    assert ETAG_RE.match(response.headers["etag"])
    assert response.headers["x-request-id"].startswith("req_")


def test_create_with_default_title_when_omitted(client):
    response = _post(client, body={}, key="k")
    assert response.status_code == 201
    assert response.json()["title"] == "Untitled Notebook"


def test_create_normalizes_cell_ids_and_minor(client):
    content = {
        "nbformat": 4,
        "nbformat_minor": 4,
        "metadata": {},
        "cells": [
            {"cell_type": "code", "metadata": {}, "source": "x", "execution_count": None, "outputs": []},
            {"cell_type": "markdown", "metadata": {}, "source": "y"},
        ],
    }
    response = _create_notebook(client, body={"content": content})
    doc = response.json()["content"]
    assert doc["nbformat_minor"] == 5
    ids = [c["id"] for c in doc["cells"]]
    assert len(set(ids)) == 2
    for cell_id in ids:
        assert re.fullmatch(r"[a-f0-9]{32}", cell_id)


def test_create_title_is_trimmed(client):
    response = _post(client, body={"title": "  Trim me  "}, key="k")
    assert response.status_code == 201
    assert response.json()["title"] == "Trim me"


def test_create_blank_title_rejected(client):
    response = _post(client, body={"title": "   "}, key="k")
    _assert_error_envelope(response, "INVALID_REQUEST", status=422)


def test_create_unknown_envelope_field_rejected(client):
    response = _post(client, body={"title": "x", "foo": 1}, key="k")
    _assert_error_envelope(response, "INVALID_REQUEST", status=422)


def test_create_non_object_content_rejected(client):
    response = _post(client, body={"content": 42}, key="k")
    _assert_error_envelope(response, "INVALID_REQUEST", status=422)


def test_create_non_4_nbformat_rejected(client):
    response = _post(client, body={"content": make_notebook(nbformat=3)}, key="k")
    envelope = _assert_error_envelope(response, "INVALID_NOTEBOOK", status=422)
    assert envelope["details"]["path"] == "/content/nbformat"


def test_create_missing_idempotency_key_rejected(client):
    response = client.post("/api/v1/notebooks", json={"title": "x"})
    envelope = _assert_error_envelope(response, "INVALID_REQUEST", status=422)
    assert envelope["details"]["path"] == "/headers/Idempotency-Key"


def test_create_malformed_json_rejected(client):
    response = client.post(
        "/api/v1/notebooks",
        content=b'{"title": ',
        headers={"Idempotency-Key": "k", "Content-Type": "application/json"},
    )
    _assert_error_envelope(response, "MALFORMED_JSON", status=400)


def test_create_nan_rejected(client):
    response = client.post(
        "/api/v1/notebooks",
        content=b'{"title": NaN}',
        headers={"Idempotency-Key": "k", "Content-Type": "application/json"},
    )
    _assert_error_envelope(response, "MALFORMED_JSON", status=400)


# -- 读取与条件读取 ---------------------------------------------------------------


def test_get_latest(client):
    created = _create_notebook(client)
    nb_id = created.json()["notebookId"]
    response = client.get(f"/api/v1/notebooks/{nb_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["notebookId"] == nb_id
    assert body["revision"] == 1
    assert body["content"] == created.json()["content"]
    assert ETAG_RE.match(response.headers["etag"])


def test_get_with_matching_if_none_match_returns_304(client):
    created = _create_notebook(client)
    nb_id = created.json()["notebookId"]
    etag = created.headers["etag"]

    response = client.get(
        f"/api/v1/notebooks/{nb_id}", headers={"If-None-Match": etag}
    )
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == etag
    assert response.headers["x-request-id"]


def test_get_with_stale_if_none_match_returns_200(client):
    created = _create_notebook(client)
    nb_id = created.json()["notebookId"]
    response = client.get(
        f"/api/v1/notebooks/{nb_id}",
        headers={"If-None-Match": '"nb_00000000000000000000000000000000-r99"'},
    )
    assert response.status_code == 200


def test_get_not_found(client):
    response = client.get("/api/v1/notebooks/nb_" + "f" * 32)
    envelope = _assert_error_envelope(response, "NOTEBOOK_NOT_FOUND", status=404)
    assert envelope["details"]["notebookId"] == "nb_" + "f" * 32


def test_get_invalid_notebook_id_rejected(client):
    response = client.get("/api/v1/notebooks/not-an-id")
    _assert_error_envelope(response, "INVALID_REQUEST", status=422)


def test_get_revision_reads_immutable_history(client):
    created = _create_notebook(client)
    nb_id = created.json()["notebookId"]
    content1 = created.json()["content"]

    content2 = make_notebook([make_code_cell("c1", source="x=1")])
    assert _put(client, nb_id, 1, content2, key="save-1").status_code == 200

    revision1 = client.get(f"/api/v1/notebooks/{nb_id}/revisions/1")
    assert revision1.status_code == 200
    assert revision1.json()["content"] == content1
    assert revision1.json()["revision"] == 1

    latest = client.get(f"/api/v1/notebooks/{nb_id}")
    assert latest.json()["revision"] == 2


def test_get_revision_not_found_vs_notebook_not_found(client):
    nb_id = _create_notebook(client).json()["notebookId"]

    response = client.get(f"/api/v1/notebooks/{nb_id}/revisions/99")
    envelope = _assert_error_envelope(response, "REVISION_NOT_FOUND", status=404)
    assert envelope["details"]["revision"] == 99

    response = client.get("/api/v1/notebooks/nb_" + "e" * 32 + "/revisions/1")
    _assert_error_envelope(response, "NOTEBOOK_NOT_FOUND", status=404)


def test_get_revision_invalid_param_rejected(client):
    nb_id = _create_notebook(client).json()["notebookId"]
    response = client.get(f"/api/v1/notebooks/{nb_id}/revisions/abc")
    _assert_error_envelope(response, "INVALID_REQUEST", status=422)
    response = client.get(f"/api/v1/notebooks/{nb_id}/revisions/0")
    _assert_error_envelope(response, "INVALID_REQUEST", status=422)


def test_get_revision_conditional_read(client):
    nb_id = _create_notebook(client).json()["notebookId"]
    etag = client.get(f"/api/v1/notebooks/{nb_id}/revisions/1").headers["etag"]
    response = client.get(
        f"/api/v1/notebooks/{nb_id}/revisions/1",
        headers={"If-None-Match": etag},
    )
    assert response.status_code == 304
    assert response.content == b""


# -- 保存 ----------------------------------------------------------------------


def test_save_creates_new_revision_without_content(client):
    created = _create_notebook(client)
    nb_id = created.json()["notebookId"]

    content = make_notebook([make_code_cell("c1", source="x=1")])
    response = _put(client, nb_id, 1, content, key="save-1")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "notebookId": nb_id,
        "revision": 2,
        "contentHash": body["contentHash"],
        "updatedAt": body["updatedAt"],
        "unchanged": False,
    }
    assert "content" not in body
    assert ETAG_RE.match(response.headers["etag"])
    assert response.headers["etag"].endswith('-r2"')


def test_save_same_content_is_unchanged(client):
    nb_id = _create_notebook(client).json()["notebookId"]
    content = make_notebook([make_code_cell("c1")])

    first = _put(client, nb_id, 1, content, key="save-1")
    assert first.json()["unchanged"] is False

    second = _put(client, nb_id, 2, content, key="save-2")
    assert second.status_code == 200
    assert second.json()["unchanged"] is True
    assert second.json()["revision"] == 2


def test_noop_save_does_not_change_updated_at(client):
    nb_id = _create_notebook(client).json()["notebookId"]
    content = make_notebook([make_code_cell("c1")])
    _put(client, nb_id, 1, content, key="save-1")

    before = client.get(f"/api/v1/notebooks/{nb_id}").json()["updatedAt"]
    response = _put(client, nb_id, 2, content, key="save-2")
    assert response.json()["updatedAt"] == before
    after = client.get(f"/api/v1/notebooks/{nb_id}").json()["updatedAt"]
    assert after == before


def test_save_stale_base_conflict(client):
    nb_id = _create_notebook(client).json()["notebookId"]
    content = make_notebook([make_code_cell("c1")])
    _put(client, nb_id, 1, content, key="save-1")

    response = _put(client, nb_id, 1, content, key="save-2")
    envelope = _assert_error_envelope(response, "REVISION_CONFLICT", status=409)
    assert envelope["details"]["baseRevision"] == 1
    assert envelope["details"]["currentRevision"] == 2
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", envelope["details"]["currentContentHash"])


def test_save_invalid_notebook_does_not_move_head(client):
    nb_id = _create_notebook(client).json()["notebookId"]

    # 缺 Cell ID：严格校验拒绝
    invalid = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [
            {"cell_type": "code", "metadata": {}, "source": "x", "execution_count": None, "outputs": []}
        ],
    }
    response = _put(client, nb_id, 1, invalid, key="bad-1")
    envelope = _assert_error_envelope(response, "INVALID_NOTEBOOK", status=422)
    assert envelope["details"]["path"] == "/content/cells/0/id"

    # 非法 output
    invalid2 = make_notebook(
        [dict(make_code_cell("c1"), outputs=[
            {"output_type": "update_display_data", "data": {}, "metadata": {}}
        ])]
    )
    response = _put(client, nb_id, 1, invalid2, key="bad-2")
    _assert_error_envelope(response, "INVALID_NOTEBOOK", status=422)

    # head 未被移动
    head = client.get(f"/api/v1/notebooks/{nb_id}").json()
    assert head["revision"] == 1
    assert head["content"] == {
        "nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": []
    }


def test_save_rejects_minor_below_5(client):
    nb_id = _create_notebook(client).json()["notebookId"]
    response = _put(client, nb_id, 1, make_notebook([make_code_cell()], minor=4), key="k")
    _assert_error_envelope(response, "INVALID_NOTEBOOK", status=422)


def test_save_missing_base_revision_rejected(client):
    nb_id = _create_notebook(client).json()["notebookId"]
    response = client.put(
        f"/api/v1/notebooks/{nb_id}",
        json={"content": make_notebook()},
        headers={"Idempotency-Key": "k"},
    )
    envelope = _assert_error_envelope(response, "INVALID_REQUEST", status=422)
    assert envelope["details"]["path"] == "/baseRevision"


def test_save_base_revision_zero_rejected(client):
    nb_id = _create_notebook(client).json()["notebookId"]
    response = _put(client, nb_id, 0, make_notebook(), key="k")
    _assert_error_envelope(response, "INVALID_REQUEST", status=422)


def test_save_notebook_not_found(client):
    response = _put(client, "nb_" + "f" * 32, 1, make_notebook(), key="k")
    _assert_error_envelope(response, "NOTEBOOK_NOT_FOUND", status=404)


def test_save_response_has_etag_and_no_content(client):
    nb_id = _create_notebook(client).json()["notebookId"]
    response = _put(client, nb_id, 1, make_notebook([make_code_cell()]), key="k")
    assert "content" not in response.json()
    assert ETAG_RE.match(response.headers["etag"])


# -- 幂等 ----------------------------------------------------------------------


def test_post_replay_returns_same_result(client):
    first = _create_notebook(client, key="idem-1", body={"title": "Same"})
    second = _post(client, body={"title": "Same"}, key="idem-1")

    assert second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["etag"] == first.headers["etag"]
    assert second.headers["location"] == first.headers["location"]


def test_post_same_key_different_body_reused(client):
    _create_notebook(client, key="idem-1", body={"title": "A"})
    response = _post(client, body={"title": "B"}, key="idem-1")
    _assert_error_envelope(response, "IDEMPOTENCY_KEY_REUSED", status=409)


def test_put_replay_returns_original_result(client):
    nb_id = _create_notebook(client).json()["notebookId"]
    content = make_notebook([make_code_cell("c1")])
    first = _put(client, nb_id, 1, content, key="idem-save")
    second = _put(client, nb_id, 1, content, key="idem-save")
    assert second.status_code == 200
    assert second.json() == first.json()
    assert second.headers["etag"] == first.headers["etag"]


def test_put_replay_after_head_advanced_returns_original_result(client):
    nb_id = _create_notebook(client).json()["notebookId"]
    content1 = make_notebook([make_code_cell("c1", source="1")])
    content2 = make_notebook([make_code_cell("c1", source="2")])

    first = _put(client, nb_id, 1, content1, key="idem-1")
    _put(client, nb_id, 2, content2, key="idem-2")

    replay = _put(client, nb_id, 1, content1, key="idem-1")
    assert replay.status_code == 200
    assert replay.json()["revision"] == 2  # 原成功结果
    assert replay.json()["unchanged"] is False
    assert replay.headers["etag"] == first.headers["etag"]
    assert replay.headers["etag"].endswith('-r2"')


def test_put_same_key_different_body_reused(client):
    nb_id = _create_notebook(client).json()["notebookId"]
    content1 = make_notebook([make_code_cell("c1", source="1")])
    content2 = make_notebook([make_code_cell("c1", source="2")])

    _put(client, nb_id, 1, content1, key="idem-1")
    response = _put(client, nb_id, 2, content2, key="idem-1")
    _assert_error_envelope(response, "IDEMPOTENCY_KEY_REUSED", status=409)


def test_put_idempotency_check_precedes_revision_check(client):
    """已成功的 key 重放即使携带过期 base 也返回原结果，而不是 REVISION_CONFLICT。"""
    nb_id = _create_notebook(client).json()["notebookId"]
    content1 = make_notebook([make_code_cell("c1", source="1")])
    content2 = make_notebook([make_code_cell("c1", source="2")])
    _put(client, nb_id, 1, content1, key="idem-1")
    _put(client, nb_id, 2, content2, key="idem-2")

    replay = _put(client, nb_id, 1, content1, key="idem-1")
    assert replay.status_code == 200
    assert replay.json()["revision"] == 2


def test_retry_after_5xx_with_same_key_succeeds(client, monkeypatch, context):
    # 第一次因存储故障 503：不持久化幂等结果，恢复后同 key 可重试成功。
    def failing_put(digest, data):
        raise OSError("disk full")

    monkeypatch.setattr(context.blob_store, "put", failing_put)
    response = _post(client, body={"title": "Retry"}, key="retry-key")
    _assert_error_envelope(response, "STORAGE_UNAVAILABLE", status=503)

    monkeypatch.undo()
    response = _post(client, body={"title": "Retry"}, key="retry-key")
    assert response.status_code == 201


def test_replay_preserves_original_etag_after_head_advances(client):
    """幂等重放返回原操作的 ETag，即使当前 head 已前进。"""
    nb_id = _create_notebook(client).json()["notebookId"]
    content1 = make_notebook([make_code_cell("c1", source="1")])
    content2 = make_notebook([make_code_cell("c1", source="2")])

    first_etag = _put(client, nb_id, 1, content1, key="k1").headers["etag"]
    _put(client, nb_id, 2, content2, key="k2")

    replay = _put(client, nb_id, 1, content1, key="k1")
    assert replay.headers["etag"] == first_etag
    # 当前 head 的 ETag 已前进
    current_etag = client.get(f"/api/v1/notebooks/{nb_id}").headers["etag"]
    assert current_etag.endswith('-r3"')
    assert replay.headers["etag"] != current_etag


# -- 错误 envelope 一致性 ----------------------------------------------------------


@pytest.mark.parametrize(
    "make_request, code, status",
    [
        (lambda c: c.post("/api/v1/notebooks", content=b"{", headers={"Idempotency-Key": "k", "Content-Type": "application/json"}), "MALFORMED_JSON", 400),
        (lambda c: c.get("/api/v1/notebooks/nb_" + "f" * 32), "NOTEBOOK_NOT_FOUND", 404),
        (lambda c: _put(c, "nb_" + "f" * 32, 1, make_notebook(), key="k"), "NOTEBOOK_NOT_FOUND", 404),
        (lambda c: c.get("/api/v1/notebooks/not-an-id"), "INVALID_REQUEST", 422),
        (lambda c: c.post("/api/v1/notebooks", json={}, headers={}), "INVALID_REQUEST", 422),
    ],
)
def test_error_envelope_request_id_matches_header(client, make_request, code, status):
    response = make_request(client)
    _assert_error_envelope(response, code, status=status, request_id_consistent=True)


def test_error_response_does_not_leak_internals(quiet_client, monkeypatch, context):
    def boom(*args, **kwargs):
        raise RuntimeError("/secret/path SELECT * FROM users")

    monkeypatch.setattr(context.service, "get", boom)
    response = quiet_client.get("/api/v1/notebooks/nb_" + "f" * 32)
    envelope = _assert_error_envelope(response, "INTERNAL_ERROR", status=500)
    assert "/secret" not in str(response.json())
    assert "SELECT" not in str(response.json())
    assert envelope["requestId"] == response.headers["x-request-id"]
