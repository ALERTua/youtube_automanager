"""rule apply trigger

Revision ID: 8d3e61b2c4a7
Revises: 5f2c9a7d1e43
Create Date: 2026-10-12 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8d3e61b2c4a7"
down_revision: str | Sequence[str] | None = "5f2c9a7d1e43"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TRIGGERS = ("schedule", "manual")
NEW_TRIGGERS = ("schedule", "manual", "rule_apply")


def _string_enum(*values: str) -> sa.Enum:
    """A string column for an enumeration. Its CHECK constraint is a separate named constraint of the table."""
    return sa.Enum(*values, native_enum=False, create_constraint=False)


def _change_triggers(old: Sequence[str], new: Sequence[str]) -> None:
    """Give ``run.trigger`` the values ``new``: the CHECK constraint and the length of the column.

    The column is as long as the longest value, so PostgreSQL refuses a longer value. SQLite cannot change a CHECK
    constraint, so the batch mode copies the table there.
    """
    values = ", ".join(f"'{value}'" for value in new)
    with op.batch_alter_table("run") as batch:
        batch.drop_constraint(op.f("ck_run_trigger"), type_="check")
        batch.alter_column(
            "trigger", existing_type=_string_enum(*old), type_=_string_enum(*new), existing_nullable=False
        )
        batch.create_check_constraint(op.f("ck_run_trigger"), sa.text(f'"trigger" IN ({values})'))


def upgrade() -> None:
    _change_triggers(OLD_TRIGGERS, NEW_TRIGGERS)


def downgrade() -> None:
    # The older schema has no rule apply. An apply is a run that the user started, so it stays in the history as one.
    op.execute(sa.text("UPDATE run SET \"trigger\" = 'manual' WHERE \"trigger\" = 'rule_apply'"))
    _change_triggers(NEW_TRIGGERS, OLD_TRIGGERS)
