"""v1 Notebook 文档接口。

- 请求体先做严格 JSON 解析（拒绝 NaN/Infinity），在生成随机 Notebook ID、
  默认 content 或 Cell ID 之前计算原始请求摘要（幂等 hash）。
- 状态码、响应头与响应 schema 与 docs/api/notebook-service-v1.openapi.yaml 对齐。
"""
from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request
from fastapi.responses import JSONResponse, Response

from ..errors import MalformedJson
from ..nbformat_codec import canonical_json_bytes, sha256_hex
from ..schemas import (
    NOTEBOOK_ID_PATTERN,
    CreateNotebookRequest,
    NotebookDocumentResponse,
    SaveNotebookRequest,
    SaveNotebookResponse,
)
from .responses import (
    ETAG_HEADER,
    REQUEST_ID_HEADER,
    error_docs,
    success_docs,
)

router = APIRouter(tags=["Notebooks"])

_IDEMPOTENCY_KEY = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
]
_IF_NONE_MATCH = Annotated[str | None, Header(alias="If-None-Match")]
_NOTEBOOK_ID = Annotated[str, Path(pattern=NOTEBOOK_ID_PATTERN)]
_REVISION = Annotated[int, Path(ge=1)]

_INVALID_REQUEST_OR_NOTEBOOK = "请求结构错误或 content 不符合 nbformat 4"

CREATE_RESPONSES = {
    201: success_docs(
        "Notebook 已创建；幂等重放返回相同资源和相同业务结果",
        NotebookDocumentResponse,
        headers={
            "Location": {
                "description": "新建 Notebook 的资源路径",
                "schema": {"type": "string"},
            },
            **ETAG_HEADER,
            **REQUEST_ID_HEADER,
        },
    ),
    400: error_docs("请求体不是合法 JSON"),
    409: error_docs("相同幂等键被用于不同的请求内容"),
    413: error_docs("请求体或 Notebook 内容超过部署限制"),
    422: error_docs(_INVALID_REQUEST_OR_NOTEBOOK),
    500: error_docs("未预期的服务内部错误"),
    503: error_docs("Notebook 持久化依赖暂时不可用", retry_after=True),
}

GET_RESPONSES = {
    200: success_docs(
        "最新 Notebook 文档",
        NotebookDocumentResponse,
        headers={**ETAG_HEADER, **REQUEST_ID_HEADER},
    ),
    304: {
        "description": "客户端持有的 ETag 仍对应当前 revision",
        "headers": {**ETAG_HEADER, **REQUEST_ID_HEADER},
    },
    404: error_docs("Notebook 不存在"),
    422: error_docs("请求字段、路径参数或请求头不符合契约"),
    500: error_docs("未预期的服务内部错误"),
    503: error_docs("Notebook 持久化依赖暂时不可用", retry_after=True),
}

SAVE_RESPONSES = {
    200: success_docs(
        "保存成功、内容未变化，或命中相同请求的幂等重放。响应不重复返回完整 content",
        SaveNotebookResponse,
        headers={**ETAG_HEADER, **REQUEST_ID_HEADER},
    ),
    400: error_docs("请求体不是合法 JSON"),
    404: error_docs("Notebook 不存在"),
    409: error_docs("Revision 冲突或幂等键被用于不同请求"),
    413: error_docs("请求体或 Notebook 内容超过部署限制"),
    422: error_docs(_INVALID_REQUEST_OR_NOTEBOOK),
    500: error_docs("未预期的服务内部错误"),
    503: error_docs("Notebook 持久化依赖暂时不可用", retry_after=True),
}

REVISION_GET_RESPONSES = {
    200: success_docs(
        "指定 revision 的完整 Notebook 文档",
        NotebookDocumentResponse,
        headers={**ETAG_HEADER, **REQUEST_ID_HEADER},
    ),
    304: {
        "description": "客户端持有的 ETag 已对应此不可变 revision",
        "headers": {**ETAG_HEADER, **REQUEST_ID_HEADER},
    },
    404: error_docs("Notebook 或 revision 不存在"),
    422: error_docs("请求字段、路径参数或请求头不符合契约"),
    500: error_docs("未预期的服务内部错误"),
    503: error_docs("Notebook 持久化依赖暂时不可用", retry_after=True),
}


def _service(request: Request):
    return request.app.state.context.service


def _reject_json_constant(constant: str):
    raise ValueError(f"非法的 JSON 常量: {constant}")


