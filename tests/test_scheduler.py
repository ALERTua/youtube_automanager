import asyncio
import itertools
import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from youtube_automanager import scheduler as scheduler_module
from youtube_automanager.db.models import Run, RunStatus, RunTrigger, Schedule, ScheduleKind
from youtube_automanager.runner import SyncRunner
from youtube_automanager.scheduler import (
    ROTATION_JOB_ID,
    RunNowOutcome,
    Scheduler,
    SchedulerState,
    ScheduleSpec,
    load_schedule,
    upcoming_run_times,
)

WAIT_SECONDS = 10
KYIV = ZoneInfo("Europe/Kyiv")
# Monday, 12 October 2026, 13:00 in Kyiv.
NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)


def cron(expression, timezone="Europe/Kyiv", enabled=True):
    return ScheduleSpec(kind=ScheduleKind.CRON, cron_expression=expression, timezone=timezone, enabled=enabled)


def interval(seconds, timezone="UTC", enabled=True):
    return ScheduleSpec(kind=ScheduleKind.INTERVAL, interval_s=seconds, timezone=timezone, enabled=enabled)


def local(times):
    return [time.astimezone(KYIV) for time in times]


# The schedule specification and its run times.


def test_default_cron_runs_each_day_at_18_in_its_time_zone():
    times = local(upcoming_run_times(cron("0 18 * * *"), 3, now=NOW))

    assert times == [datetime(2026, 10, day, 18, 0, tzinfo=KYIV) for day in (12, 13, 14)]


def test_run_times_are_strictly_after_now():
    at_a_run_time = datetime(2026, 10, 12, 18, 0, tzinfo=KYIV)

    (first,) = upcoming_run_times(cron("0 18 * * *"), 1, now=at_a_run_time)

    assert first == datetime(2026, 10, 13, 18, 0, tzinfo=KYIV)


def test_cron_keeps_the_local_hour_across_a_change_of_summer_time():
    # Kyiv leaves summer time on 25 October 2026.
    times = local(upcoming_run_times(cron("0 18 * * *"), 15, now=NOW))

    assert {time.hour for time in times} == {18}
    assert {time.utcoffset() for time in times} == {timedelta(hours=3), timedelta(hours=2)}


@pytest.mark.parametrize(
    ("day_of_week", "weekdays"),
    [
        # Standard cron: 0 and 7 are Sunday, 1 is Monday. Python: Monday is 0, Sunday is 6.
        ("1", {0}),
        ("0", {6}),
        ("7", {6}),
        ("1-5", {0, 1, 2, 3, 4}),
        ("*/2", {6, 1, 3, 5}),
        ("1-5/2", {0, 2, 4}),
        ("5/1", {4, 5}),
        ("0,6", {5, 6}),
        ("6-7", {5, 6}),
        ("mon-fri", {0, 1, 2, 3, 4}),
        ("sat,sun", {5, 6}),
    ],
)
def test_day_of_week_follows_standard_cron(day_of_week, weekdays):
    times = upcoming_run_times(cron(f"0 9 * * {day_of_week}"), 21, now=NOW)

    assert {time.astimezone(KYIV).weekday() for time in times} == weekdays


def test_day_of_month_and_day_of_week_together_match_either_day():
    times = local(upcoming_run_times(cron("0 9 13 * 5"), 10, now=NOW))

    assert all(time.day == 13 or time.weekday() == 4 for time in times)
    assert any(time.day == 13 and time.weekday() != 4 for time in times)
    assert any(time.weekday() == 4 and time.day != 13 for time in times)
    assert times == sorted(times)


def test_interval_counts_from_now():
    assert upcoming_run_times(interval(600), 3, now=NOW) == [NOW + timedelta(seconds=600 * n) for n in (1, 2, 3)]


def test_disabled_schedule_has_no_run_times():
    assert upcoming_run_times(cron("0 18 * * *", enabled=False), 3, now=NOW) == []


@pytest.mark.parametrize(
    ("make_spec", "message"),
    [
        (lambda: cron("0 18 * *"), "5 fields"),
        (lambda: cron("0 18 * * * *"), "5 fields"),
        (lambda: cron("61 18 * * *"), "maximum"),
        (lambda: cron("0 18 * * 8"), "day of the week"),
        (lambda: cron("0 18 * * 5-1"), "day of the week"),
        (lambda: cron("0 18 * * */0"), "day of the week"),
        (lambda: cron("0 18 * * *-3"), "day of the week"),
        (lambda: cron(""), "cron expression"),
        (lambda: cron("0 18 * * *", timezone="Nowhere/City"), "Unknown time zone"),
        (lambda: interval(0), "1 s or more"),
        (lambda: ScheduleSpec(kind=ScheduleKind.INTERVAL, timezone="UTC"), "1 s or more"),
    ],
)
def test_wrong_schedule_is_refused(make_spec, message):
    with pytest.raises(ValueError, match=message):
        make_spec()


