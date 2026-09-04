"""Health 接口：/health/live 与 /health/ready。"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..errors import StorageUnavailable
from ..schemas import HealthResponse
from .responses import REQUEST_ID_HEADER, error_docs, success_docs

router = APIRouter(tags=["Health"])

READY_RESPONSES = {
    200: success_docs(
        "所有必要依赖可用",
        HealthResponse,
        headers=REQUEST_ID_HEADER,
    ),
    503: error_docs(
        "Notebook 持久化依赖暂时不可用", retry_after=True
    ),
}


@router.get(
    "/health/live",
    operation_id="getLiveness",
    response_model=HealthResponse,
    responses={
        200: success_docs(
            "服务进程存活", HealthResponse, headers=REQUEST_ID_HEADER
        )
    },
)
def get_liveness():
    # 不访问数据库或 BlobStore。
    return HealthResponse(status="ok")


@router.get(
    "/health/ready",
    operation_id="getReadiness",
    response_model=HealthResponse,
    responses=READY_RESPONSES,
)
def get_readiness(request: Request):
    context = request.app.state.context

    # 轻量数据库查询。
    try:
        with context.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as error:
        raise StorageUnavailable("database") from error

    # Blob 根目录存在且进程具备必要访问权限；不创建永久探针 Blob。
    blob_root = context.settings.blob_root
    if not (
        blob_root.is_dir()
        and os.access(blob_root, os.R_OK | os.W_OK | os.X_OK)
    ):
        raise StorageUnavailable("blob")

    return HealthResponse(status="ok")
