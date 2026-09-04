"""notebooks 增加 nullable UTC deleted_at（NS-D1-DELETE 软删除）

Revision ID: 20260904_0002
Revises: 20260903_0001
Create Date: 2026-09-04

已有记录自然保持 deleted_at IS NULL；不删除、不改写任何现有数据。
软删除只更新 deleted_at，不得因删除改变 current_revision、current_content_hash
或 updated_at；revision 行与 Blob 保留。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260904_0002"
down_revision: Union[str, None] = "20260903_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "notebooks",
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    # SQLite 的 DROP COLUMN 依赖其内部重建表逻辑，其余列数据保留。
    op.drop_column("notebooks", "deleted_at")
