"""rule catch up since

Revision ID: 3a7c5e9b2d14
Revises: 8d3e61b2c4a7
Create Date: 2026-10-18 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3a7c5e9b2d14"
down_revision: str | Sequence[str] | None = "8d3e61b2c4a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The stored rules get no catch-up, because they already add their new videos.
    op.add_column("rule", sa.Column("catch_up_since", sa.Date(), nullable=True))


def downgrade() -> None:
    # SQLite 3.35 and later drops a plain column in place, so the table needs no copy.
    op.drop_column("rule", "catch_up_since")
