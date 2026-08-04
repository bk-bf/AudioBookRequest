"""add downloaded_at to audiobook

The wishlist used to sort on audiobook.updated_at, which is the audible
metadata cache marker - every search/refetch re-stamps it, floating old
library books back to the top of the Downloaded tab. downloaded_at records
when the book actually entered the library instead.

Backfill prefers the real import timestamp from the download queue and falls
back to updated_at for books that predate the queue (e.g. ABS library sync).

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-08-04 16:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "d0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("audiobook", schema=None) as batch_op:
        batch_op.add_column(sa.Column("downloaded_at", sa.DateTime(), nullable=True))

    op.execute(
        sa.text(
            """
            UPDATE audiobook
            SET downloaded_at = COALESCE(
                (
                    SELECT MIN(q.updated_at)
                    FROM downloadqueueitem q
                    WHERE q.asin = audiobook.asin AND q.state = 'imported'
                ),
                updated_at
            )
            WHERE downloaded = true
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("audiobook", schema=None) as batch_op:
        batch_op.drop_column("downloaded_at")
