import asyncio
import logging
import threading
from contextlib import asynccontextmanager

import httpx
import pytest
from nicegui import ui
from sqlalchemy import update
from ui_support import PatientUser, client_of, eventually, store

from youtube_automanager import run_events as run_events_module
from youtube_automanager.db.models import Run, RunAction, RunActionKind, RunStatus, RunTrigger
from youtube_automanager.run_events import ActionItem, RunEvents, read_progress
from youtube_automanager.runner import SyncRunner
from youtube_automanager.web.app import create_app
from youtube_automanager.web.ui import mount_ui

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]

WAIT_SECONDS = 10
# A short poll, so a test sees a change soon.
POLL_SECONDS = 0.02


class Recorder:
    """Subscribes to the three events and records each emit in its order, with the thread that ran the subscriber.

    NiceGUI gives the arguments of an emit only to a handler with a required positional parameter, so a handler of
    ``*args`` needs ``expect_args=True``.
    """

    def __init__(self, events):
        self.emits = []
        self.threads = set()
        events.started.subscribe(lambda *args: self._record("started", args), expect_args=True)
        events.progress.subscribe(lambda *args: self._record("progress", args), expect_args=True)
        events.finished.subscribe(lambda *args: self._record("finished", args), expect_args=True)

    def _record(self, name, args):
        self.threads.add(threading.get_ident())
        self.emits.append((name, *args))

    def names(self):
        return [emit[0] for emit in self.emits]

    def of(self, name):
        return [emit[1:] for emit in self.emits if emit[0] == name]


def new_run(sessions, status=RunStatus.RUNNING):
    with sessions.begin() as session:
        run = Run(status=status, trigger=RunTrigger.MANUAL)
        session.add(run)
        session.flush()
        return run.id


def set_counts(sessions, run_id, added, seen):
    with sessions.begin() as session:
        session.execute(update(Run).where(Run.id == run_id).values(added_count=added, seen_count=seen))


def add_action(sessions, run_id, video_id, action=RunActionKind.ADDED):
    store(sessions, RunAction(run_id=run_id, action=action, video_id=video_id, playlist_id="WL", rule_id=1))


async def in_thread(call, *args):
    """Call the listener from another thread, as the thread of a run does."""
    await asyncio.to_thread(call, *args)


@pytest.fixture
async def events(sessions):
    events = RunEvents(sessions, asyncio.get_running_loop(), poll_seconds=POLL_SECONDS)
    yield events
    await events.close()


async def test_calls_from_the_thread_of_a_run_arrive_on_the_event_loop_in_their_order(events):
    recorder = Recorder(events)

    await in_thread(events.run_started, 7, RunTrigger.SCHEDULE)
    await in_thread(events.run_finished, 7, RunStatus.FAILED)
    await eventually(lambda: "finished" in recorder.names(), "the end of the run")

    assert recorder.emits == [("started", 7, RunTrigger.SCHEDULE), ("finished", 7, RunStatus.FAILED)]
    assert recorder.threads == {threading.get_ident()}


async def test_a_run_of_the_runner_gives_started_then_finished(sessions, settings, fake_starter):
    events = RunEvents(sessions, asyncio.get_running_loop(), poll_seconds=POLL_SECONDS)
    recorder = Recorder(events)
    runner = SyncRunner(
        sessions,
        ytdlp_dir=settings.ytdlp_dir,
        database_url=settings.database_url,
        child_timeout_s=WAIT_SECONDS,
        start=fake_starter,
        lock=threading.Lock(),
        listener=events,
    )
    fake_starter.plan.append({"exit_code": 1, "stderr": "the cause\n"})

    # The claim and the run come from two threads, as `run_now` and the job of the scheduler do. The child ends at once.
    run_id = await asyncio.to_thread(runner.claim, RunTrigger.MANUAL)
    assert run_id is not None
    status = await asyncio.to_thread(runner.execute, run_id)
    await eventually(lambda: "finished" in recorder.names(), "the end of the run")
    await events.close()

    assert status is RunStatus.FAILED
    assert recorder.emits == [("started", run_id, RunTrigger.MANUAL), ("finished", run_id, RunStatus.FAILED)]


