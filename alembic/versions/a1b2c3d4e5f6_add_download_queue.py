"""add download queue and downloaded_path

Revision ID: a1b2c3d4e5f6
Revises: cafe562e2832, 9420621b2ed4, 6477fe89a011, 63489e50e337, 1718055d5ca8
Create Date: 2026-07-15 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = (
    "cafe562e2832",
    "9420621b2ed4",
    "6477fe89a011",
    "63489e50e337",
    "1718055d5ca8",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "downloadqueueitem",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("asin", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("manual_request_id", sa.Uuid(), nullable=True),
        sa.Column("download_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("client", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("indexer", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("protocol", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column(
            "state",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("eta_seconds", sa.Integer(), nullable=True),
        sa.Column("download_speed", sa.Integer(), nullable=True),
        sa.Column("save_path", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("import_path", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_downloadqueueitem_asin"), "downloadqueueitem", ["asin"], unique=False
    )
    op.create_index(
        op.f("ix_downloadqueueitem_manual_request_id"),
        "downloadqueueitem",
        ["manual_request_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_downloadqueueitem_download_id"),
        "downloadqueueitem",
        ["download_id"],
        unique=False,
    )
    with op.batch_alter_table("audiobook", schema=None) as batch_op:
        batch_op.add_column(sa.Column("downloaded_path", sa.String(), nullable=True))
    with op.batch_alter_table("manualbookrequest", schema=None) as batch_op:
        batch_op.add_column(sa.Column("downloaded_path", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("manualbookrequest", schema=None) as batch_op:
        batch_op.drop_column("downloaded_path")
    with op.batch_alter_table("audiobook", schema=None) as batch_op:
        batch_op.drop_column("downloaded_path")
    op.drop_index(op.f("ix_downloadqueueitem_download_id"), "downloadqueueitem")
    op.drop_index(op.f("ix_downloadqueueitem_manual_request_id"), "downloadqueueitem")
    op.drop_index(op.f("ix_downloadqueueitem_asin"), "downloadqueueitem")
    op.drop_table("downloadqueueitem")
