# Notebook Service（v1 / NS-C1）

Notebook 文档平面（Document Plane）服务：Notebook 的唯一真相源。浏览器编辑完整
nbformat 4 文档并全量保存；Runtime、Jupyter Server、Session、Kernel 不参与本服务
的文档持久化。

对外契约见 [docs/api/notebook-service-v1.openapi.yaml](../docs/api/notebook-service-v1.openapi.yaml)，
实现约束见 [docs/Notebook_Service_v1_Implementation_Guide.md](../docs/Notebook_Service_v1_Implementation_Guide.md)。

## v1 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/notebooks` | 创建 Notebook（revision 1），创建阶段规范化缺失/重复 Cell ID |
| `GET` | `/api/v1/notebooks/{notebookId}` | 读取最新 revision（支持 `If-None-Match` → 304） |
| `PUT` | `/api/v1/notebooks/{notebookId}` | 全量保存（`baseRevision` 乐观并发，严格 nbformat 校验） |
| `GET` | `/api/v1/notebooks/{notebookId}/revisions/{revision}` | 读取不可变历史 revision |
| `GET` | `/health/live` | 存活检查（不访问外部依赖） |
| `GET` | `/health/ready` | 就绪检查（数据库 + Blob 根目录） |

`POST`/`PUT` 强制携带 `Idempotency-Key` 头。所有响应带 `X-Request-ID`，错误使用
统一 envelope：

```json
{
  "error": {
    "code": "REVISION_CONFLICT",
    "message": "Notebook has been modified",
    "requestId": "req_01J7ABCDEFXYZ1234567890",
    "details": { "baseRevision": 12, "currentRevision": 13, "currentContentHash": "sha256:…" }
  }
}
```

## 与旧 POC route 的关系

`POST/GET/PUT /api/notebooks` 旧 route **仅作为 POC 兼容入口保留**：

- 响应结构保持 POC 原样，且 `include_in_schema=False`，不出现在 v1 OpenAPI 中；
- 继续使用原 `LocalNotebookRepository` 和 `data/notebooks/` 数据目录；
- **两套 route 暂不互通**：v1 使用 SQLite + content-addressed Blob
  （`data/v1/`），旧 route 使用 `data/notebooks/` 文件目录，互不可见；
- 不提供 v0 → v1 自动迁移；如确需保留 POC 文档，另行设计显式、可回滚的迁移工具。

这是过渡状态，不是生产方案；新前端和新测试只使用 `/api/v1`。

## 启动

```bash
cd notebook-service
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 首次使用：创建默认数据目录并执行数据库迁移
mkdir -p data/v1
.venv/bin/alembic upgrade head

# 使用 Uvicorn factory 模式启动（配置错误会在启动阶段快速失败）
.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

`/openapi.json` 为 FastAPI 生成的实时契约；旧 POC route 不会出现在其中。

## 配置

全部通过环境变量（默认值见 [app/config.py](app/config.py)）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NOTEBOOK_DATABASE_URL` | `sqlite:///./data/v1/notebooks.sqlite3` | 相对路径按 `notebook-service/` 解析 |
| `NOTEBOOK_BLOB_ROOT` | `./data/v1/blobs` | 同上，启动时创建 |
| `NOTEBOOK_MAX_REQUEST_BYTES` | `20971520`（20 MiB） | 超限请求在 JSON/nbformat/Blob/DB 之前返回 413 |
| `NOTEBOOK_IDEMPOTENCY_TTL_SECONDS` | `86400`（24 h） | 幂等记录过期时间（NS-C1 不主动清理，为后续预留） |

## 数据布局

```text
data/v1/
├── notebooks.sqlite3        # metadata：notebooks / notebook_revisions / idempotency_records
└── blobs/sha256/xx/<digest>.ipynb   # content-addressed 不可变 Blob（canonical bytes）
```

- Blob 先于 metadata 原子发布（temp + fsync + rename）；数据库失败可能留下不可见
  孤儿 Blob，不影响正确性，本轮不做 GC。
- 写事务使用 SQLite `BEGIN IMMEDIATE` + WAL + `synchronous=FULL` +
  `busy_timeout=5000`；head 推进使用条件 UPDATE（CAS），revision 行不可变。

## 测试

```bash
.venv/bin/python3 -m pytest
```

覆盖：contract（OpenAPI 语义比较）、nbformat、revision、幂等、真实 SQLite 并发、
失败边界、health。测试只使用临时目录和临时数据库，不读写仓库中的 `data/`。

## 当前限制（单机正确性基线）

NS-C1 只承诺**单台机器**上的正确性：

- Metadata：SQLite；Content Blob：本地文件系统；
- 可使用同一台机器上的多个 Uvicorn worker；
- **不支持多节点**、高可用或云存储；
- 尚未支持：认证/ACL、列表/目录/删除、重命名、快照、版本列表、配额、
  幂等记录清理、Blob GC。

SQLite 和本地 Blob 只是接口实现，不渗透到 API 层或 domain service；后续替换为
PostgreSQL + OSS 时不应修改 v1 HTTP 契约与核心业务流程。
