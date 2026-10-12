"""cookie versions

Revision ID: 5f2c9a7d1e43
Revises: bce2013af8cf
Create Date: 2026-10-12 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5f2c9a7d1e43"
down_revision: str | Sequence[str] | None = "bce2013af8cf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _string_enum(*values: str) -> sa.Enum:
    """A string column for an enumeration. Its CHECK constraint is a separate named constraint of the table."""
    return sa.Enum(*values, native_enum=False, create_constraint=False)


def upgrade() -> None:
    op.create_table(
        "cookie_version",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin", _string_enum("upload", "write_back", "rotation"), nullable=False),
        sa.Column("state", _string_enum("unchecked", "good", "dead"), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cookies", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("origin IN ('upload', 'write_back', 'rotation')", name=op.f("ck_cookie_version_origin")),
        sa.CheckConstraint("state IN ('unchecked', 'good', 'dead')", name=op.f("ck_cookie_version_state")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cookie_version")),
        # SQLite gives an id again after the row with the highest id went, unless the table has AUTOINCREMENT.
        sqlite_autoincrement=True,
    )
    op.add_column("account", sa.Column("expiry_warning_days", sa.Integer(), nullable=True))
    # The cookies that an account already holds become the first version of their chain.
    op.execute(
        sa.text(
            "INSERT INTO cookie_version (created_at, origin, state, cookies) "
            "SELECT CURRENT_TIMESTAMP, 'upload', 'unchecked', cookies FROM account WHERE cookies IS NOT NULL"
        )
    )


def downgrade() -> None:
    # account.cookies keeps a copy of the current version, so the older schema still has the cookies.
    op.drop_column("account", "expiry_warning_days")
    op.drop_table("cookie_version")
