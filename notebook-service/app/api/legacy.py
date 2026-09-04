"""旧 POC route：过渡期兼容入口，不出现在 v1 OpenAPI 中。

- 不删除、不改变现有响应结构；
- 继续使用原 LocalNotebookRepository 和原数据目录 data/notebooks；
- 与 v1 route 不共享持久化数据；新前端和新测试不得调用这些 route。
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..models import CreateNotebookRequest, SaveNotebookRequest
from ..repository import LocalNotebookRepository, NotebookNotFound, RevisionConflict

router = APIRouter(include_in_schema=False)

# 原 app/main.py 中 Path(__file__).parent.parent 指向 notebook-service/；
# 本文件位于 app/api/，因此向上三级。
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "notebooks"

repository = LocalNotebookRepository(DATA_DIR)


@router.post("/api/notebooks")
def create_notebook(req: CreateNotebookRequest):
    notebook_id, revision, content = repository.create(
        req.content
    )

    return {
        "notebookId": notebook_id,
        "revision": revision,
        "content": content,
    }


@router.get("/api/notebooks/{notebook_id}")
def get_notebook(notebook_id: str):
    try:
        revision, content = repository.get(
            notebook_id
        )
    except NotebookNotFound:
        raise HTTPException(
            status_code=404,
            detail="Notebook not found",
        )

    return {
        "notebookId": notebook_id,
        "revision": revision,
        "content": content,
    }


@router.put("/api/notebooks/{notebook_id}")
def save_notebook(
    notebook_id: str,
    req: SaveNotebookRequest,
):
    try:
        revision, content = repository.save(
            notebook_id,
            req.baseRevision,
            req.content,
        )
    except NotebookNotFound:
        raise HTTPException(
            status_code=404,
            detail="Notebook not found",
        )
    except RevisionConflict as error:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Revision conflict",
                "currentRevision": error.current_revision,
            },
        )

    return {
        "notebookId": notebook_id,
        "revision": revision,
        "content": content,
    }