async def test_the_poll_emits_progress_when_the_row_changes_and_stops_at_the_end(events, sessions):
    recorder = Recorder(events)
    run_id = new_run(sessions)
    await in_thread(events.run_started, run_id, RunTrigger.MANUAL)
    await eventually(lambda: recorder.of("started"), "the start")

    set_counts(sessions, run_id, 0, 3)
    await eventually(lambda: recorder.of("progress"), "the first progress")
    add_action(sessions, run_id, "vid1")
    add_action(sessions, run_id, "vid2")
    set_counts(sessions, run_id, 2, 5)
    await eventually(lambda: recorder.of("progress")[-1][:3] == (run_id, 2, 5), "the second progress")
    # No change, no progress.
    count = len(recorder.of("progress"))
    await asyncio.sleep(POLL_SECONDS * 5)
    assert len(recorder.of("progress")) == count

    actions = [action for (_, _, _, items) in recorder.of("progress") for action in items]
    assert [(action.video_id, action.action, action.playlist_id, action.rule_id) for action in actions] == [
        ("vid1", RunActionKind.ADDED, "WL", 1),
        ("vid2", RunActionKind.ADDED, "WL", 1),
    ]
    # Plain values, not rows of a session that closed.
    assert all(type(action) is ActionItem for action in actions)
    assert recorder.of("progress")[0] == (run_id, 0, 3, ())

    await in_thread(events.run_finished, run_id, RunStatus.SUCCEEDED)
    await eventually(lambda: recorder.of("finished"), "the end")
    add_action(sessions, run_id, "vid3")
    set_counts(sessions, run_id, 3, 6)
    await asyncio.sleep(POLL_SECONDS * 5)

    assert recorder.names()[-1] == "finished"
    assert not events._polls


async def test_no_progress_comes_after_finished_also_from_a_read_that_runs(events, sessions, monkeypatch):
    recorder = Recorder(events)
    run_id = new_run(sessions)
    set_counts(sessions, run_id, 1, 1)
    reading = threading.Event()
    release = threading.Event()

    def slow_read(*args):
        reading.set()
        release.wait(WAIT_SECONDS)
        return read_progress(*args)

    monkeypatch.setattr(run_events_module, "read_progress", slow_read)
    await in_thread(events.run_started, run_id, RunTrigger.MANUAL)
    assert await asyncio.to_thread(reading.wait, WAIT_SECONDS)
    (poll,) = events._polls.values()

    await in_thread(events.run_finished, run_id, RunStatus.SUCCEEDED)
    await eventually(lambda: recorder.of("finished"), "the end")
    release.set()
    await asyncio.wait_for(poll.task, WAIT_SECONDS)

    assert recorder.names() == ["started", "finished"]


async def test_the_poll_ends_when_the_run_row_is_gone(events, sessions):
    run_id = new_run(sessions)
    await in_thread(events.run_started, run_id, RunTrigger.MANUAL)
    await eventually(lambda: events._polls, "the poll")
    (poll,) = events._polls.values()

    with sessions.begin() as session:
        session.delete(session.get(Run, run_id))

    await asyncio.wait_for(poll.task, WAIT_SECONDS)


async def test_the_poll_of_an_ended_row_emits_the_last_change_and_ends(events, sessions):
    """A run whose end the listener never learned, for example after a failed write, does not keep a poll."""
    recorder = Recorder(events)
    run_id = new_run(sessions)
    await in_thread(events.run_started, run_id, RunTrigger.MANUAL)
    await eventually(lambda: events._polls, "the poll")
    (poll,) = events._polls.values()

    add_action(sessions, run_id, "vid1", RunActionKind.ERROR)
    with sessions.begin() as session:
        session.execute(update(Run).where(Run.id == run_id).values(status=RunStatus.FAILED))
    await asyncio.wait_for(poll.task, WAIT_SECONDS)

    ((_, added, seen, (action,)),) = recorder.of("progress")
    assert (added, seen, action.video_id, action.action) == (0, 0, "vid1", RunActionKind.ERROR)


async def test_a_failed_read_writes_a_warning_and_the_poll_goes_on(events, sessions, monkeypatch, caplog):
    recorder = Recorder(events)
    run_id = new_run(sessions)
    set_counts(sessions, run_id, 1, 2)
    failures = [RuntimeError("the database is locked")]

    def read(*args):
        if failures:
            raise failures.pop()
        return read_progress(*args)

    monkeypatch.setattr(run_events_module, "read_progress", read)
    await in_thread(events.run_started, run_id, RunTrigger.MANUAL)
    await eventually(lambda: recorder.of("progress"), "the progress after the failed read")

    assert recorder.of("progress") == [(run_id, 1, 2, ())]
    (record,) = [record for record in caplog.records if record.name == run_events_module.__name__]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == f"Run {run_id}: cannot read the progress: the database is locked"


