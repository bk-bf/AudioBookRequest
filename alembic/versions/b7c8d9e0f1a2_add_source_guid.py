"""add source_guid to download queue

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-07-16 20:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("downloadqueueitem", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source_guid", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("downloadqueueitem", schema=None) as batch_op:
        batch_op.drop_column("source_guid")
