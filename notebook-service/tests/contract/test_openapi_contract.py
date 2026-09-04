"""OpenAPI contract tests（指引 §15.1）。

- 契约文件可被 OpenAPI 3.1 validator 解析；
- FastAPI 生成的 /openapi.json 与提交契约做语义比较：path、method、operationId、
  状态码、必需响应头、信封级字段与 required 必须一致；描述文本、key 顺序、format
  提示不作为差异；
- 旧 POC route 不出现在 v1 schema；
- 每个声明错误至少有一个可产出的运行时响应测试。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.conftest import make_code_cell, make_notebook

SERVICE_DIR = Path(__file__).resolve().parent.parent.parent
CONTRACT_PATH = (
    SERVICE_DIR.parent / "docs" / "api" / "notebook-service-v1.openapi.yaml"
)

SCALAR_TYPES = ("string", "integer", "number", "boolean")


# -- 加载与解析 -------------------------------------------------------------------


def _load_contract():
    return yaml.safe_load(CONTRACT_PATH.read_text())


def test_contract_file_parses_as_openapi_31():
    from openapi_spec_validator import validate

    validate(_load_contract())


@pytest.fixture()
def contract():
    return _load_contract()


@pytest.fixture()
def generated(client):
    return client.get("/openapi.json").json()


# -- 语义比较辅助 -------------------------------------------------------------------


def _resolve(spec, node):
    """沿 $ref 链解析（两个 spec 内部的相对引用）。"""
    seen = set()
    while isinstance(node, dict) and "$ref" in node and len(node) == 1:
        ref = node["$ref"]
        if ref in seen:  # pragma: no cover - 防御循环引用
            break
        seen.add(ref)
        parts = ref.lstrip("#/").split("/")
        target = spec
        for part in parts:
            target = target[part]
        node = target
    return node


def _schema(spec, node):
    node = _resolve(spec, node)
    if isinstance(node, dict) and "schema" in node:
        node = _resolve(spec, node["schema"])
    return node


def _type_kinds(prop):
    """返回属性允许的类型集合（anyOf/oneOf 展开；契约与生成的 type 表达方式可能不同）。"""
    kinds = set()
    if prop.get("type"):
        kinds.add(prop["type"])
    for variant in list(prop.get("anyOf", [])) + list(prop.get("oneOf", [])):
        kinds.add(variant.get("type"))
    return kinds - {None}


def _compare_envelope(gen_spec, con_spec, gen_schema, con_schema, label):
    """信封级字段比较：顶层 properties 集合、required 集合、标量类型一致。"""
    gen = _schema(gen_spec, gen_schema)
    con = _schema(con_spec, con_schema)
    assert gen.get("type") == "object", f"{label}: 应为 object"
    assert con.get("type") == "object", f"{label}: 契约应为 object"

    gen_props = set(gen.get("properties", {}))
    con_props = set(con.get("properties", {}))
    assert gen_props == con_props, f"{label}: 字段不一致 {gen_props ^ con_props}"

    gen_required = set(gen.get("required", []))
    con_required = set(con.get("required", []))
    assert gen_required == con_required, f"{label}: required 不一致"

    for name in con_props:
        gen_prop = _resolve(gen_spec, gen["properties"][name])
        con_prop = _resolve(con_spec, con["properties"][name])
        con_type = con_prop.get("type")
        if con_type in SCALAR_TYPES + ("object", "array"):
            assert con_type in _type_kinds(gen_prop), (
                f"{label}.{name}: 类型 {_type_kinds(gen_prop)} 不含 {con_type}"
            )

    if con.get("additionalProperties") is False:
        assert gen.get("additionalProperties") is False, (
            f"{label}: additionalProperties 应为 false"
        )


def _response_object(spec, responses, status):
    entry = responses.get(str(status))
    assert entry is not None, f"缺少 {status} 响应声明"
    return _resolve(spec, entry)


def _content_schema(spec, response_object):
    content = response_object.get("content") or {}
    media = content.get("application/json") or {}
    return media.get("schema")


# -- 语义比较 -----------------------------------------------------------------------


def _ops(spec):
    for path, path_item in spec["paths"].items():
        for method, op in path_item.items():
            if method in ("parameters",):
                continue
            yield path, method, op


def test_legacy_routes_not_in_generated_schema(generated):
    assert "/api/notebooks" not in generated["paths"]
    assert "/api/notebooks/{notebook_id}" not in generated["paths"]


def test_paths_methods_and_operation_ids_match(contract, generated):
    gen_paths = set(generated["paths"])
    con_paths = set(contract["paths"])
    assert gen_paths == con_paths, f"path 集合不一致: {gen_paths ^ con_paths}"

    for path in con_paths:
        gen_methods = {
            m for m in generated["paths"][path] if m != "parameters"
        }
        con_methods = {
            m for m in contract["paths"][path] if m != "parameters"
        }
        assert gen_methods == con_methods, f"{path}: method 不一致"
        for method in con_methods:
            assert (
                generated["paths"][path][method]["operationId"]
                == contract["paths"][path][method]["operationId"]
            ), f"{method} {path}: operationId 不一致"


def test_status_codes_match_per_operation(contract, generated):
    for path, method, con_op in _ops(contract):
        gen_op = generated["paths"][path][method]
        gen_statuses = set(gen_op["responses"])
        con_statuses = {str(s) for s in con_op["responses"]}
        assert gen_statuses == con_statuses, (
            f"{method} {path}: 状态码不一致 {gen_statuses ^ con_statuses}"
        )


def test_response_headers_match_per_operation(contract, generated):
    for path, method, con_op in _ops(contract):
        gen_op = generated["paths"][path][method]
        for status, con_resp in con_op["responses"].items():
            con_headers = {
                name.lower()
                for name in (_resolve(contract, con_resp).get("headers") or {})
            }
            gen_resp = _response_object(generated, gen_op["responses"], status)
            gen_headers = {
                name.lower() for name in (gen_resp.get("headers") or {})
            }
            missing = con_headers - gen_headers
            assert not missing, (
                f"{method} {path} {status}: 缺少契约响应头 {missing}"
            )


def test_request_body_envelopes_match(contract, generated):
    for path, method, con_op in _ops(contract):
        con_body = con_op.get("requestBody")
        gen_body = generated["paths"][path][method].get("requestBody")
        assert (con_body is None) == (gen_body is None), (
            f"{method} {path}: requestBody 存在性不一致"
        )
        if con_body is None:
            continue
        assert gen_body.get("required") == con_body.get("required", False), (
            f"{method} {path}: requestBody required 不一致"
        )
        _compare_envelope(
            generated,
            contract,
            gen_body["content"]["application/json"]["schema"],
            con_body["content"]["application/json"]["schema"],
            f"request {method} {path}",
        )


def test_success_response_schemas_match(contract, generated):
    for path, method, con_op in _ops(contract):
        for status, con_resp in con_op["responses"].items():
            con_obj = _resolve(contract, con_resp)
            gen_responses = generated["paths"][path][method]["responses"]
            gen_obj = _response_object(generated, gen_responses, status)
            con_schema = _content_schema(contract, con_obj)
            gen_schema = _content_schema(generated, gen_obj)
            assert (con_schema is None) == (gen_schema is None), (
                f"{method} {path} {status}: content 存在性不一致"
            )
            if con_schema is None:
                continue
            if int(status) < 300:
                _compare_envelope(
                    generated, contract, gen_schema, con_schema,
                    f"response {method} {path} {status}",
                )
            else:
                _compare_error_schema(
                    generated, contract, gen_schema, con_schema,
                    f"error response {method} {path} {status}",
                )


def _compare_error_schema(gen_spec, con_spec, gen_schema, con_schema, label):
    gen = _schema(gen_spec, gen_schema)
    con = _schema(con_spec, con_schema)
    assert con.get("type") == "object"
    assert set(con.get("required", [])) == {"error"}
    assert set(gen.get("required", [])) == {"error"}, f"{label}: error 必填"
    con_error = _resolve(con_spec, con["properties"]["error"])
    gen_error = _resolve(gen_spec, gen["properties"]["error"])
    assert set(con_error.get("required", [])) == {"code", "message", "requestId"}
    assert set(gen_error.get("required", [])) == {
        "code", "message", "requestId"
    }, f"{label}: error 字段必填集不一致"
    con_code = _resolve(con_spec, con_error["properties"]["code"])
    gen_code = _resolve(gen_spec, gen_error["properties"]["code"])
    assert con_code.get("type") == "string"
    assert gen_code.get("type") == "string"


def test_path_and_header_parameters_match(contract, generated):
    for path, method, con_op in _ops(contract):
        con_param_entries = [
            _resolve(contract, p)
            for p in con_op.get("parameters", [])
            + contract["paths"][path].get("parameters", [])
        ]
        con_params = {p["name"]: p for p in con_param_entries}
        gen_op = generated["paths"][path][method]
        gen_param_entries = [
            _resolve(generated, p)
            for p in gen_op.get("parameters", [])
            + generated["paths"][path].get("parameters", [])
        ]
        gen_params = {p["name"]: p for p in gen_param_entries}
        assert set(gen_params) == set(con_params), (
            f"{method} {path}: 参数不一致 {set(gen_params) ^ set(con_params)}"
        )
        for name, con_param in con_params.items():
            gen_param = gen_params[name]
            assert gen_param.get("in") == con_param.get("in")
            # path 参数两侧都隐含 required
            con_required = (
                True
                if con_param.get("in") == "path"
                else con_param.get("required", False)
            )
            gen_required = (
                True
                if gen_param.get("in") == "path"
                else gen_param.get("required", False)
            )
            assert gen_required == con_required, (
                f"{method} {path} 参数 {name}: required 不一致"
            )
            con_schema = _resolve(contract, con_param["schema"])
            gen_schema = _resolve(generated, gen_param["schema"])
            assert con_schema.get("type") in _type_kinds(gen_schema), (
                f"{method} {path} 参数 {name}: 类型不一致"
            )


def test_304_has_no_content(contract, generated):
    for path, method, con_op in _ops(contract):
        if "304" not in {str(s) for s in con_op["responses"]}:
            continue
        gen_obj = _response_object(
            generated,
            generated["paths"][path][method]["responses"],
            "304",
        )
        assert gen_obj.get("content") in (None, {})


# -- 每个声明错误可产出 ----------------------------------------------------------------


@pytest.fixture()
def small_client(migrated_env):
    from app.config import Settings
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app(
        Settings(
            database_url=migrated_env["db_url"],
            blob_root=migrated_env["blob_root"],
            max_request_bytes=1024,
            idempotency_ttl_seconds=86400,
        )
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, app.state.context


def _new_notebook(client):
    response = client.post(
        "/api/v1/notebooks",
        json={"title": "x"},
        headers={"Idempotency-Key": "create"},
    )
    return response.json()["notebookId"]


ERROR_CASES = [
    # -- POST /api/v1/notebooks --
    ("POST 400 MALFORMED_JSON",
     lambda c, ctx: c.post("/api/v1/notebooks", content=b"{", headers={"Idempotency-Key": "k", "Content-Type": "application/json"}),
     "MALFORMED_JSON", 400, "post"),
    ("POST 409 IDEMPOTENCY_KEY_REUSED",
     lambda c, ctx: (c.post("/api/v1/notebooks", json={"title": "a"}, headers={"Idempotency-Key": "k"}), c.post("/api/v1/notebooks", json={"title": "b"}, headers={"Idempotency-Key": "k"}))[1],
     "IDEMPOTENCY_KEY_REUSED", 409, "post"),
    ("POST 413 PAYLOAD_TOO_LARGE",
     lambda c, ctx: c.post("/api/v1/notebooks", json={"title": "x" * 1500}, headers={"Idempotency-Key": "k"}),
     "PAYLOAD_TOO_LARGE", 413, "post"),
    ("POST 422 INVALID_REQUEST",
     lambda c, ctx: c.post("/api/v1/notebooks", json={}, headers={}),
     "INVALID_REQUEST", 422, "post"),
    ("POST 422 INVALID_NOTEBOOK",
     lambda c, ctx: c.post("/api/v1/notebooks", json={"content": make_notebook(nbformat=3)}, headers={"Idempotency-Key": "k"}),
     "INVALID_NOTEBOOK", 422, "post"),
    ("POST 500 INTERNAL_ERROR",
     lambda c, ctx: _boom(ctx, "create") or c.post("/api/v1/notebooks", json={"title": "x"}, headers={"Idempotency-Key": "k"}),
     "INTERNAL_ERROR", 500, "post"),
    ("POST 503 STORAGE_UNAVAILABLE",
     lambda c, ctx: _blob_down(ctx) or c.post("/api/v1/notebooks", json={"title": "x"}, headers={"Idempotency-Key": "k"}),
     "STORAGE_UNAVAILABLE", 503, "post"),
    # -- GET /api/v1/notebooks/{notebookId} --
    ("GET 404 NOTEBOOK_NOT_FOUND",
     lambda c, ctx: c.get("/api/v1/notebooks/nb_" + "f" * 32),
     "NOTEBOOK_NOT_FOUND", 404, "get"),
    ("GET 422 INVALID_REQUEST",
     lambda c, ctx: c.get("/api/v1/notebooks/not-an-id"),
     "INVALID_REQUEST", 422, "get"),
    ("GET 500 INTERNAL_ERROR",
     lambda c, ctx: _tamper(ctx, c) or c.get(f"/api/v1/notebooks/{_tampered_id(ctx)}"),
     "INTERNAL_ERROR", 500, "get"),
    ("GET 503 STORAGE_UNAVAILABLE",
     lambda c, ctx: _read_fail(ctx, c) or c.get(f"/api/v1/notebooks/{_read_fail_id(ctx)}"),
     "STORAGE_UNAVAILABLE", 503, "get"),
    # -- PUT /api/v1/notebooks/{notebookId} --
    ("PUT 400 MALFORMED_JSON",
     lambda c, ctx: c.put(f"/api/v1/notebooks/{_new_notebook(c)}", content=b"{", headers={"Idempotency-Key": "k", "Content-Type": "application/json"}),
     "MALFORMED_JSON", 400, "put"),
    ("PUT 404 NOTEBOOK_NOT_FOUND",
     lambda c, ctx: c.put("/api/v1/notebooks/nb_" + "f" * 32, json={"baseRevision": 1, "content": make_notebook()}, headers={"Idempotency-Key": "k"}),
     "NOTEBOOK_NOT_FOUND", 404, "put"),
    ("PUT 409 REVISION_CONFLICT",
     lambda c, ctx: _put_conflict(c),
     "REVISION_CONFLICT", 409, "put"),
    ("PUT 409 IDEMPOTENCY_KEY_REUSED",
     lambda c, ctx: _put_key_reuse(c),
     "IDEMPOTENCY_KEY_REUSED", 409, "put"),
    ("PUT 413 PAYLOAD_TOO_LARGE",
     lambda c, ctx: c.put(f"/api/v1/notebooks/{_new_notebook(c)}", json={"baseRevision": 1, "content": {"pad": "x" * 1500}}, headers={"Idempotency-Key": "k"}),
     "PAYLOAD_TOO_LARGE", 413, "put"),
    ("PUT 422 INVALID_REQUEST",
     lambda c, ctx: c.put(f"/api/v1/notebooks/{_new_notebook(c)}", json={"content": make_notebook()}, headers={"Idempotency-Key": "k"}),
     "INVALID_REQUEST", 422, "put"),
    ("PUT 422 INVALID_NOTEBOOK",
     lambda c, ctx: c.put(f"/api/v1/notebooks/{_new_notebook(c)}", json={"baseRevision": 1, "content": make_notebook([make_code_cell(None)])}, headers={"Idempotency-Key": "k"}),
     "INVALID_NOTEBOOK", 422, "put"),
    ("PUT 500 INTERNAL_ERROR",
     lambda c, ctx: _boom(ctx, "save") or c.put(f"/api/v1/notebooks/{_new_notebook(c)}", json={"baseRevision": 1, "content": make_notebook()}, headers={"Idempotency-Key": "k"}),
     "INTERNAL_ERROR", 500, "put"),
    ("PUT 503 STORAGE_UNAVAILABLE",
     lambda c, ctx: _put_with_blob_failure(c, ctx),
     "STORAGE_UNAVAILABLE", 503, "put"),
    # -- GET /api/v1/notebooks/{notebookId}/revisions/{revision} --
    ("REV GET 404 NOTEBOOK_NOT_FOUND",
     lambda c, ctx: c.get("/api/v1/notebooks/nb_" + "f" * 32 + "/revisions/1"),
     "NOTEBOOK_NOT_FOUND", 404, "get_revision"),
    ("REV GET 404 REVISION_NOT_FOUND",
     lambda c, ctx: c.get(f"/api/v1/notebooks/{_new_notebook(c)}/revisions/99"),
     "REVISION_NOT_FOUND", 404, "get_revision"),
    ("REV GET 422 INVALID_REQUEST",
     lambda c, ctx: c.get(f"/api/v1/notebooks/{_new_notebook(c)}/revisions/abc"),
     "INVALID_REQUEST", 422, "get_revision"),
    ("REV GET 500 INTERNAL_ERROR",
     lambda c, ctx: _tamper(ctx, c) or c.get(f"/api/v1/notebooks/{_tampered_id(ctx)}/revisions/1"),
     "INTERNAL_ERROR", 500, "get_revision"),
    ("REV GET 503 STORAGE_UNAVAILABLE",
     lambda c, ctx: _read_fail(ctx, c) or c.get(f"/api/v1/notebooks/{_read_fail_id(ctx)}/revisions/1"),
     "STORAGE_UNAVAILABLE", 503, "get_revision"),
    # -- /health/ready --
    ("READY 503 STORAGE_UNAVAILABLE",
     lambda c, ctx: _rm_blob_root(ctx) or c.get("/health/ready"),
     "STORAGE_UNAVAILABLE", 503, "ready"),
]


def _boom(context, method):
    import types

    def raise_runtime(*args, **kwargs):
        raise RuntimeError("boom")

    if method == "create":
        context.service.create = raise_runtime
    else:
        context.service.save = raise_runtime


def _blob_down(context):
    def failing_put(digest, data):
        raise OSError("simulated failure")

    context.blob_store.put = failing_put


def _put_with_blob_failure(client, context):
    # 先正常创建 Notebook，再让 Blob 写入失败（创建本身也需要写 Blob）。
    nb_id = _new_notebook(client)
    _blob_down(context)
    return client.put(
        f"/api/v1/notebooks/{nb_id}",
        json={"baseRevision": 1, "content": make_notebook([make_code_cell()])},
        headers={"Idempotency-Key": "k"},
    )


def _put_conflict(client):
    nb_id = _new_notebook(client)
    content = make_notebook([make_code_cell("c1", source="x=1")])
    client.put(f"/api/v1/notebooks/{nb_id}", json={"baseRevision": 1, "content": content}, headers={"Idempotency-Key": "a"})
    return client.put(f"/api/v1/notebooks/{nb_id}", json={"baseRevision": 1, "content": content}, headers={"Idempotency-Key": "b"})


def _put_key_reuse(client):
    nb_id = _new_notebook(client)
    content1 = make_notebook([make_code_cell("c1", source="1")])
    content2 = make_notebook([make_code_cell("c1", source="2")])
    client.put(f"/api/v1/notebooks/{nb_id}", json={"baseRevision": 1, "content": content1}, headers={"Idempotency-Key": "k"})
    return client.put(f"/api/v1/notebooks/{nb_id}", json={"baseRevision": 2, "content": content2}, headers={"Idempotency-Key": "k"})


def _rm_blob_root(context):
    import shutil

    shutil.rmtree(context.settings.blob_root)


_TAMPERED: dict = {}


def _tamper(context, client):
    nb_id = _new_notebook(client)
    blob = next((context.settings.blob_root).rglob("*.ipynb"))
    blob.write_bytes(b"tampered")
    _TAMPERED["id"] = nb_id


def _tampered_id(context):
    return _TAMPERED["id"]


def _read_fail(context, client):
    nb_id = _new_notebook(client)
    def failing_get(key):
        raise OSError("simulated read failure")
    context.blob_store.get = failing_get
    _TAMPERED["id"] = nb_id


def _read_fail_id(context):
    return _TAMPERED["id"]


@pytest.mark.parametrize(
    "label, make_request, code, status, op",
    ERROR_CASES,
    ids=[case[0] for case in ERROR_CASES],
)
def test_declared_errors_are_producible(
    small_client, label, make_request, code, status, op
):
    client, context = small_client
    response = make_request(client, context)
    body = response.json()
    assert response.status_code == status, (
        f"{label}: {response.text} url={response.request.url}"
    )
    assert "error" in body, f"{label}: {response.text}"
    envelope = body["error"]
    assert envelope["code"] == code, f"{label}: {response.text}"
    assert envelope["requestId"] == response.headers["x-request-id"]


def test_error_cases_cover_all_declared_errors(contract):
    """交叉检查：OpenAPI 声明的每个 (operation, status) 都有可产出测试覆盖。"""
    covered = {(case[4], case[3]) for case in ERROR_CASES}
    declared = set()
    for path, method, op in _ops(contract):
        op_id = op["operationId"]
        for status in op["responses"]:
            if int(status) >= 400:
                declared.add((op_id, int(status)))
    # operationId 映射到上面的 op 标签
    label_map = {
        "createNotebook": "post",
        "getNotebook": "get",
        "saveNotebook": "put",
        "getNotebookRevision": "get_revision",
        "getReadiness": "ready",
    }
    normalized_declared = {
        (label_map.get(op_id, op_id), status)
        for op_id, status in declared
    }
    missing = normalized_declared - covered
    assert not missing, f"缺少可产出测试的错误响应: {missing}"
