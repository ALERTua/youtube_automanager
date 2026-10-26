"""The Dashboard, the History and the Scheduler page follow a run while it runs, with no click and no wait for a timer.

A blocking fake child keeps a run active. The test writes the counts and the actions into the database, as the child of
a run does, and the events of the runs bring them to the pages.
"""

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest
from nicegui import ElementFilter, ui
from sqlalchemy import update
from ui_support import (
    PatientUser,
    client_of,
    element_of,
    elements_of,
    eventually,
    service_with_pages,
    set_value,
    store,
    text_of,
)

from youtube_automanager.db.models import Run, RunAction, RunActionKind, RunStatus, RunTrigger, Schedule
from youtube_automanager.run_events import ActionItem
from youtube_automanager.web.ui import dashboard, history_page, scheduler_page
from youtube_automanager.web.ui.live_runs import RunFollower

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]

WAIT_SECONDS = 10
# A short poll of the progress, so a test sees a change soon.
POLL_SECONDS = 0.05
# How long a test waits for an event that must change nothing.
SETTLE_SECONDS = 0.3
BASE = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def live(nicegui_globals, settings, fake_starter):
    async with service_with_pages(settings, fake_starter) as (user, services):
        services.run_events._poll_seconds = POLL_SECONDS
        yield user, services


async def start_run(services, fake_starter) -> int:
    """Start a manual run whose child runs until ``end_run``."""
    fake_starter.plan.append({"blocking": True})
    result = await services.scheduler.run_now()
    assert await asyncio.to_thread(fake_starter.child_started.wait, WAIT_SECONDS)
    return result.run_id


def end_run(fake_starter, exit_code=0) -> None:
    fake_starter.children[-1].finish(exit_code)


def child_writes(sessions, run_id, added, seen, *actions) -> None:
    """Commit the counts and the actions of a run, as its child does."""
    with sessions.begin() as session:
        session.add_all(
            RunAction(run_id=run_id, action=kind, video_id=video_id, playlist_id="WL", rule_id=1)
            for kind, video_id in actions
        )
        session.execute(update(Run).where(Run.id == run_id).values(added_count=added, seen_count=seen))


def finished_run(number: int) -> Run:
    return Run(
        id=number,
        status=RunStatus.SUCCEEDED,
        trigger=RunTrigger.SCHEDULE,
        started_at=BASE + timedelta(minutes=number),
        finished_at=BASE + timedelta(minutes=number, seconds=30),
    )


def item(number: int, kind=RunActionKind.ADDED, video_id="late") -> ActionItem:
    return ActionItem(number, BASE, kind, video_id, None, 1, "WL", None)


def counts_on_dashboard(user) -> tuple[str, str]:
    return text_of(user, "run-added"), text_of(user, "run-seen")


def runs_of(user) -> list[dict]:
    tables = elements_of(user, "runs-table")
    return tables[0].rows if tables else []


def run_ids(user) -> list[int]:
    return [row["id"] for row in runs_of(user)]


def row_of(user, run_id) -> dict:
    (row,) = [row for row in runs_of(user) if row["id"] == run_id]
    return row


def videos_of(user) -> list[str]:
    tables = elements_of(user, "actions-table")
    return [row["video"] for row in tables[0].rows] if tables else []


def followers_of(user) -> list[RunFollower]:
    with client_of(user):
        return list(ElementFilter(kind=RunFollower, local_scope=False))


def subscribers(services) -> list[int]:
    events = services.run_events
    return [len(events.started.callbacks), len(events.progress.callbacks), len(events.finished.callbacks)]


async def test_the_dashboard_shows_the_start_the_counts_and_the_end_of_a_run(live, sessions, fake_starter):
    user, services = live
    await user.open("/")
    await user.should_see("No run yet.")

    run_id = await start_run(services, fake_starter)
    await eventually(lambda: text_of(user, "run-status") == "In progress", "The start of the run")
    assert text_of(user, "run-trigger") == "Manual"
    assert counts_on_dashboard(user) == ("0", "0")

    child_writes(sessions, run_id, 1, 4, (RunActionKind.ADDED, "vid1"))
    await eventually(lambda: counts_on_dashboard(user) == ("1", "4"), "The first counts")
    child_writes(sessions, run_id, 2, 9, (RunActionKind.ADDED, "vid2"))
    await eventually(lambda: counts_on_dashboard(user) == ("2", "9"), "The second counts")

    end_run(fake_starter)
    await eventually(lambda: text_of(user, "run-status") == "Succeeded", "The end of the run")
    assert counts_on_dashboard(user) == ("2", "9")


