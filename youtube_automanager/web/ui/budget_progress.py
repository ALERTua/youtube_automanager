"""The progress block of a bulk action that sends requests with the cookies of the account.

The block shows the progress of the action, "k of N" with a bar, and the shortest time that the rest of the action
needs. Under it, the block shows the state of the shared request budget of ``pacing.py``: the tokens of the bucket as
"Requests without a wait", and the wait for the next token as "Next request". During a pause of the requests with
cookies, it shows the end of the pause in the form of the browser instead. The random pause between two requests does
not show, because a process draws it only at its request.

While an action runs, the block reads the row of ``request_budget`` each ``BUDGET_POLL_SECONDS`` in a thread, with a
plain ``select``. It never reads with ``ROW_QUERY`` or ``_locked_row`` of ``pacing.py``: they lock the row, and
``_locked_row`` inserts a missing row, which takes the write lock of SQLite from the child that sends the requests. A
missing row is a full bucket without a pause. When no action runs, the block is hidden and reads nothing.
"""

import asyncio
import datetime
import math
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from nicegui import ui
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import RequestBudgetState, utc_now
from youtube_automanager.i18n import _
from youtube_automanager.pacing import BUCKET_SIZE, REFILL_S, Clock, refilled_tokens
from youtube_automanager.request_pause import RequestPause
from youtube_automanager.scheduler import stored_timezone
from youtube_automanager.web.ui.account_lists import pause_text, show_pause_end
from youtube_automanager.web.ui.common import show_time

# How often the block reads the request budget while an action runs.
BUDGET_POLL_SECONDS = 1.0
# The color of the part of a bar or of a cell that is not filled. It shows on a light, a dark and a colored background.
EMPTY_COLOR = "rgba(128, 128, 128, 0.35)"
FILLED_COLOR = "var(--q-positive)"
# The label, the bar or the cells, and the value of each line of the budget.
GRID_STYLE = "display: grid; grid-template-columns: minmax(8rem, auto) 1fr minmax(7rem, auto); gap: 6px 12px"


@dataclass(frozen=True)
class BudgetView:
    """The state of the request budget at ``now``: the tokens of the bucket, and the pause of the requests with cookies
    with its start, or None while no pause is on."""

    tokens: float
    pause: RequestPause | None
    pause_started: datetime.datetime | None
    now: datetime.datetime

    @property
    def next_wait(self) -> float:
        return next_wait_seconds(self.tokens)


def next_wait_seconds(tokens: float) -> float:
    """Return the wait in seconds for the token of the next request, when the bucket holds ``tokens``."""
    return (1 - tokens) * REFILL_S if tokens < 1 else 0.0


def remaining_seconds(requests: int, tokens: float) -> float:
    """Return the shortest time in seconds of ``requests`` more requests, when the bucket holds ``tokens``.

    The tokens let the first requests go without a wait, and each later request waits for a new token. The answers and
    the random pauses only make the time longer.
    """
    return max(0.0, requests - tokens) * REFILL_S


def _zone(name: str) -> str:
    """Return ``name`` when it is a known time zone, else UTC, as the texts of the breaker do."""
    try:
        ZoneInfo(name)
    # An unknown name raises ZoneInfoNotFoundError, a KeyError, and a name that is no file name raises ValueError or
    # OSError.
    except KeyError, ValueError, OSError:
        return "UTC"
    return name


def budget_view(row: RequestBudgetState | None, zone: str, now: datetime.datetime) -> BudgetView:
    """Return the state of the budget row ``row`` at ``now``. ``zone`` is the time zone of the service.

    A missing row is a full bucket without a pause, as the first use of the budget makes it. A pause that starts after
    ``now``, because the clock went back, counts from ``now``, as the budget counts it.
    """
    if row is None:
        return BudgetView(float(BUCKET_SIZE), None, None, now)
    tokens = refilled_tokens(row.tokens, row.refilled_at, now)
    start, end = row.breaker_opened_at, row.breaker_until
    if start is not None and end is not None and start > now:
        start, end = now, now + (end - start)
    if row.breaker_reason is None or end is None or now >= end:
        return BudgetView(tokens, None, None, now)
    return BudgetView(tokens, RequestPause(row.breaker_reason, end, _zone(zone)), start, now)


def read_budget(sessions: sessionmaker[Session], default_timezone: str, clock: Clock = utc_now) -> BudgetView:
    """Read the state of the request budget with a plain ``select``, which takes no lock and writes nothing.

    The time zone of the schedule names the end of a pause. This call blocks.
    """
    with sessions() as session:
        row = session.scalar(select(RequestBudgetState).where(RequestBudgetState.id == 1))
        zone = stored_timezone(session, default_timezone)
    return budget_view(row, zone, clock())


