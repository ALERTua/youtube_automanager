"""run summary empty

Revision ID: b3e8f15c7a29
Revises: a7d4c2e9f1b3
Create Date: 2026-10-30 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3e8f15c7a29"
down_revision: str | Sequence[str] | None = "a7d4c2e9f1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The summary of a run without an added video and without an error had the event of each run summary before. So each
# stored target that gets the run summaries gets the new event too, and keeps the messages that it got.
SUMMARY = "run_summary"
EVENT = "run_summary_empty"

_TARGETS = sa.table("notification_target", sa.column("id", sa.Integer()), sa.column("events", sa.JSON()))


def _change_events(change) -> None:
    """Write ``change(events)`` into each stored target whose events it changes."""
    connection = op.get_bind()
    for target_id, events in connection.execute(sa.select(_TARGETS.c.id, _TARGETS.c.events)).all():
        changed = change(list(events or ()))
        if changed != events:
            connection.execute(sa.update(_TARGETS).where(_TARGETS.c.id == target_id).values(events=changed))


def _with_event(events: list[str]) -> list[str]:
    """Put the new event right after the run summary, in the order of the events of the service."""
    if SUMMARY not in events or EVENT in events:
        return events
    position = events.index(SUMMARY) + 1
    return [*events[:position], EVENT, *events[position:]]


def upgrade() -> None:
    _change_events(_with_event)


def downgrade() -> None:
    # The older version has no such event, so it goes out of the stored targets.
    _change_events(lambda events: [event for event in events if event != EVENT])