async def test_the_dashboard_shows_the_status_of_the_row_and_keeps_it_after_a_late_progress(
    live, sessions, fake_starter
):
    user, services = live
    await user.open("/")
    run_id = await start_run(services, fake_starter)
    child_writes(sessions, run_id, 3, 5)
    await eventually(lambda: counts_on_dashboard(user) == ("3", "5"), "The counts")

    end_run(fake_starter, exit_code=1)
    await eventually(lambda: text_of(user, "run-status") == "Failed", "The end of the run")
    # A progress that NiceGUI runs after the end of the same run.
    services.run_events.progress.emit(run_id, 99, 99, ())
    await asyncio.sleep(SETTLE_SECONDS)

    assert (text_of(user, "run-status"), *counts_on_dashboard(user)) == ("Failed", "3", "5")


async def test_history_shows_the_new_run_at_the_top_with_its_counts_and_its_end(live, sessions, fake_starter):
    user, services = live
    store(sessions, finished_run(1), finished_run(2))
    await user.open("/history")
    assert run_ids(user) == [2, 1]

    run_id = await start_run(services, fake_starter)
    await eventually(lambda: run_ids(user) == [run_id, 2, 1], "The row of the new run at the top")
    assert (row_of(user, run_id)["status"], row_of(user, run_id)["finished"]) == ("In progress", "")
    assert text_of(user, "runs-summary") == "Runs 1-3 of 3"

    child_writes(sessions, run_id, 1, 6, (RunActionKind.ADDED, "vid1"))
    await eventually(lambda: (row_of(user, run_id)["added"], row_of(user, run_id)["seen"]) == (1, 6), "The counts")

    end_run(fake_starter)
    await eventually(lambda: row_of(user, run_id)["status"] == "Succeeded", "The status of the end")
    assert row_of(user, run_id)["finished"]
    assert (row_of(user, run_id)["added"], row_of(user, run_id)["seen"]) == (1, 6)


async def test_history_shows_no_new_run_that_its_filters_exclude(live, sessions, fake_starter):
    user, services = live
    store(sessions, finished_run(1))
    await user.open("/history")
    set_value(user, "filter-trigger", RunTrigger.SCHEDULE.value)
    await eventually(lambda: run_ids(user) == [1], "The filtered table")

    await start_run(services, fake_starter)
    await asyncio.sleep(SETTLE_SECONDS)
    end_run(fake_starter)
    await eventually(lambda: services.run_events._polls == {}, "The end of the run")
    await asyncio.sleep(SETTLE_SECONDS)

    assert run_ids(user) == [1]


async def test_history_with_a_status_filter_shows_the_run_when_its_end_matches(live, sessions, fake_starter):
    user, services = live
    await user.open("/history")
    set_value(user, "filter-status", RunStatus.SUCCEEDED.value)
    await user.should_see("No run matches the filters.")

    run_id = await start_run(services, fake_starter)
    await asyncio.sleep(SETTLE_SECONDS)
    assert run_ids(user) == []

    end_run(fake_starter)
    await eventually(lambda: run_ids(user) == [run_id], "The row of the run after its end")
    assert row_of(user, run_id)["status"] == "Succeeded"


async def test_history_on_its_second_page_does_not_show_the_new_run(live, sessions, fake_starter):
    user, services = live
    store(sessions, *(finished_run(number) for number in range(1, 61)))
    await user.open("/history")
    set_value(user, "runs-pagination", 2)
    await eventually(lambda: run_ids(user) == list(range(10, 0, -1)), "The second page")

    run_id = await start_run(services, fake_starter)

    # The new run is on the first page, so each row of the second page moves one place down.
    await eventually(lambda: run_ids(user) == list(range(11, 0, -1)), "The second page after the start")
    assert run_id not in run_ids(user)
    end_run(fake_starter)


