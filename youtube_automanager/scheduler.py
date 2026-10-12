"""The schedule of the sync runs: the async interface of the service over APScheduler.

The rest of the service calls APScheduler only through this module, so a move to another major version of APScheduler
changes only this module. The schedule lives in the ``schedule`` table. APScheduler keeps its one job in memory, and
a change of the schedule acts at once, without a restart.
"""

import asyncio
import enum
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import DEFAULT_CRON_EXPRESSION, RunTrigger, Schedule, ScheduleKind
from youtube_automanager.runner import SyncRunner

LOG = logging.getLogger(__name__)

JOB_ID = "sync"

# Standard cron counts the days of the week from Sunday, and 7 is Sunday again. APScheduler 3 counts 0 as Monday.
# A name means the same day in both, so the numbers become names before APScheduler sees them.
_CRON_WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_WEEKDAY_NUMBERS = re.compile(r"(\*|\d+)(?:-(\d+))?(?:/(\d+))?")


class SchedulerState(enum.StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


class RunNowOutcome(enum.StrEnum):
    STARTED = "started"
    ALREADY_ACTIVE = "already_active"


@dataclass(frozen=True)
class RunNowResult:
    outcome: RunNowOutcome
    run_id: int | None = None


@dataclass(frozen=True)
class ScheduleSpec:
    """A schedule of the runs. The constructor refuses a schedule that APScheduler cannot follow."""

    kind: ScheduleKind
    timezone: str
    cron_expression: str | None = None
    interval_s: int | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        build_trigger(self)

    @classmethod
    def from_row(cls, row: Schedule) -> ScheduleSpec:
        return cls(
            kind=row.kind,
            timezone=row.timezone,
            cron_expression=row.cron_expression,
            interval_s=row.interval_s,
            enabled=row.enabled,
        )


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"Unknown time zone {name!r}") from error


def _weekday_names(field: str) -> str:
    """Translate the numbers of a standard cron day-of-week field into day names. Names stay as they are."""
    parts = []
    for part in field.split(","):
        match = _WEEKDAY_NUMBERS.fullmatch(part)
        if match is None:
            parts.append(part)
            continue
        first_text, last_text, step_text = match.groups()
        if first_text == "*":
            if last_text is not None:
                raise ValueError(f"Wrong day of the week {part!r}")
            if step_text is None:
                parts.append(part)
                continue
            first, last = 0, 6
        else:
            first = int(first_text)
            # "5/2" means from 5 to the end of the week, as in APScheduler.
            last = int(last_text) if last_text is not None else (6 if step_text is not None else first)
        step = int(step_text) if step_text is not None else 1
        if not (0 <= first <= last <= 7) or step < 1:
            raise ValueError(f"Wrong day of the week {part!r}: use 0 to 7, where 0 and 7 are Sunday")
        days = sorted({day % 7 for day in range(first, last + 1, step)})
        parts.extend(_CRON_WEEKDAYS[day] for day in days)
    return ",".join(parts)


def _cron_trigger(expression: str, zone: ZoneInfo) -> BaseTrigger:
    """Build a trigger that fires as standard cron fires for ``expression``.

    When both the day of the month and the day of the week are set, standard cron fires on a day that matches either
    of them, but APScheduler 3 fires only on a day that matches both. Such an expression becomes two triggers.
    """
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(f"A cron expression has 5 fields, got {len(fields)}: {expression!r}")
    minute, hour, day, month, day_of_week = fields
    day_of_week = _weekday_names(day_of_week)
    if not day.startswith("*") and not day_of_week.startswith("*"):
        return OrTrigger(
            [
                CronTrigger(minute=minute, hour=hour, day=day, month=month, timezone=zone),
                CronTrigger(minute=minute, hour=hour, month=month, day_of_week=day_of_week, timezone=zone),
            ]
        )
    return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week, timezone=zone)


def build_trigger(spec: ScheduleSpec) -> BaseTrigger:
    """Return the APScheduler trigger of ``spec``. Raise ValueError when the schedule is wrong."""
    zone = _zone(spec.timezone)
    if spec.kind is ScheduleKind.CRON:
        if not spec.cron_expression:
            raise ValueError("A cron schedule needs a cron expression")
        return _cron_trigger(spec.cron_expression, zone)
    if spec.kind is ScheduleKind.INTERVAL:
        if spec.interval_s is None or spec.interval_s < 1:
            raise ValueError(f"An interval schedule needs an interval of 1 s or more, got {spec.interval_s}")
        return IntervalTrigger(seconds=spec.interval_s, timezone=zone)
    raise ValueError(f"Unknown schedule kind {spec.kind!r}")


def _following_times(trigger: BaseTrigger, first: datetime, count: int) -> list[datetime]:
    times = [first]
    while len(times) < count:
        following = trigger.get_next_fire_time(times[-1], times[-1])
        if following is None:
            break
        times.append(following)
    return times


def upcoming_run_times(spec: ScheduleSpec, count: int, now: datetime | None = None) -> list[datetime]:
    """Return the next ``count`` run times of ``spec`` after ``now``, for example to show a schedule before a save.

    An interval schedule counts from ``now``. A disabled schedule has no run times.
    """
    if not spec.enabled or count < 1:
        return []
    now = now or datetime.now(UTC)
    trigger = (
        IntervalTrigger(seconds=spec.interval_s, start_date=now, timezone=_zone(spec.timezone))
        if spec.kind is ScheduleKind.INTERVAL
        else build_trigger(spec)
    )
    first = trigger.get_next_fire_time(None, now)
    if first is not None and first <= now:
        first = trigger.get_next_fire_time(first, now)
    return [] if first is None else _following_times(trigger, first, count)


