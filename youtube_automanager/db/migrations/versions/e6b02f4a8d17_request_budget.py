"""request budget

Revision ID: e6b02f4a8d17
Revises: c41e7a9d3b58
Create Date: 2026-10-28 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6b02f4a8d17"
down_revision: str | Sequence[str] | None = "c41e7a9d3b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The table starts empty. The budget makes its one row at the first use, with a full bucket.
    op.create_table(
        "request_budget",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("tokens", sa.Double(), nullable=False),
        sa.Column("refilled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_answer_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_request_budget_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_request_budget")),
    )


def downgrade() -> None:
    op.drop_table("request_budget")