async def test_the_open_actions_of_a_running_run_get_its_new_actions(live, sessions, fake_starter):
    user, services = live
    await user.open("/history")
    run_id = await start_run(services, fake_starter)
    await eventually(lambda: run_ids(user) == [run_id], "The row of the new run")
    user.find(marker="runs-table").trigger("rowClick", args=run_id)
    await user.should_see("This run has no actions.")

    child_writes(sessions, run_id, 1, 1, (RunActionKind.ADDED, "vid1"))
    await eventually(lambda: videos_of(user) == ["vid1"], "The first action")
    child_writes(sessions, run_id, 2, 3, (RunActionKind.SKIPPED, "vid2"), (RunActionKind.ADDED, "vid3"))
    await eventually(lambda: videos_of(user) == ["vid1", "vid2", "vid3"], "The next actions")

    set_value(user, "filter-action", RunActionKind.ADDED.value)
    await eventually(lambda: videos_of(user) == ["vid1", "vid3"], "The added actions")
    child_writes(sessions, run_id, 3, 5, (RunActionKind.ERROR, "vid4"), (RunActionKind.ADDED, "vid5"))
    await eventually(lambda: videos_of(user) == ["vid1", "vid3", "vid5"], "The new added action only")

    end_run(fake_starter)
    await eventually(lambda: text_of(user, "details-status") == "Succeeded", "The status of the end in the panel")
    assert videos_of(user) == ["vid1", "vid3", "vid5"]


async def test_a_late_progress_changes_no_row_and_no_action_of_an_ended_run(live, sessions, fake_starter):
    user, services = live
    await user.open("/history")
    run_id = await start_run(services, fake_starter)
    child_writes(sessions, run_id, 1, 2, (RunActionKind.ADDED, "vid1"))
    await eventually(lambda: run_ids(user) == [run_id], "The row of the new run")
    user.find(marker="runs-table").trigger("rowClick", args=run_id)
    await eventually(lambda: videos_of(user) == ["vid1"], "The action")
    end_run(fake_starter)
    await eventually(lambda: row_of(user, run_id)["status"] == "Succeeded", "The end")

    # A progress that NiceGUI runs after the end of the same run.
    services.run_events.progress.emit(run_id, 99, 99, (item(1000),))
    await asyncio.sleep(SETTLE_SECONDS)

    assert (row_of(user, run_id)["added"], row_of(user, run_id)["seen"]) == (1, 2)
    assert videos_of(user) == ["vid1"]


async def test_a_new_run_with_the_id_of_an_ended_run_shows_its_progress(live, sessions):
    """SQLite can give the id of a deleted newest run to the next run."""
    user, services = live
    store(sessions, Run(id=5, status=RunStatus.RUNNING, trigger=RunTrigger.MANUAL))
    await user.open("/history")
    events = services.run_events
    events.finished.emit(5, RunStatus.FAILED)
    events.started.emit(5, RunTrigger.SCHEDULE)

    events.progress.emit(5, 7, 8, ())

    await eventually(lambda: (row_of(user, 5)["added"], row_of(user, 5)["seen"]) == (7, 8), "The new counts")


async def test_a_read_that_ends_after_a_progress_keeps_the_newer_counts(live, sessions, fake_starter, monkeypatch):
    user, services = live
    await user.open("/history")
    run_id = await start_run(services, fake_starter)
    child_writes(sessions, run_id, 1, 1)
    await eventually(lambda: row_of(user, run_id)["seen"] == 1, "The first counts")
    reading = threading.Event()
    release = threading.Event()
    read_runs = history_page.read_runs

    def slow_read(*args):
        result = read_runs(*args)
        reading.set()
        release.wait(WAIT_SECONDS)
        return result

    monkeypatch.setattr(history_page, "read_runs", slow_read)
    user.find(marker="refresh").click()
    assert await asyncio.to_thread(reading.wait, WAIT_SECONDS)
    child_writes(sessions, run_id, 2, 5)
    await eventually(lambda: row_of(user, run_id)["seen"] == 5, "The counts of the progress")

    release.set()
    await asyncio.sleep(SETTLE_SECONDS)

    # The read holds the counts 1 and 1, from before the progress.
    assert (row_of(user, run_id)["added"], row_of(user, run_id)["seen"]) == (2, 5)
    monkeypatch.setattr(history_page, "read_runs", read_runs)
    end_run(fake_starter)
    await eventually(lambda: row_of(user, run_id)["status"] == "Succeeded", "The end")


async def test_the_scheduler_page_reads_its_status_at_the_start_and_at_the_end_of_a_run(live, sessions, fake_starter):
    user, services = live
    await user.open("/scheduler")
    assert text_of(user, "timezone") == "Europe/Kyiv"

    with sessions.begin() as session:
        session.get(Schedule, 1).timezone = "America/New_York"
    await start_run(services, fake_starter)
    await eventually(lambda: text_of(user, "timezone") == "America/New_York", "The status at the start")

    with sessions.begin() as session:
        session.get(Schedule, 1).timezone = "Asia/Tokyo"
    end_run(fake_starter)
    await eventually(lambda: text_of(user, "timezone") == "Asia/Tokyo", "The status at the end")


