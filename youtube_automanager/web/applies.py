"""The applies of a rule, which run in the background of the service.

An apply adds the matching videos of a saved rule since a day, and it can take more than 15 minutes. So it runs in a
task of the service and not of a page: the user can close the dialog, the editor or the tab, and the apply goes on. The
pages follow an apply through ``RunEvents`` and through ``Applies.changed``, which tells that an apply started or ended.

The claim of the run and the child run in one call of ``apply_in_child`` in a thread. A cancel of the page or of the
task thus never leaves a claimed run without its end, and the end releases the sync lock.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from nicegui import Event, background_tasks
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.rule_actions import apply_in_child
from youtube_automanager.runner import SyncRunner
from youtube_automanager.short_child import ChildError

LOG = logging.getLogger(__name__)

# How long the stop of the service waits for the task of an apply. The runner stops the child of the apply first, so
# the task then only reads the end of the run from the database.
CLOSE_WAIT_SECONDS = 5.0


@dataclass(frozen=True)
class ActiveApply:
    """An apply whose run started and whose task did not end yet."""

    run_id: int
    rule_id: int
    since: date


@dataclass
class _Start:
    """One call of ``Applies.start``. ``claimed`` gets the id of the run, or the error of a start that claimed no run."""

    rule_id: int
    since: date
    claimed: asyncio.Future[int]
    run_id: int | None = None


class Applies:
    """The applies of the service. The sync lock lets one apply run at a time."""

    def __init__(self, runner: SyncRunner, sessions: sessionmaker[Session]) -> None:
        # Emitted with no arguments when an apply started or ended.
        self.changed: Event[[]] = Event()
        self._runner = runner
        self._sessions = sessions
        # The active applies by run id. Only the event loop changes it.
        self._active: dict[int, ActiveApply] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def current(self) -> ActiveApply | None:
        """Return the active apply, or None. The end of one apply and the start of the next can meet for a moment, and
        then the newer apply wins."""
        return self._active[max(self._active)] if self._active else None

    def is_active(self, run_id: int) -> bool:
        return run_id in self._active

    async def start(self, rule_id: int, since: date) -> int:
        """Start the apply of the saved rule ``rule_id`` to the videos since ``since``. Return the id of its run.

        The call returns after the claim of the run, and the apply goes on in a task of the service. Raise ``ChildBusy``
        and start nothing when the sync lock is taken, and raise the error of a claim that failed.
        """
        loop = asyncio.get_running_loop()
        start = _Start(rule_id, since, loop.create_future())

        def on_claimed(run_id: int) -> None:
            # The thread of the apply calls it, so the event loop learns the id through its queue. The end of the run
            # comes through the same queue later, so the loop learns the start first.
            loop.call_soon_threadsafe(self._claimed, start, run_id)

        task = background_tasks.create(self._apply(start, on_claimed), name=f"apply-rule-{rule_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return await start.claimed

    def _claimed(self, start: _Start, run_id: int) -> None:
        start.run_id = run_id
        self._active[run_id] = ActiveApply(run_id, start.rule_id, start.since)
        # The caller can be gone, for example after a cancel of its page handler.
        if not start.claimed.done():
            start.claimed.set_result(run_id)
        self.changed.emit()

    async def _apply(self, start: _Start, on_claimed: Callable[[int], None]) -> None:
        try:
            applied = await asyncio.to_thread(
                apply_in_child, self._runner, self._sessions, start.rule_id, start.since, on_claimed
            )
        except Exception as error:
            if start.run_id is None:
                # No run started, so the caller tells the user.
                if not start.claimed.done():
                    start.claimed.set_exception(error)
                return
            # The apply has no page to tell, because the user can have closed it. The history shows the run.
            if isinstance(error, ChildError):
                LOG.error("The apply of rule %s in run %s failed: %s", start.rule_id, start.run_id, error)
            else:
                LOG.exception("The apply of rule %s in run %s failed", start.rule_id, start.run_id)
        else:
            LOG.info(
                "The apply of rule %s ended as run %s with %s", start.rule_id, applied.run_id, applied.status.value
            )
        finally:
            if not start.claimed.done():
                # The task was cancelled before the claim, so the caller gets the cancel too.
                start.claimed.cancel()
            if start.run_id is not None:
                self._active.pop(start.run_id, None)
                self.changed.emit()

    async def close(self) -> None:
        """Wait up to CLOSE_WAIT_SECONDS for the task of each apply, and cancel a task that does not end.

        Call it after ``SyncRunner.stop``, which stops the child of an active apply and ends its run as failed.
        """
        tasks = list(self._tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=CLOSE_WAIT_SECONDS)
        for task in pending:
            task.cancel()
        if pending:
            LOG.warning("An apply did not end within %s s after the stop of the service", CLOSE_WAIT_SECONDS)
            await asyncio.wait(pending)
