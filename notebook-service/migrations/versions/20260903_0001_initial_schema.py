"""NS-C1 初始 schema：notebooks / notebook_revisions / idempotency_records

Revision ID: 20260903_0001
Revises:
Create Date: 2026-09-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260903_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # revision 行只允许 INSERT（应用层约定，无 UPDATE/DELETE 业务方法）。
    op.create_table(
        "notebooks",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("current_revision", sa.BigInteger(), nullable=False),
        sa.Column("current_content_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_notebooks"),
        sa.CheckConstraint(
            "length(title) > 0", name="ck_notebooks_title_not_empty"
        ),
        sa.CheckConstraint(
            "current_revision >= 1",
            name="ck_notebooks_current_revision_positive",
        ),
    )

    op.create_table(
        "notebook_revisions",
        sa.Column("notebook_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("blob_key", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("notebook_id", "revision", name="pk_notebook_revisions"),
        sa.ForeignKeyConstraint(
            ["notebook_id"],
            ["notebooks.id"],
            name="fk_notebook_revisions_notebook_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_revisions_positive"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_revisions_size_nonnegative"),
    )

    op.create_table(
        "idempotency_records",
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("result_notebook_id", sa.Text(), nullable=False),
        sa.Column("result_revision", sa.BigInteger(), nullable=False),
        sa.Column("result_metadata", sa.Text(), nullable=False),
        sa.Column("response_headers", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("scope", "key", name="pk_idempotency_records"),
        sa.CheckConstraint(
            "length(key) > 0", name="ck_idempotency_key_not_empty"
        ),
    )


def downgrade() -> None:
    op.drop_table("idempotency_records")
    op.drop_table("notebook_revisions")
    op.drop_table("notebooks")