def test_load_schedule_writes_the_default_row_once(db_sessions):
    spec = load_schedule(db_sessions, "Europe/Kyiv")

    assert spec == cron("0 18 * * *")
    with db_sessions.begin() as session:
        (row,) = session.scalars(select(Schedule)).all()
        assert (row.id, row.enabled, row.kind, row.timezone) == (1, True, ScheduleKind.CRON, "Europe/Kyiv")
        row.kind = ScheduleKind.INTERVAL
        row.interval_s = 600
        row.enabled = False

    assert load_schedule(db_sessions, "UTC") == ScheduleSpec(
        kind=ScheduleKind.INTERVAL,
        timezone="Europe/Kyiv",
        cron_expression="0 18 * * *",
        interval_s=600,
        enabled=False,
    )


# The scheduler on a running event loop.


@pytest.fixture
def runner(db_sessions, db_url, tmp_path, fake_starter):
    return SyncRunner(
        db_sessions,
        ytdlp_dir=tmp_path / "yt-dlp",
        database_url=db_url,
        child_timeout_s=WAIT_SECONDS,
        start=fake_starter,
        lock=threading.Lock(),
    )


def run_with_scheduler(runner, spec, body):
    """Start a scheduler with ``spec`` on a new event loop, await ``body(scheduler)``, and shut the scheduler down."""

    async def main():
        scheduler = Scheduler(runner)
        await scheduler.start(spec)
        try:
            return await body(scheduler)
        finally:
            await asyncio.to_thread(runner.stop, WAIT_SECONDS)
            await scheduler.shutdown()

    return asyncio.run(main())


async def wait_until(condition, what):
    deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if result := condition():
            return result
        await asyncio.sleep(0.02)
    raise AssertionError(f"{what} did not happen within {WAIT_SECONDS} s")


async def wait_for_run(sessions, run_id, status):
    def finished_run():
        with sessions() as session:
            run = session.get(Run, run_id)
            return run if run is not None and run.status is status else None

    return await wait_until(finished_run, f"Run {run_id} with status {status}")


def test_enabled_schedule_runs_and_shows_its_next_run_times(runner):
    async def body(scheduler):
        return await scheduler.state(), await scheduler.next_run_times(3)

    state, times = run_with_scheduler(runner, cron("0 18 * * *"), body)

    assert state is SchedulerState.RUNNING
    assert len(times) == 3
    assert {time.astimezone(KYIV).hour for time in times} == {18}
    assert times[0] > datetime.now(UTC)
    assert [later - earlier for earlier, later in itertools.pairwise(times)] == [timedelta(days=1)] * 2


def test_disabled_schedule_starts_paused(runner):
    async def body(scheduler):
        return await scheduler.state(), await scheduler.next_run_times(3)

    assert run_with_scheduler(runner, cron("0 18 * * *", enabled=False), body) == (SchedulerState.PAUSED, [])


def test_pause_and_resume(runner):
    async def body(scheduler):
        await scheduler.pause()
        paused = await scheduler.state(), await scheduler.next_run_times(1)
        await scheduler.resume()
        await scheduler.resume()
        return paused, await scheduler.state(), await scheduler.next_run_times(1)

    paused, state, times = run_with_scheduler(runner, cron("0 18 * * *"), body)

    assert paused == (SchedulerState.PAUSED, [])
    assert state is SchedulerState.RUNNING
    assert len(times) == 1


def test_apply_changes_the_schedule_without_a_restart(runner):
    async def body(scheduler):
        before = datetime.now(UTC)
        await scheduler.apply(interval(600))
        return before, await scheduler.state(), await scheduler.next_run_times(2)

    before, state, times = run_with_scheduler(runner, cron("0 18 * * *"), body)

    assert state is SchedulerState.RUNNING
    assert before + timedelta(seconds=600) <= times[0] <= datetime.now(UTC) + timedelta(seconds=600)
    assert times[1] - times[0] == timedelta(seconds=600)


def test_apply_of_a_disabled_schedule_keeps_the_scheduler_paused(runner):
    async def body(scheduler):
        await scheduler.apply(interval(600, enabled=False))
        first = await scheduler.state()
        # A second change while paused must not resume the schedule.
        await scheduler.apply(interval(300, enabled=False))
        second = await scheduler.state(), await scheduler.next_run_times(1)
        await scheduler.resume()
        return first, second, await scheduler.next_run_times(2)

    first, second, times = run_with_scheduler(runner, cron("0 18 * * *"), body)

    assert first is SchedulerState.PAUSED
    assert second == (SchedulerState.PAUSED, [])
    assert times[1] - times[0] == timedelta(seconds=300)


def test_apply_of_an_enabled_schedule_resumes_a_paused_scheduler(runner):
    async def body(scheduler):
        await scheduler.apply(interval(300))
        return await scheduler.state()

    assert run_with_scheduler(runner, cron("0 18 * * *", enabled=False), body) is SchedulerState.RUNNING


def test_run_now_starts_a_manual_run(runner, db_sessions):
    async def body(scheduler):
        result = await scheduler.run_now()
        run = await wait_for_run(db_sessions, result.run_id, RunStatus.SUCCEEDED)
        return result, run

    result, run = run_with_scheduler(runner, cron("0 18 * * *", enabled=False), body)

    assert result.outcome is RunNowOutcome.STARTED
    assert run.trigger is RunTrigger.MANUAL


