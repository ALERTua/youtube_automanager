"""The link of a page to the events of the runs, so that the page follows a run while it runs, with no click.

A page makes one ``RunFollower`` in its builder, before its first read, and calls ``start`` after it shows that read.
"""

from collections.abc import Callable
from typing import Any

from nicegui import Event, ui

from youtube_automanager.run_events import RunEvents

# A handler of an event gets the arguments of the emit. It can return an awaitable: NiceGUI then awaits it as a task in
# the client of the page.
type Handler = Callable[..., Any]


class RunFollower(ui.element):
    """An invisible element that gives the events of the runs to the handlers of one page, while the element exists.

    The follower subscribes in the UI context of the page, so each handler runs in the client of its page. NiceGUI
    matches an unsubscribe by the function, so the follower subscribes closures of its own, and the unsubscribe of one
    page leaves each other page subscribed. The follower unsubscribes when it is deleted: with the other elements of the
    page at the delete of the client, or alone.

    The follower holds back each event until ``start``, and ``start`` returns True when an event came before it. A page
    then reads its data again, so no event goes missing between the first read of the page and the subscription.

    NiceGUI runs an async handler as a task, so the handlers of a page must not let a ``progress`` overwrite the end of
    its run. The follower notes each ``finished`` at its emit and gives no later ``progress`` of that run to the page. A
    ``progress`` that came before the ``finished`` starts its task first, and asyncio runs the first step of the tasks in
    the order of their start. So a ``progress`` handler that changes the page in its first step, before any await of a
    read, always comes before the read of the ``finished`` handler.
    """

    def __init__(
        self,
        events: RunEvents,
        *,
        started: Handler | None = None,
        progress: Handler | None = None,
        finished: Handler | None = None,
    ) -> None:
        super().__init__()
        self.set_visibility(False)
        self._live = False
        self._missed = False
        self._ended: set[int] = set()
        # The counts of the last progress of each run. They stay after the end of the run, because a progress handler
        # that NiceGUI runs after the ``finished`` still reads them. Only a new start of the same run id drops them.
        self._counts: dict[int, tuple[int, int]] = {}
        self._subscriptions: list[tuple[Event[...], Handler]] = []
        self._subscribe(events.started, started, self._note_start)
        self._subscribe(events.progress, progress, self._note_progress)
        self._subscribe(events.finished, finished, self._note_end)

    def _subscribe(self, event: Event[...], handler: Handler | None, note: Callable[..., bool]) -> None:
        def call(*args: Any) -> Any:
            # The note of the event comes first, also before ``start``, so the follower knows each ended run.
            if not note(*args):
                return None
            if not self._live:
                self._missed = True
                return None
            return None if handler is None else handler(*args)

        event.subscribe(call, expect_args=True)
        self._subscriptions.append((event, call))

    def _note_start(self, run_id: int, *_args: Any) -> bool:
        # SQLite can give the id of a deleted newest run to a new run.
        self._ended.discard(run_id)
        self._counts.pop(run_id, None)
        return True

    def _note_progress(self, run_id: int, added: int, seen: int, *_args: Any) -> bool:
        if run_id in self._ended:
            return False
        self._counts[run_id] = (added, seen)
        return True

    def _note_end(self, run_id: int, *_args: Any) -> bool:
        self._ended.add(run_id)
        return True

    def start(self) -> bool:
        """Give each next event to the handlers. Return True when an event came before, and the page missed it."""
        self._live = True
        return self._missed

    def counts(self, run_id: int, added: int, seen: int) -> tuple[int, int]:
        """Return the added and the seen count of a read of the run ``run_id``, raised to the counts of its last progress.

        The counts of a run only grow, and a read that started before a progress can end after it.
        """
        last_added, last_seen = self._counts.get(run_id, (added, seen))
        return max(added, last_added), max(seen, last_seen)

    def _handle_delete(self) -> None:
        for event, call in self._subscriptions:
            event.unsubscribe(call)
        self._subscriptions.clear()
        super()._handle_delete()