def decimal_text(value: float) -> str:
    """Return ``value`` with one digit after the decimal mark of the language."""
    # Translators: the decimal mark of a number, for example in "7.4 of 15".
    return f"{value:.1f}".replace(".", _("."))


def clock_text(seconds: float) -> str:
    """Return ``seconds``, rounded up to a whole second, as minutes and seconds, for example "1:05"."""
    minutes, rest = divmod(math.ceil(seconds), 60)
    return f"{minutes}:{rest:02d}"


def remaining_text(seconds: float) -> str:
    # Translators: {time} is a time in minutes and seconds, for example "0:27".
    return _("at least ~{time} left").format(time=clock_text(seconds))


def tokens_text(tokens: float) -> str:
    # Translators: the requests that can go without a wait, of the most that the budget holds, for example "7.4 of 15".
    return _("{tokens} of {size}").format(tokens=decimal_text(tokens), size=BUCKET_SIZE)


def wait_text(seconds: float) -> str:
    if seconds <= 0:
        # Translators: the next request goes without a wait.
        return _("at once")
    # Translators: the wait for the next request, for example "in 1.6 s".
    return _("in {seconds} s").format(seconds=decimal_text(seconds))


def pause_left_text(seconds: float) -> str:
    """Return the rest of a pause, rounded up to a whole minute, in minutes or in hours and minutes."""
    hours, minutes = divmod(max(1, math.ceil(seconds / 60)), 60)
    if not hours:
        return _("{minutes} min left").format(minutes=minutes)
    return _("{hours} h {minutes} min left").format(hours=hours, minutes=minutes)


def pause_until_text(view: BudgetView) -> str:
    """Return the line of a pause, with ``{time}`` for its end, which the browser writes in its own form."""
    assert view.pause is not None
    left = pause_left_text((view.pause.until - view.now).total_seconds())
    # Translators: {time} is the end of the pause, which the browser writes in its own form. {left} is the rest of the
    # pause, for example "55 min left".
    return _("Pause of requests until {time} ({left})").format(time="{time}", left=left)


def _set_progressbar(element: ui.element, now: float, maximum: float, text: str) -> None:
    """Give ``element`` the values of a progress bar for a screen reader."""
    element.props["aria-valuenow"] = round(now, 1)
    element.props["aria-valuemax"] = maximum
    element.props["aria-valuetext"] = text