async def capture_request_envelope(request: Request) -> dict:
    """严格解析原始请求体，并计算原始请求摘要（幂等 hash）。

    - 摘要基于客户端实际发送的 envelope（仅含发送的字段），在生成随机
      Notebook ID、默认 content 或 Cell ID 之前完成；
    - NaN/Infinity/-Infinity 不是合法 JSON，与语法错误一样返回 MALFORMED_JSON；
    - route 是同步 `def`，原始 body 读取放在异步依赖中完成。
    """
    try:
        text = (await request.body()).decode("utf-8")
    except UnicodeDecodeError as error:
        raise MalformedJson() from error
    try:
        parsed = json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise MalformedJson(
            details={"position": getattr(error, "pos", None)}
        ) from error
    request.state.request_hash = sha256_hex(canonical_json_bytes(parsed))
    return parsed


_RAW_ENVELOPE = Annotated[dict, Depends(capture_request_envelope)]


def _request_hash(request: Request) -> str:
    return request.state.request_hash


def _document_response(
    result, status_code: int = 200, headers: dict[str, str] | None = None
) -> JSONResponse:
    payload = NotebookDocumentResponse(
        notebookId=result.notebook_id,
        title=result.title,
        revision=result.revision,
        contentHash=result.content_hash,
        createdAt=result.created_at,
        updatedAt=result.updated_at,
        content=result.content,
    )
    return JSONResponse(
        status_code=status_code,
        headers=headers or {},
        content=payload.model_dump(mode="json"),
    )


@router.post(
    "/api/v1/notebooks",
    operation_id="createNotebook",
    status_code=201,
    response_model=NotebookDocumentResponse,
    responses=CREATE_RESPONSES,
)
def create_notebook(
    request: Request,
    body: CreateNotebookRequest,
    idempotency_key: _IDEMPOTENCY_KEY,
    envelope: _RAW_ENVELOPE,
):
    result = _service(request).create(
        idempotency_key=idempotency_key,
        request_hash=_request_hash(request),
        title=body.title,
        content=body.content,
    )
    return _document_response(
        result, status_code=result.status_code, headers=result.headers
    )


@router.get(
    "/api/v1/notebooks/{notebookId}",
    operation_id="getNotebook",
    response_model=NotebookDocumentResponse,
    responses=GET_RESPONSES,
)
def get_notebook(
    request: Request,
    notebookId: _NOTEBOOK_ID,
    if_none_match: _IF_NONE_MATCH = None,
):
    result = _service(request).get(notebookId, if_none_match)
    if result.kind == "not_modified":
        return Response(status_code=304, headers={"ETag": result.etag})
    return _document_response(
        result, status_code=200, headers={"ETag": result.etag}
    )


@router.put(
    "/api/v1/notebooks/{notebookId}",
    operation_id="saveNotebook",
    response_model=SaveNotebookResponse,
    responses=SAVE_RESPONSES,
)
def save_notebook(
    request: Request,
    notebookId: _NOTEBOOK_ID,
    body: SaveNotebookRequest,
    idempotency_key: _IDEMPOTENCY_KEY,
    envelope: _RAW_ENVELOPE,
):
    result = _service(request).save(
        idempotency_key=idempotency_key,
        request_hash=_request_hash(request),
        notebook_id=notebookId,
        base_revision=body.baseRevision,
        content=body.content,
    )
    payload = SaveNotebookResponse(
        notebookId=result.notebook_id,
        revision=result.revision,
        contentHash=result.content_hash,
        updatedAt=result.updated_at,
        unchanged=result.unchanged,
    )
    return JSONResponse(
        status_code=200,
        headers={"ETag": result.etag},
        content=payload.model_dump(mode="json"),
    )


@router.get(
    "/api/v1/notebooks/{notebookId}/revisions/{revision}",
    operation_id="getNotebookRevision",
    response_model=NotebookDocumentResponse,
    responses=REVISION_GET_RESPONSES,
)
def get_notebook_revision(
    request: Request,
    notebookId: _NOTEBOOK_ID,
    revision: _REVISION,
    if_none_match: _IF_NONE_MATCH = None,
):
    result = _service(request).get_revision(
        notebookId, revision, if_none_match
    )
    if result.kind == "not_modified":
        return Response(status_code=304, headers={"ETag": result.etag})
    return _document_response(
        result, status_code=200, headers={"ETag": result.etag}
    )
