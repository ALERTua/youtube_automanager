"""request breaker

Revision ID: a7d4c2e9f1b3
Revises: e6b02f4a8d17
Create Date: 2026-10-28 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d4c2e9f1b3"
down_revision: str | Sequence[str] | None = "e6b02f4a8d17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REASONS = ("http_429", "try_again_later", "bot_check")
# The notification event of the breaker. Each stored target gets it, as a new target does.
EVENT = "request_breaker"

_TARGETS = sa.table("notification_target", sa.column("id", sa.Integer()), sa.column("events", sa.JSON()))


def _string_enum(*values: str) -> sa.Enum:
    """A string column for an enumeration. Its CHECK constraint is a separate named constraint of the table."""
    return sa.Enum(*values, native_enum=False, create_constraint=False)


def _change_events(change) -> None:
    """Write ``change(events)`` into each stored target whose events it changes."""
    connection = op.get_bind()
    for target_id, events in connection.execute(sa.select(_TARGETS.c.id, _TARGETS.c.events)).all():
        changed = change(list(events or ()))
        if changed != events:
            connection.execute(sa.update(_TARGETS).where(_TARGETS.c.id == target_id).values(events=changed))


def upgrade() -> None:
    # SQLite cannot add a CHECK constraint to a table, so the batch mode copies the table there.
    values = ", ".join(f"'{value}'" for value in REASONS)
    with op.batch_alter_table("request_budget") as batch:
        batch.add_column(sa.Column("breaker_until", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("breaker_level", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("breaker_reason", _string_enum(*REASONS), nullable=True))
        batch.add_column(sa.Column("breaker_opened_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint(
            op.f("ck_request_budget_breaker_reason"), sa.text(f"breaker_reason IN ({values})")
        )
    _change_events(lambda events: events if EVENT in events else [*events, EVENT])


def downgrade() -> None:
    # The older schema has no such event, so it goes out of the stored targets.
    _change_events(lambda events: [event for event in events if event != EVENT])
    with op.batch_alter_table("request_budget") as batch:
        batch.drop_constraint(op.f("ck_request_budget_breaker_reason"), type_="check")
        batch.drop_column("breaker_opened_at")
        batch.drop_column("breaker_reason")
        batch.drop_column("breaker_level")
        batch.drop_column("breaker_until")
