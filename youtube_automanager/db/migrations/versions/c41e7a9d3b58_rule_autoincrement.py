"""rule autoincrement

Revision ID: c41e7a9d3b58
Revises: 3a7c5e9b2d14
Create Date: 2026-10-27 19:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c41e7a9d3b58"
down_revision: str | Sequence[str] | None = "3a7c5e9b2d14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The highest rule id that the database knows: of a rule, or of an action of the history, which keeps the id of a deleted
# rule. NULL when the database knows no rule id.
HIGHEST_RULE_ID = (
    "SELECT MAX(id) FROM (SELECT MAX(id) AS id FROM rule UNION ALL SELECT MAX(rule_id) AS id FROM run_action) AS ids"
)


def _rebuild_rule(autoincrement: bool) -> None:
    """Copy the SQLite table ``rule`` into a new table with or without AUTOINCREMENT. The rows keep their ids.

    The engine of the migrations leaves the foreign keys of SQLite off, so the drop of the old table deletes no row of
    ``rule_channel`` and changes no row of ``added_video``. Their foreign keys name the table ``rule``, so they point to
    the new table.
    """
    with op.batch_alter_table("rule", recreate="always", table_kwargs={"sqlite_autoincrement": autoincrement}):
        pass


def upgrade() -> None:
    # Without AUTOINCREMENT, SQLite gives the id of the newest rule again after its delete, and the new rule would show
    # the history of the deleted one. PostgreSQL never gives an id of its sequence again.
    if op.get_bind().dialect.name != "sqlite":
        return
    _rebuild_rule(True)
    # A rule that went before this migration can still have actions in the history, so the next rule starts above them.
    op.execute(sa.text("DELETE FROM sqlite_sequence WHERE name = 'rule'"))
    op.execute(
        sa.text(
            f"INSERT INTO sqlite_sequence (name, seq) SELECT 'rule', ({HIGHEST_RULE_ID}) "
            f"WHERE ({HIGHEST_RULE_ID}) IS NOT NULL"
        )
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    # The drop of the table with AUTOINCREMENT also removes its row of sqlite_sequence.
    _rebuild_rule(False)