async def test_each_page_unsubscribes_when_its_client_or_its_follower_is_deleted(live, sessions, fake_starter):
    user, services = live
    other = PatientUser(user.http_client)
    assert subscribers(services) == [0, 0, 0]
    await user.open("/")
    await other.open("/history")
    assert subscribers(services) == [2, 2, 2]

    client_of(user).delete()

    assert subscribers(services) == [1, 1, 1]
    # The other page still follows the runs.
    run_id = await start_run(services, fake_starter)
    await eventually(lambda: run_ids(other) == [run_id], "The new run on the page that stayed")
    end_run(fake_starter)
    await eventually(lambda: row_of(other, run_id)["status"] == "Succeeded", "The end on the page that stayed")

    (follower,) = followers_of(other)
    # The follower takes no place on the page.
    assert not follower.visible
    with client_of(other):
        follower.delete()

    assert subscribers(services) == [0, 0, 0]


async def test_a_run_that_starts_during_the_first_read_of_the_dashboard_shows(live, fake_starter, monkeypatch):
    user, services = live
    reading = asyncio.Event()
    release = asyncio.Event()
    take_snapshot = dashboard.take_snapshot
    calls = []

    async def slow_first_snapshot(services):
        snapshot = await take_snapshot(services)
        calls.append(snapshot)
        if len(calls) == 1:
            reading.set()
            await release.wait()
        return snapshot

    monkeypatch.setattr(dashboard, "take_snapshot", slow_first_snapshot)
    opening = asyncio.create_task(user.open("/"))
    await asyncio.wait_for(reading.wait(), WAIT_SECONDS)
    # The first read saw no run, and the run starts before the page shows that read.
    assert calls[0].stored.last_run is None
    await start_run(services, fake_starter)
    await eventually(lambda: services.run_events._polls, "The start of the run")
    release.set()
    await asyncio.wait_for(opening, WAIT_SECONDS)

    await eventually(lambda: text_of(user, "run-status") == "In progress", "The run that started during the read")
    end_run(fake_starter)
    await eventually(lambda: text_of(user, "run-status") == "Succeeded", "The end of the run")


@pytest.mark.parametrize(("path", "module"), [("/", dashboard), ("/scheduler", scheduler_page)])
async def test_the_timer_of_the_page_reads_again_each_60_seconds(live, path, module):
    user, _services = live
    await user.open(path)

    with client_of(user):
        timers = list(ElementFilter(kind=ui.timer, local_scope=False))

    assert module.REFRESH_SECONDS == 60
    assert [timer.interval for timer in timers] == [60]
    assert [timer.active for timer in timers] == [True]


async def next_progress(services):
    """Return a task that ends at the next emit of ``progress``."""
    task = asyncio.create_task(asyncio.wait_for(services.run_events.progress.emitted(), WAIT_SECONDS))
    await asyncio.sleep(0)
    return task


async def open_running_run_with_one_action(user, services, sessions, fake_starter) -> int:
    await user.open("/history")
    run_id = await start_run(services, fake_starter)
    child_writes(sessions, run_id, 1, 1, (RunActionKind.ADDED, "vid0"))
    await eventually(lambda: run_ids(user) == [run_id] and row_of(user, run_id)["added"] == 1, "The row of the run")
    return run_id


async def test_an_action_that_comes_during_the_read_of_the_actions_shows_after_the_read(
    live, sessions, fake_starter, monkeypatch
):
    user, services = live
    run_id = await open_running_run_with_one_action(user, services, sessions, fake_starter)
    read_run_detail = history_page.read_run_detail
    reading = threading.Event()
    release = threading.Event()

    def slow_read(*args):
        result = read_run_detail(*args)
        reading.set()
        release.wait(WAIT_SECONDS)
        return result

    monkeypatch.setattr(history_page, "read_run_detail", slow_read)
    user.find(marker="runs-table").trigger("rowClick", args=run_id)
    assert await asyncio.to_thread(reading.wait, WAIT_SECONDS)
    progress = await next_progress(services)
    child_writes(sessions, run_id, 2, 2, (RunActionKind.ADDED, "vid1"))
    await progress
    monkeypatch.setattr(history_page, "read_run_detail", read_run_detail)
    release.set()

    # The read holds only vid0, and the poll gives vid1 only once, during the read.
    await eventually(lambda: videos_of(user) == ["vid0", "vid1"], "The action of the progress during the read")
    child_writes(sessions, run_id, 3, 3, (RunActionKind.ADDED, "vid2"))
    await eventually(lambda: videos_of(user) == ["vid0", "vid1", "vid2"], "The action of the next progress")
    end_run(fake_starter)
    await eventually(lambda: text_of(user, "details-status") == "Succeeded", "The end of the run")