def test_run_now_while_a_run_is_active_starts_nothing(runner, fake_starter, db_sessions):
    fake_starter.plan.append({"blocking": True})

    async def body(scheduler):
        first = await scheduler.run_now()
        assert await asyncio.to_thread(fake_starter.child_started.wait, WAIT_SECONDS)
        second = await scheduler.run_now()
        fake_starter.children[0].finish(0)
        await wait_for_run(db_sessions, first.run_id, RunStatus.SUCCEEDED)
        await wait_until(lambda: not runner.is_active(), "The release of the sync lock")
        third = await scheduler.run_now()
        await wait_for_run(db_sessions, third.run_id, RunStatus.SUCCEEDED)
        return first, second, third

    first, second, third = run_with_scheduler(runner, cron("0 18 * * *"), body)

    assert first.outcome is RunNowOutcome.STARTED
    assert (second.outcome, second.run_id) == (RunNowOutcome.ALREADY_ACTIVE, None)
    assert third.outcome is RunNowOutcome.STARTED
    with db_sessions() as session:
        assert session.scalars(select(Run.id).order_by(Run.id)).all() == [first.run_id, third.run_id]


def test_shutdown_stops_the_schedule(runner):
    async def main():
        scheduler = Scheduler(runner)
        await scheduler.start(cron("0 18 * * *"))
        await scheduler.shutdown()
        await scheduler.shutdown()
        state = await scheduler.state(), await scheduler.next_run_times(1)
        with pytest.raises(RuntimeError, match="not running"):
            await scheduler.run_now()
        with pytest.raises(RuntimeError, match="not running"):
            await scheduler.pause()
        return state

    assert asyncio.run(main()) == (SchedulerState.STOPPED, [])


def test_second_start_is_refused(runner):
    async def body(scheduler):
        with pytest.raises(RuntimeError, match="already running"):
            await scheduler.start(cron("0 18 * * *"))
        return await scheduler.state()

    assert run_with_scheduler(runner, cron("0 18 * * *"), body) is SchedulerState.RUNNING


# The cookie rotation


class FakeRotation:
    """Counts the turns. Each turn returns the next delay, or raises it when it is an exception."""

    def __init__(self, delays):
        self.delays = list(delays)
        self.turns = 0

    def turn(self):
        self.turns += 1
        delay = self.delays.pop(0) if self.delays else 3600.0
        if isinstance(delay, Exception):
            raise delay
        return delay


def test_each_turn_of_the_rotation_plans_the_next_one_by_its_delay(runner):
    rotation = FakeRotation([0.05, 0.05, 3600.0])

    async def body(scheduler):
        await scheduler.start_rotation(rotation, first_delay_s=0.01)
        await wait_until(lambda: rotation.turns == 3, "Three turns of the rotation")
        await asyncio.sleep(0.3)
        return rotation.turns, scheduler._scheduler.get_job(ROTATION_JOB_ID).next_run_time

    turns, next_turn = run_with_scheduler(runner, cron("0 18 * * *"), body)

    assert turns == 3
    assert next_turn > datetime.now(UTC) + timedelta(seconds=3000)


def test_a_turn_that_asks_for_the_next_turn_at_once_still_gets_it(runner):
    # APScheduler still counts the turn that plans the next one as running when the next one is due.
    rotation = FakeRotation([0.0, 0.0, 3600.0])

    async def body(scheduler):
        await scheduler.start_rotation(rotation, first_delay_s=0.0)
        await wait_until(lambda: rotation.turns == 3, "Three turns of the rotation")
        return rotation.turns

    assert run_with_scheduler(runner, cron("0 18 * * *"), body) == 3


def test_a_failed_turn_of_the_rotation_still_plans_the_next_turn(runner, monkeypatch, caplog):
    monkeypatch.setattr(scheduler_module, "ROTATION_DELAY_S", 0.01)
    rotation = FakeRotation([RuntimeError("a bug in the turn"), 3600.0])

    async def body(scheduler):
        await scheduler.start_rotation(rotation, first_delay_s=0.01)
        await wait_until(lambda: rotation.turns == 2, "The turn after the failed turn")
        return rotation.turns

    assert run_with_scheduler(runner, cron("0 18 * * *"), body) == 2
    assert "A turn of the cookie rotation failed" in caplog.text


def test_the_rotation_goes_on_while_the_schedule_is_paused(runner):
    rotation = FakeRotation([0.05, 3600.0])

    async def body(scheduler):
        await scheduler.pause()
        await scheduler.start_rotation(rotation, first_delay_s=0.01)
        await wait_until(lambda: rotation.turns == 2, "Two turns of the rotation")
        return await scheduler.state()

    assert run_with_scheduler(runner, cron("0 18 * * *"), body) is SchedulerState.PAUSED


def test_the_rotation_needs_a_running_scheduler(runner):
    with pytest.raises(RuntimeError, match="not running"):
        asyncio.run(Scheduler(runner).start_rotation(FakeRotation([])))
