"""The retention of the run history: how many days of runs the service keeps, and the cleanup of the older runs.

The number of days lives in the ``setting`` table. The cleanup deletes each finished run that started before the
retention period, together with its actions. It never deletes a run that is still ``running``.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import Run, RunAction, RunStatus, Setting, utc_now

LOG = logging.getLogger(__name__)

RETENTION_KEY = "history_retention_days"
DEFAULT_RETENTION_DAYS = 90
MIN_RETENTION_DAYS = 1


@dataclass(frozen=True)
class CleanupResult:
    runs: int
    actions: int


def _is_valid_days(value: object) -> bool:
    # A bool is an int in Python, but True is not a number of days.
    return isinstance(value, int) and not isinstance(value, bool) and value >= MIN_RETENTION_DAYS


def get_retention_days(sessions: sessionmaker[Session]) -> int:
    """Return the number of days of history to keep. A missing or wrong stored value gives the default. This call blocks."""
    with sessions() as session:
        row = session.get(Setting, RETENTION_KEY)
        value = row.value if row is not None else None
    if value is None:
        return DEFAULT_RETENTION_DAYS
    if not _is_valid_days(value):
        LOG.warning(
            "The setting %s holds %r, which is not a number of days of %s or more, so the service uses %s",
            RETENTION_KEY,
            value,
            MIN_RETENTION_DAYS,
            DEFAULT_RETENTION_DAYS,
        )
        return DEFAULT_RETENTION_DAYS
    return value


def set_retention_days(sessions: sessionmaker[Session], days: int) -> None:
    """Store the number of days of history to keep. Raise ValueError for a value below the minimum. This call blocks."""
    if not _is_valid_days(days):
        raise ValueError(f"The history is kept for {MIN_RETENTION_DAYS} day or more, got {days!r}")
    with sessions.begin() as session:
        row = session.get(Setting, RETENTION_KEY)
        if row is None:
            session.add(Setting(key=RETENTION_KEY, value=days))
        else:
            row.value = days


def delete_old_runs(sessions: sessionmaker[Session], days: int, now: datetime | None = None) -> CleanupResult:
    """Delete each run that started more than ``days`` days before ``now``, and its actions. This call blocks.

    A run with the status ``running`` stays, however old it is. The actions go first and in the same transaction as
    their runs, so the result does not depend on the cascade of the foreign key.
    """
    cutoff = (now or utc_now()) - timedelta(days=days)
    old = (Run.started_at < cutoff) & (Run.status != RunStatus.RUNNING)
    with sessions.begin() as session:
        actions = session.execute(delete(RunAction).where(RunAction.run_id.in_(select(Run.id).where(old))))
        runs = session.execute(delete(Run).where(old))
        return CleanupResult(runs=runs.rowcount, actions=actions.rowcount)


def clean_history(sessions: sessionmaker[Session]) -> CleanupResult:
    """Delete the runs that are older than the stored retention period. The scheduler calls this each day."""
    days = get_retention_days(sessions)
    result = delete_old_runs(sessions, days)
    if result.runs:
        LOG.info(
            "Deleted %s run(s) and %s action(s) that started more than %s day(s) ago", result.runs, result.actions, days
        )
    return result