async def test_a_progress_between_the_read_of_the_actions_and_its_display_shows(
    live, sessions, fake_starter, monkeypatch
):
    user, services = live
    run_id = await open_running_run_with_one_action(user, services, sessions, fake_starter)
    read_run_detail = history_page.read_run_detail
    loop = asyncio.get_running_loop()
    events = services.run_events
    late = (item(10_000, video_id="vidX"),)

    def read_then_emit(*args):
        result = read_run_detail(*args)
        # The emit comes in the turn of the loop that wakes the page after the read, before the page goes on.
        loop.call_soon_threadsafe(lambda: loop.call_soon(events.progress.emit, run_id, 2, 2, late))
        return result

    monkeypatch.setattr(history_page, "read_run_detail", read_then_emit)
    user.find(marker="runs-table").trigger("rowClick", args=run_id)
    await eventually(lambda: videos_of(user), "The panel of the run")
    await asyncio.sleep(SETTLE_SECONDS)

    assert videos_of(user) == ["vid0", "vidX"]
    monkeypatch.setattr(history_page, "read_run_detail", read_run_detail)
    end_run(fake_starter)
    await eventually(lambda: text_of(user, "details-status") == "Succeeded", "The end of the run")


async def test_the_dashboard_keeps_the_counts_of_a_progress_that_runs_after_the_end(
    live, sessions, fake_starter, monkeypatch
):
    user, services = live
    await user.open("/")
    run_id = await start_run(services, fake_starter)
    child_writes(sessions, run_id, 3, 5)
    await eventually(lambda: counts_on_dashboard(user) == ("3", "5"), "The counts")
    take_snapshot = dashboard.take_snapshot
    release = asyncio.Event()

    async def blocked_snapshot(services):
        snapshot = await take_snapshot(services)
        await release.wait()
        return snapshot

    monkeypatch.setattr(dashboard, "take_snapshot", blocked_snapshot)
    events = services.run_events
    # A poll step and the end of the run in one turn of the loop: the handler of the progress runs after the end.
    events.progress.emit(run_id, 4, 6, ())
    events.finished.emit(run_id, RunStatus.SUCCEEDED)
    await asyncio.sleep(SETTLE_SECONDS)

    # The read of the end has not ended yet, and the card shows the counts of the last progress.
    assert (text_of(user, "run-status"), *counts_on_dashboard(user)) == ("In progress", "4", "6")
    monkeypatch.setattr(dashboard, "take_snapshot", take_snapshot)
    release.set()
    end_run(fake_starter)


async def test_an_older_read_of_the_dashboard_does_not_replace_a_newer_one(live, fake_starter, monkeypatch):
    user, services = live
    await user.open("/")
    take_snapshot = dashboard.take_snapshot
    release = asyncio.Event()
    calls = []

    async def first_read_late(services):
        calls.append(1)
        number = len(calls)
        snapshot = await take_snapshot(services)
        if number == 1:
            await release.wait()
        return snapshot

    monkeypatch.setattr(dashboard, "take_snapshot", first_read_late)
    await start_run(services, fake_starter)
    await eventually(lambda: len(calls) == 1, "The read of the start")
    end_run(fake_starter)
    await eventually(lambda: text_of(user, "run-status") == "Succeeded", "The read of the end")

    release.set()
    await asyncio.sleep(SETTLE_SECONDS)

    # The read of the start saw the run in progress.
    assert text_of(user, "run-status") == "Succeeded"


async def test_a_progress_of_another_run_does_not_rebuild_the_last_run_card(live, sessions):
    user, services = live
    store(sessions, finished_run(1), finished_run(2))
    await user.open("/")
    (added,) = user.find(marker="run-added").elements

    services.run_events.progress.emit(1, 5, 5, ())
    await asyncio.sleep(SETTLE_SECONDS)

    assert not added.is_deleted