class BulkProgress:
    """The progress block of a bulk action, with the state of the request budget.

    ``start`` shows the block and starts the reads of the budget, ``update`` gives it the new progress, and ``stop``
    hides it and ends the reads. The caller puts its own buttons, for example Stop, into ``actions``. ``color`` is the
    Quasar color of the progress bar, so the block also shows on a colored background. With another color than
    "primary", the block sits on a colored background, and the text of a pause keeps the color of the other texts,
    because the color of an error is hard to read there.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        default_timezone: str,
        *,
        color: str = "primary",
        clock: Clock = utc_now,
    ) -> None:
        self._sessions = sessions
        self._default_timezone = default_timezone
        self._clock = clock
        self._remaining_requests = 0
        self._view: BudgetView | None = None
        # The number of the current action. A read that ends after the action ended, or after a new one started, drops
        # its result.
        self._action = 0
        with ui.column().classes("w-full gap-2").mark("bulk-progress") as self.element:
            with ui.row().classes("w-full items-center gap-4"):
                ui.spinner(color=color)
                self.title = ui.label().classes("text-weight-bold").mark("bulk-progress-title")
                self.remaining = ui.label().style("opacity: 0.8").mark("bulk-progress-remaining")
                ui.space()
                self.actions = ui.row().classes("items-center gap-2")
            self.bar = (
                ui.linear_progress(0, show_value=False, size="10px", color=color)
                .props("rounded aria-valuemin=0")
                .mark("bulk-progress-bar")
            )
            with ui.element("div").classes("w-full items-center").style(GRID_STYLE).mark("bulk-budget") as self.budget:
                ui.label(_("Requests without a wait")).style("opacity: 0.8")
                with (
                    ui.element("div")
                    .classes("row no-wrap")
                    .style("gap: 3px")
                    .props("role=progressbar aria-valuemin=0")
                    .mark("bulk-tokens") as self.tokens_bar
                ):
                    self.cells = [
                        ui.element("div").classes("col").style("height: 10px; border-radius: 2px")
                        for _index in range(BUCKET_SIZE)
                    ]
                self.tokens_bar.props["aria-label"] = _("Requests without a wait")
                self.tokens_label = ui.label().classes("text-right").mark("bulk-tokens-text")
                ui.label(_("Next request")).style("opacity: 0.8")
                self.wait_bar = (
                    ui.linear_progress(0, show_value=False, size="10px").props("rounded").mark("bulk-next-bar")
                )
                self.wait_bar.props["aria-label"] = _("Next request")
                self.wait_label = ui.label().classes("text-right").mark("bulk-next-text")
            self.slow_note = (
                ui.label(
                    _(
                        "The service sends at most one request with cookies each {seconds} s, so that YouTube does not "
                        "limit the requests."
                    ).format(seconds=f"{REFILL_S:g}")
                )
                .style("opacity: 0.8")
                .mark("bulk-slow-note")
            )
            with ui.column().classes("w-full gap-1").mark("bulk-pause") as self.pause_part:
                self.pause_label = ui.label().classes("text-weight-bold").mark("bulk-pause-until")
                self.pause_bar = (
                    ui.linear_progress(0, show_value=False, size="10px", color="negative")
                    .props("rounded")
                    .mark("bulk-pause-bar")
                )
                self.pause_bar.props["aria-label"] = _("Pause of the requests with cookies")
                self.pause_text = ui.label().mark("bulk-pause-text")
                if color == "primary":
                    self.pause_text.classes("text-negative")
        self.element.set_visibility(False)
        self._draw()
        self.timer = ui.timer(BUDGET_POLL_SECONDS, self._poll, active=False, immediate=False)

    @property
    def running(self) -> bool:
        return self.timer.active

    def start(self, title: str, done: int, total: int, remaining_requests: int) -> None:
        """Show the block for a new action, and read the budget each ``BUDGET_POLL_SECONDS`` until ``stop``."""
        self._action += 1
        self._view = None
        self.element.set_visibility(True)
        self.timer.active = True
        self.update(title, done, total, remaining_requests)

    def update(self, title: str, done: int, total: int, remaining_requests: int) -> None:
        """Show the progress ``done`` of ``total`` under ``title``, and ``remaining_requests`` for the rest."""
        self.title.set_text(title)
        self.bar.set_value(done / total if total else 0)
        self.bar.props["aria-label"] = title
        self._remaining_requests = remaining_requests
        self._draw()

    def stop(self) -> None:
        """Hide the block, and read the budget no more."""
        self._action += 1
        self.timer.active = False
        self._view = None
        self.element.set_visibility(False)

    async def _poll(self) -> None:
        action = self._action
        view = await asyncio.to_thread(read_budget, self._sessions, self._default_timezone, self._clock)
        if action != self._action:
            return
        self._view = view
        self._draw()

    def _draw(self) -> None:
        """Show the last read of the budget. Before the first read, only the progress shows."""
        view = self._view
        self.remaining.set_visibility(view is not None and view.pause is None)
        self.budget.set_visibility(view is not None and view.pause is None)
        self.slow_note.set_visibility(view is not None and view.pause is None and view.tokens < 1)
        self.pause_part.set_visibility(view is not None and view.pause is not None)
        if view is None:
            return
        if view.pause is not None:
            self._draw_pause(view)
            return
        self.remaining.set_text(remaining_text(remaining_seconds(self._remaining_requests, view.tokens)))
        for index, cell in enumerate(self.cells):
            filled = min(1.0, max(0.0, view.tokens - index))
            percent = round(filled * 100)
            cell.style(f"background: linear-gradient(90deg, {FILLED_COLOR} {percent}%, {EMPTY_COLOR} {percent}%)")
        text = tokens_text(view.tokens)
        self.tokens_label.set_text(text)
        _set_progressbar(self.tokens_bar, view.tokens, BUCKET_SIZE, text)
        wait = view.next_wait
        text = wait_text(wait)
        self.wait_label.set_text(text)
        self.wait_bar.set_value(1 - wait / REFILL_S)
        self.wait_bar.props["color"] = "warning" if wait > 0 else "positive"
        self.wait_bar.props["aria-valuetext"] = text

    def _draw_pause(self, view: BudgetView) -> None:
        pause = view.pause
        assert pause is not None
        zone = ZoneInfo(pause.zone)
        show_time(self.pause_label, pause.until, zone, pause_until_text(view))
        length = (pause.until - (view.pause_started or view.now)).total_seconds()
        passed = (view.now - (view.pause_started or view.now)).total_seconds()
        self.pause_bar.set_value(passed / length if length > 0 else 0)
        self.pause_bar.props["aria-valuetext"] = pause_left_text((pause.until - view.now).total_seconds())
        show_pause_end(self.pause_text, pause_text(pause), pause)