async def test_close_waits_for_the_read_that_runs_and_emits_nothing_more(sessions, monkeypatch):
    events = RunEvents(sessions, asyncio.get_running_loop(), poll_seconds=POLL_SECONDS)
    recorder = Recorder(events)
    run_id = new_run(sessions)
    set_counts(sessions, run_id, 1, 1)
    reading = threading.Event()
    read_ended = threading.Event()

    def slow_read(*args):
        reading.set()
        # The read ends by itself a moment later, as a read of the database does.
        threading.Event().wait(0.2)
        try:
            return read_progress(*args)
        finally:
            read_ended.set()

    monkeypatch.setattr(run_events_module, "read_progress", slow_read)
    await in_thread(events.run_started, run_id, RunTrigger.MANUAL)
    assert await asyncio.to_thread(reading.wait, WAIT_SECONDS)
    (poll,) = events._polls.values()

    await events.close()

    # No thread of the poll still reads after the close.
    assert read_ended.is_set()
    assert poll.task.done()
    await in_thread(events.run_finished, run_id, RunStatus.SUCCEEDED)
    await in_thread(events.run_started, run_id + 1, RunTrigger.MANUAL)
    await asyncio.sleep(POLL_SECONDS * 5)
    assert recorder.names() == ["started"]
    assert not events._polls


async def test_close_cancels_a_read_that_does_not_end_in_time(sessions, monkeypatch, caplog):
    monkeypatch.setattr(run_events_module, "CLOSE_WAIT_SECONDS", 0.1)
    events = RunEvents(sessions, asyncio.get_running_loop(), poll_seconds=POLL_SECONDS)
    run_id = new_run(sessions)
    reading = threading.Event()
    release = threading.Event()

    def stuck_read(*args):
        reading.set()
        release.wait(WAIT_SECONDS)
        return read_progress(*args)

    monkeypatch.setattr(run_events_module, "read_progress", stuck_read)
    await in_thread(events.run_started, run_id, RunTrigger.MANUAL)
    assert await asyncio.to_thread(reading.wait, WAIT_SECONDS)
    (poll,) = events._polls.values()

    try:
        await events.close()
        assert poll.task.cancelled()
        assert "A read of the progress of a run did not end within 0.1 s" in caplog.text
    finally:
        release.set()


async def test_a_call_after_the_loop_closed_does_not_raise(sessions):
    loop = asyncio.new_event_loop()
    loop.close()
    events = RunEvents(sessions, loop)

    # A run that ends while the service stops calls the listener after the loop closed.
    events.run_started(1, RunTrigger.SCHEDULE)
    events.run_finished(1, RunStatus.FAILED)


async def test_read_progress_of_a_missing_run_is_none(sessions):
    assert read_progress(sessions, 1, 0) is None


@asynccontextmanager
async def service_with_pages(settings, start):
    """Run the whole service with its pages, and give a simulated browser on it and the services."""
    app = create_app(settings, start=start)
    mount_ui(app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        yield PatientUser(client), app.state.services


async def test_the_service_tells_the_start_and_the_end_of_a_run_now(nicegui_globals, settings, fake_starter):
    async with service_with_pages(settings, fake_starter) as (_, services):
        recorder = Recorder(services.run_events)

        result = await services.scheduler.run_now()
        await eventually(lambda: recorder.of("finished"), "the end of the run")

    assert recorder.emits == [
        ("started", result.run_id, RunTrigger.MANUAL),
        ("finished", result.run_id, RunStatus.SUCCEEDED),
    ]


async def test_a_subscriber_runs_in_the_client_where_it_subscribed_until_the_client_is_deleted(
    nicegui_globals, settings, fake_starter
):
    """The facts of NiceGUI that the pages build on: an emit from a callback of the loop needs no client, an async
    subscriber runs as a task in the client where it subscribed, and the delete of the client unsubscribes it."""
    async with service_with_pages(settings, fake_starter) as (user, services):
        await user.open("/")
        client = client_of(user)
        clients = []

        async def on_started(run_id, trigger):
            await asyncio.sleep(0)
            clients.append(ui.context.client)

        with client:
            services.run_events.started.subscribe(on_started)
        await services.scheduler.run_now()
        await eventually(lambda: clients, "the subscriber")
        assert clients == [client]

        client.delete()
        assert on_started not in [callback.func for callback in services.run_events.started.callbacks]


async def test_the_stop_of_the_service_ends_the_poll_of_an_active_run(nicegui_globals, settings, fake_starter, caplog):
    fake_starter.plan.append({"blocking": True})
    async with service_with_pages(settings, fake_starter) as (user, services):
        await user.open("/")
        await services.scheduler.run_now()
        assert await asyncio.to_thread(fake_starter.child_started.wait, WAIT_SECONDS)
        await eventually(lambda: services.run_events._polls, "the poll of the run")
        (poll,) = services.run_events._polls.values()

    assert poll.task.done()
    assert not services.run_events._polls
    assert fake_starter.children[0].terminated
    assert "Could not cancel" not in caplog.text
