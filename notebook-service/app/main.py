"""应用装配：app 工厂、router、middleware、异常映射。

使用 Uvicorn factory 模式启动（不做模块级 app 实例，避免 import 时触碰
真实数据目录）：

    uvicorn app.main:create_app --factory
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .api import health, legacy, notebooks
from .config import Settings, load_settings
from .database import build_engine, build_session_factory
from .errors import (
    DomainError,
    InternalError,
    InvalidRequest,
    MalformedJson,
    StorageUnavailable,
)
from .middleware import RequestIdMiddleware, SizeLimitMiddleware, error_body
from .repositories.sqlalchemy import SqlAlchemyNotebookRepository
from .services.notebooks import NotebookService
from .storage.local import LocalBlobStore

logger = logging.getLogger("notebook_service")

# 新 v1 契约作用的路径前缀；其余路径（旧 POC route、/docs、/openapi.json 等）
# 保持原有错误行为。
_V1_PREFIXES = ("/api/v1", "/health")


@dataclass
class AppContext:
    settings: Settings
    engine: object
    session_factory: object
    repository: SqlAlchemyNotebookRepository
    blob_store: LocalBlobStore
    service: NotebookService


def create_app(settings: Settings | None = None) -> FastAPI:
    # 配置错误/存储不可用时在启动阶段快速失败。
    settings = settings or load_settings()
    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)
    repository = SqlAlchemyNotebookRepository(
        session_factory, settings.idempotency_ttl_seconds
    )
    blob_store = LocalBlobStore(settings.blob_root)
    service = NotebookService(repository, blob_store)

    app = FastAPI(title="Notebook Service API", version="1.0.0-draft.1")
    app.state.context = AppContext(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        repository=repository,
        blob_store=blob_store,
        service=service,
    )

    # 先注册 SizeLimit（内层），后注册 RequestId（外层）：RequestId 保证所有
    # 响应（含 413 提前拒绝）都携带同一个 X-Request-ID。
    app.add_middleware(SizeLimitMiddleware, limit_bytes=settings.max_request_bytes)
    app.add_middleware(RequestIdMiddleware)

    _register_exception_handlers(app)

    app.include_router(notebooks.router)
    app.include_router(health.router)
    app.include_router(legacy.router)
    return app


def _is_v1(request: Request) -> bool:
    return request.scope.get("path", "").startswith(_V1_PREFIXES)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _validation_details(errors) -> dict | None:
    first = errors[0] if errors else None
    if first is None:
        return None
    loc = first.get("loc", ())
    if loc and loc[0] == "header":
        pointer = "/headers/" + str(loc[1])
    elif loc and loc[0] in ("body", "query", "path"):
        pointer = "/" + "/".join(str(part) for part in loc[1:])
    else:
        pointer = "/"
    return {"path": pointer, "reason": first.get("msg", "Invalid value")}


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        body = error_body(
            exc.code, exc.message, _request_id(request), exc.details
        )
        headers = {}
        if isinstance(exc, StorageUnavailable):
            headers["Retry-After"] = "5"
        return JSONResponse(
            status_code=exc.http_status, headers=headers, content=body
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ):
        if not _is_v1(request):
            # 旧 POC route 保持 FastAPI 默认 422 响应结构。
            return JSONResponse(
                status_code=422,
                content={"detail": jsonable_encoder(exc.errors())},
            )
        if any(error.get("type") == "json_invalid" for error in exc.errors()):
            error = MalformedJson()
        else:
            error = InvalidRequest(details=_validation_details(exc.errors()))
        return JSONResponse(
            status_code=error.http_status,
            content=error_body(
                error.code, error.message, _request_id(request), error.details
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        if isinstance(exc, HTTPException) or not _is_v1(request):
            # 旧 POC route 与框架 4xx/5xx 保持原有行为（交给默认处理器）。
            raise exc
        logger.exception(
            "未处理异常 request_id=%s path=%s",
            _request_id(request),
            request.scope.get("path"),
        )
        error = InternalError()
        request_id = _request_id(request)
        # 该响应由 ServerErrorMiddleware 直接发送，绕过 RequestId middleware 的
        # send 包装，因此这里显式携带 X-Request-ID 响应头。
        return JSONResponse(
            status_code=500,
            headers={"X-Request-ID": request_id},
            content=error_body(error.code, error.message, request_id),
        )
