"""mark queue items rejected by verification

Distinguishes "the download finished but it was the wrong book" from "the
download failed", so the UI can say which happened instead of showing every
failure as a generic error.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-04 19:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("downloadqueueitem", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "rejected", sa.Boolean(), nullable=False, server_default="false"
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("downloadqueueitem", schema=None) as batch_op:
        batch_op.drop_column("rejected")
