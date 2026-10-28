"""Tells the pages at once when a sync run starts, makes progress and ends.

``RunEvents`` is the listener of the runner. The runner calls it from the thread of the run, and it moves each call to
the event loop with ``loop.call_soon_threadsafe``. The child of a run tells the parent nothing until its exit, but it
commits each add and each seen video together with the counters of the ``run`` row. So while a run is active,
``RunEvents`` reads the counters and the new ``run_action`` rows each POLL_SECONDS, and emits ``progress`` when
something changed.

The events are NiceGUI events. A page subscribes in its builder and wraps its handler in ``with client:`` of its own
client. NiceGUI gives the arguments of an emit to a handler only when the handler has a required parameter that is not
``*args`` or ``**kwargs``, a keyword-only one included, or when it subscribed with ``expect_args=True``. A handler of
``*args`` alone thus needs ``expect_args=True``.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from nicegui import Event
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import Run, RunAction, RunActionKind, RunStatus, RunTrigger

LOG = logging.getLogger(__name__)

# How often the progress of an active run is read from the database.
POLL_SECONDS = 1.0
# How long the stop of the service waits for a read of the progress that already runs. A read takes milliseconds.
CLOSE_WAIT_SECONDS = 5.0


@dataclass(frozen=True)
class ActionItem:
    """One action of a run as plain values, so a subscriber can read it after the session of the read closed."""

    id: int
    at: datetime
    action: RunActionKind
    video_id: str | None
    channel_id: str | None
    rule_id: int | None
    playlist_id: str | None
    detail: str | None


def action_item(row: RunAction) -> ActionItem:
    """Copy the values of a ``run_action`` row, so they stay readable after its session closed."""
    return ActionItem(
        row.id,
        row.at,
        row.action,
        row.video_id,
        row.channel_id,
        row.rule_id,
        row.playlist_id,
        row.detail,
    )


@dataclass(frozen=True)
class RunProgress:
    """The counters of a run, and its actions with an id above the id of the last read, oldest first."""

    status: RunStatus
    added: int
    seen: int
    actions: tuple[ActionItem, ...]


def read_progress(sessions: sessionmaker[Session], run_id: int, after_id: int) -> RunProgress | None:
    """Read the progress of the run ``run_id``. Return None when the ``run`` row is gone."""
    with sessions() as session:
        run = session.get(Run, run_id)
        if run is None:
            return None
        rows = session.scalars(
            select(RunAction).where(RunAction.run_id == run_id, RunAction.id > after_id).order_by(RunAction.id)
        )
        actions = tuple(action_item(row) for row in rows)
        return RunProgress(run.status, run.added_count, run.seen_count, actions)


@dataclass(frozen=True)
class _Poll:
    """The poll of the progress of one active run. ``stop`` ends it after the read that runs."""

    stop: asyncio.Event
    task: asyncio.Task[None]


class RunEvents:
    """The events of the runs of one service, and the listener of its runner.

    ``started`` gets the run id and the trigger, ``progress`` the run id, the added count, the seen count and the new
    actions as a tuple of ``ActionItem``, and ``finished`` the run id and the status that the runner meant to write. The
    write can fail, so a page reads the ``run`` row again at ``finished``. ``started`` of a run always comes
    before its ``finished``, and no ``progress`` of a run comes after its ``finished``. ``finished`` comes at once, so
    the last actions of a run can come in no ``progress``: a page reads the run again at its end.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        loop: asyncio.AbstractEventLoop,
        *,
        poll_seconds: float = POLL_SECONDS,
    ):
        self.started: Event[[int, RunTrigger]] = Event()
        self.progress: Event[[int, int, int, tuple[ActionItem, ...]]] = Event()
        self.finished: Event[[int, RunStatus]] = Event()
        self._sessions = sessions
        self._loop = loop
        self._poll_seconds = poll_seconds
        # The polls of the active runs, by run id. Only the event loop changes it.
        self._polls: dict[int, _Poll] = {}
        self._closed = False

    # The listener of the runner. Each call comes from the thread of a run.

    def run_started(self, run_id: int, trigger: RunTrigger) -> None:
        self._call_in_loop(self._started, run_id, trigger)

    def run_finished(self, run_id: int, status: RunStatus) -> None:
        self._call_in_loop(self._finished, run_id, status)

    def _call_in_loop[*Args](self, callback: Callable[[int, *Args], None], run_id: int, *args: *Args) -> None:
        # The calls of one run come one after the other, and the loop runs its callbacks in the order of the calls, so
        # `started` comes before `finished` also for a run that ends at once.
        try:
            self._loop.call_soon_threadsafe(callback, run_id, *args)
        except RuntimeError:
            # The loop is closed: a run that ends while the service stops has no page left to tell.
            LOG.debug("Run %s: the event loop is closed, so no page learns about it", run_id)

    # The event loop.

    def _started(self, run_id: int, trigger: RunTrigger) -> None:
        if self._closed:
            return
        # A poll of the same id is left from a run whose end never came, for example when SQLite gave the id of a
        # deleted newest row again. It stops after its read that runs.
        old = self._polls.pop(run_id, None)
        if old is not None:
            old.stop.set()
        stop = asyncio.Event()
        task = self._loop.create_task(self._poll(run_id, stop), name=f"run-{run_id}-progress")
        self._polls[run_id] = _Poll(stop, task)
        self.started.emit(run_id, trigger)

    def _finished(self, run_id: int, status: RunStatus) -> None:
        poll = self._polls.pop(run_id, None)
        if poll is not None:
            poll.stop.set()
        if self._closed:
            return
        self.finished.emit(run_id, status)

    async def _poll(self, run_id: int, stop: asyncio.Event) -> None:
        """Read the progress of the run each POLL_SECONDS until ``stop`` is set, and emit ``progress`` on a change.

        The poll ends also when the ``run`` row is gone or no longer running.
        """
        added = seen = last_id = 0
        failing = False
        while not await _stopped_within(stop, self._poll_seconds):
            try:
                progress = await asyncio.to_thread(read_progress, self._sessions, run_id, last_id)
            except Exception as error:  # noqa: BLE001
                # The next read can succeed, for example after a lock of the database. A database that stays down would
                # give one warning each second, so only the first failure in a row is a warning.
                LOG.log(
                    logging.DEBUG if failing else logging.WARNING, "Run %s: cannot read the progress: %s", run_id, error
                )
                failing = True
                continue
            failing = False
            # The run can end during the read, and no progress comes after `finished`.
            if stop.is_set() or progress is None:
                return
            if progress.actions or (progress.added, progress.seen) != (added, seen):
                added, seen = progress.added, progress.seen
                if progress.actions:
                    last_id = progress.actions[-1].id
                self.progress.emit(run_id, added, seen, progress.actions)
            if progress.status is not RunStatus.RUNNING:
                return

    async def close(self) -> None:
        """Stop each poll, and emit nothing more. A read that already runs gets up to CLOSE_WAIT_SECONDS to end."""
        self._closed = True
        polls = list(self._polls.values())
        self._polls.clear()
        for poll in polls:
            poll.stop.set()
        tasks = [poll.task for poll in polls]
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=CLOSE_WAIT_SECONDS)
        for task in pending:
            task.cancel()
        if pending:
            LOG.warning("A read of the progress of a run did not end within %s s", CLOSE_WAIT_SECONDS)
            await asyncio.wait(pending)


async def _stopped_within(stop: asyncio.Event, seconds: float) -> bool:
    """Wait up to ``seconds`` for ``stop``. Return True when it is set."""
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        return False
    return True
