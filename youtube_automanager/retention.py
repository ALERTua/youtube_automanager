"""The retention of the run history: how many days of runs the service keeps, and the daily cleanup.

The number of days lives in the ``setting`` table. The cleanup deletes each finished run that started before the
retention period, together with its actions. It never deletes a run that is still ``running``.

The same cleanup deletes each ``added_video`` row that is older than ADDED_VIDEO_KEEP_DAYS and whose video no source
keeps in ``seen_video``. A younger row stays, so an apply of a rule does not add its video to the playlist again.
"""

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db import affected_rows
from youtube_automanager.db.models import AddedVideo, Run, RunAction, RunStatus, SeenVideo, Setting, utc_now

LOG = logging.getLogger(__name__)

RETENTION_KEY = "history_retention_days"
DEFAULT_RETENTION_DAYS = 90
MIN_RETENTION_DAYS = 1
# An ``added_video`` row whose video no source keeps stays this many days after its add. An apply of a rule reads the
# videos of up to MAX_DAYS_BACK days back, the limit of the rules page. The date of an old flat entry comes from text
# such as "1 year ago", which yt-dlp dates 364 days back, also for a video that is almost 2 years old. "2 years ago"
# gets 729 days back, out of the reach of an apply, so a row stays 2 years and 2 days.
ADDED_VIDEO_KEEP_DAYS = 732


@dataclass(frozen=True)
class CleanupResult:
    runs: int
    actions: int
    added_videos: int = 0


def _is_valid_days(value: object) -> bool:
    # A bool is an int in Python, but True is not a number of days.
    return isinstance(value, int) and not isinstance(value, bool) and value >= MIN_RETENTION_DAYS


def _old_run(days: int, now: datetime | None) -> ColumnElement[bool]:
    """The condition of a run that the cleanup deletes: it started more than ``days`` days before ``now``."""
    cutoff = (now or utc_now()) - timedelta(days=days)
    return (Run.started_at < cutoff) & (Run.status != RunStatus.RUNNING)


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
    old = _old_run(days, now)
    with sessions.begin() as session:
        actions = session.execute(delete(RunAction).where(RunAction.run_id.in_(select(Run.id).where(old))))
        runs = session.execute(delete(Run).where(old))
        return CleanupResult(runs=affected_rows(runs), actions=affected_rows(actions))


def runs_lost_by_retention(sessions: sessionmaker[Session], days: int, now: datetime | None = None) -> int:
    """Return how many runs the next cleanup deletes after the retention changes to ``days``. This call blocks.

    A value that is not smaller than the stored one deletes nothing that the stored one keeps, so it gives 0.
    """
    if days >= get_retention_days(sessions):
        return 0
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(Run).where(_old_run(days, now))) or 0


def delete_old_added_videos(sessions: sessionmaker[Session], now: datetime | None = None) -> int:
    """Delete each ``added_video`` row that is older than ADDED_VIDEO_KEEP_DAYS days before ``now`` and whose video no
    source keeps in ``seen_video``. Return the number of deleted rows. This call blocks.

    A row whose video a source keeps stays, however old it is, because a sync run can still reach that video.
    """
    cutoff = (now or utc_now()) - timedelta(days=ADDED_VIDEO_KEEP_DAYS)
    kept = select(SeenVideo.video_id).where(SeenVideo.video_id == AddedVideo.video_id).exists()
    with sessions.begin() as session:
        return affected_rows(session.execute(delete(AddedVideo).where(AddedVideo.added_at < cutoff, ~kept)))


def clean_history(sessions: sessionmaker[Session]) -> CleanupResult:
    """Delete the runs that are older than the stored retention period, and the old ``added_video`` rows that no source
    keeps. The scheduler calls this each day."""
    days = get_retention_days(sessions)
    result = delete_old_runs(sessions, days)
    if result.runs:
        LOG.info(
            "Deleted %s run(s) and %s action(s) that started more than %s day(s) ago", result.runs, result.actions, days
        )
    added_videos = delete_old_added_videos(sessions)
    if added_videos:
        LOG.info(
            "Deleted %s added video record(s) that are older than %s day(s) and whose video no source keeps",
            added_videos,
            ADDED_VIDEO_KEEP_DAYS,
        )
    return replace(result, added_videos=added_videos)