async def test_an_older_read_of_the_scheduler_page_does_not_replace_a_newer_one(
    live, sessions, fake_starter, monkeypatch
):
    user, services = live
    await user.open("/scheduler")
    take_status = scheduler_page.take_status
    release = asyncio.Event()
    calls = []

    async def first_read_late(services):
        calls.append(1)
        number = len(calls)
        status = await take_status(services)
        if number == 1:
            await release.wait()
        return status

    monkeypatch.setattr(scheduler_page, "take_status", first_read_late)
    with sessions.begin() as session:
        session.get(Schedule, 1).timezone = "America/New_York"
    await start_run(services, fake_starter)
    await eventually(lambda: len(calls) == 1, "The read of the start")
    with sessions.begin() as session:
        session.get(Schedule, 1).timezone = "Asia/Tokyo"
    end_run(fake_starter)
    await eventually(lambda: text_of(user, "timezone") == "Asia/Tokyo", "The read of the end")

    release.set()
    await asyncio.sleep(SETTLE_SECONDS)

    assert text_of(user, "timezone") == "Asia/Tokyo"


async def test_a_progress_sends_the_larger_counts_of_the_row_to_the_browser(live, sessions, monkeypatch):
    user, services = live
    store(sessions, Run(id=5, status=RunStatus.RUNNING, trigger=RunTrigger.MANUAL, added_count=5, seen_count=9))
    await user.open("/history")
    table = element_of(user, "runs-table")
    outbox = client_of(user).outbox
    sent = []
    enqueue_update = outbox.enqueue_update

    def record(element):
        sent.append(element)
        enqueue_update(element)

    monkeypatch.setattr(outbox, "enqueue_update", record)
    events = services.run_events

    events.progress.emit(5, 6, 10, ())
    await eventually(lambda: table in sent, "The update of the table")
    assert (row_of(user, 5)["added"], row_of(user, 5)["seen"]) == (6, 10)

    # A progress that is older than the counts of the row does not lower them.
    events.progress.emit(5, 1, 1, ())
    await asyncio.sleep(SETTLE_SECONDS)
    assert (row_of(user, 5)["added"], row_of(user, 5)["seen"]) == (6, 10)

    # A read again with larger counts than the last progress shows the counts of the read.
    with sessions.begin() as session:
        session.execute(update(Run).where(Run.id == 5).values(added_count=8, seen_count=12))
    user.find(marker="refresh").click()
    await eventually(lambda: table.is_deleted, "The new table of the read")
    assert (row_of(user, 5)["added"], row_of(user, 5)["seen"]) == (8, 12)


async def test_a_run_that_starts_during_the_first_read_of_history_shows(live, fake_starter, monkeypatch):
    user, services = live
    read_runs = history_page.read_runs
    reading = threading.Event()
    release = threading.Event()
    calls = []

    def slow_first_read(*args):
        result = read_runs(*args)
        calls.append(result)
        if len(calls) == 1:
            reading.set()
            release.wait(WAIT_SECONDS)
        return result

    monkeypatch.setattr(history_page, "read_runs", slow_first_read)
    opening = asyncio.create_task(user.open("/history"))
    assert await asyncio.to_thread(reading.wait, WAIT_SECONDS)
    assert calls[0].runs == []
    run_id = await start_run(services, fake_starter)
    await eventually(lambda: services.run_events._polls, "The start of the run")
    release.set()
    await asyncio.wait_for(opening, WAIT_SECONDS)

    await eventually(lambda: run_ids(user) == [run_id], "The run that started during the read")
    assert row_of(user, run_id)["status"] == "In progress"
    end_run(fake_starter)
    await eventually(lambda: row_of(user, run_id)["status"] == "Succeeded", "The end of the run")


async def test_a_run_that_starts_during_the_first_read_of_the_scheduler_page_shows(
    live, sessions, fake_starter, monkeypatch
):
    user, services = live
    take_status = scheduler_page.take_status
    reading = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def slow_first_status(services):
        status = await take_status(services)
        calls.append(status)
        if len(calls) == 1:
            reading.set()
            await release.wait()
        return status

    monkeypatch.setattr(scheduler_page, "take_status", slow_first_status)
    opening = asyncio.create_task(user.open("/scheduler"))
    await asyncio.wait_for(reading.wait(), WAIT_SECONDS)
    with sessions.begin() as session:
        session.get(Schedule, 1).timezone = "America/New_York"
    await start_run(services, fake_starter)
    await eventually(lambda: services.run_events._polls, "The start of the run")
    release.set()
    await asyncio.wait_for(opening, WAIT_SECONDS)

    await eventually(lambda: text_of(user, "timezone") == "America/New_York", "The status after the missed start")
    end_run(fake_starter)