def load_schedule(sessions: sessionmaker[Session], default_timezone: str) -> ScheduleSpec:
    """Return the schedule from the single row of the ``schedule`` table.

    When the table is empty, first write the default row: enabled, cron ``0 18 * * *``, and ``default_timezone``.
    """
    with sessions.begin() as session:
        row = session.get(Schedule, 1)
        if row is None:
            row = Schedule(
                id=1,
                enabled=True,
                kind=ScheduleKind.CRON,
                cron_expression=DEFAULT_CRON_EXPRESSION,
                interval_s=None,
                timezone=default_timezone,
            )
            session.add(row)
            LOG.info("Wrote the default schedule: cron %r in %s", DEFAULT_CRON_EXPRESSION, default_timezone)
        return ScheduleSpec.from_row(row)


class Scheduler:
    """Starts ``SyncRunner`` runs by a schedule and on request.

    Each method runs on the event loop of the service. A pause or a new schedule acts only in this process: the caller
    keeps the ``enabled`` flag and the schedule in the ``schedule`` table, so they survive a restart.
    """

    def __init__(self, runner: SyncRunner):
        self._runner = runner
        self._scheduler: AsyncIOScheduler | None = None

    def _running_scheduler(self) -> AsyncIOScheduler:
        if self._scheduler is None:
            raise RuntimeError("The scheduler is not running")
        return self._scheduler

    async def start(self, spec: ScheduleSpec) -> None:
        if self._scheduler is not None:
            raise RuntimeError("The scheduler is already running")
        trigger = build_trigger(spec)
        scheduler = AsyncIOScheduler(timezone=UTC)
        scheduler.start()
        # A synchronous job runs in the thread pool of the event loop. Without a misfire grace time, a run that starts
        # late, for example after a busy event loop, still starts. A missed run is never queued twice.
        options = {} if spec.enabled else {"next_run_time": None}
        scheduler.add_job(
            self._runner.run_scheduled,
            trigger=trigger,
            id=JOB_ID,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,
            **options,
        )
        self._scheduler = scheduler
        LOG.info("The scheduler started: %s", self._describe(spec))

    async def shutdown(self) -> None:
        """Stop the schedule. An active run goes on: ``SyncRunner.stop`` stops it."""
        scheduler = self._scheduler
        if scheduler is None:
            return
        self._scheduler = None
        # AsyncIOScheduler cannot wait for a job in a thread, and it shuts down on the next turn of the event loop.
        scheduler.shutdown(wait=False)
        await asyncio.sleep(0)
        LOG.info("The scheduler stopped")

    async def apply(self, spec: ScheduleSpec) -> None:
        """Replace the schedule. A disabled ``spec`` pauses the schedule, and an enabled one resumes it."""
        scheduler = self._running_scheduler()
        trigger = build_trigger(spec)
        if spec.enabled:
            scheduler.reschedule_job(JOB_ID, trigger=trigger)
        else:
            # reschedule_job sets a next run time, so it would also resume a paused job.
            scheduler.modify_job(JOB_ID, trigger=trigger, next_run_time=None)
        LOG.info("The schedule changed: %s", self._describe(spec))

    async def pause(self) -> None:
        self._running_scheduler().pause_job(JOB_ID)
        LOG.info("The schedule is paused")

    async def resume(self) -> None:
        scheduler = self._running_scheduler()
        if scheduler.get_job(JOB_ID).next_run_time is None:
            scheduler.resume_job(JOB_ID)
        LOG.info("The schedule is resumed")

    async def run_now(self) -> RunNowResult:
        """Start a run now, also while the schedule is paused. Start nothing when a run is already active."""
        scheduler = self._running_scheduler()
        run_id = await asyncio.to_thread(self._runner.claim, RunTrigger.MANUAL)
        if run_id is None:
            return RunNowResult(RunNowOutcome.ALREADY_ACTIVE)
        try:
            # A run that never starts would keep the sync lock, so this job has no misfire grace time either.
            scheduler.add_job(self._runner.execute, args=(run_id,), id=f"manual-{run_id}", misfire_grace_time=None)
        except Exception as error:
            await asyncio.to_thread(self._runner.abandon, run_id, f"The service cannot start the run: {error}")
            raise
        return RunNowResult(RunNowOutcome.STARTED, run_id)

    async def next_run_times(self, count: int) -> list[datetime]:
        """Return the next ``count`` run times of the schedule. A paused or stopped schedule has none."""
        if self._scheduler is None or count < 1:
            return []
        job = self._scheduler.get_job(JOB_ID)
        if job is None or job.next_run_time is None:
            return []
        return _following_times(job.trigger, job.next_run_time, count)

    async def state(self) -> SchedulerState:
        if self._scheduler is None:
            return SchedulerState.STOPPED
        job = self._scheduler.get_job(JOB_ID)
        if job is None or job.next_run_time is None:
            return SchedulerState.PAUSED
        return SchedulerState.RUNNING

    @staticmethod
    def _describe(spec: ScheduleSpec) -> str:
        when = f"cron {spec.cron_expression!r}" if spec.kind is ScheduleKind.CRON else f"every {spec.interval_s} s"
        return f"{when} in {spec.timezone}, {'enabled' if spec.enabled else 'paused'}"
