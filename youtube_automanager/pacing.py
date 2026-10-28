"""The shared budget of the requests that carry the cookies of the account.

Each such request takes one token from a bucket of ``BUCKET_SIZE`` tokens. The bucket gets one new token each
``REFILL_S`` seconds, continuously, so a full bucket lets a short action go at once, and a long run gets at most one
request each ``REFILL_S`` seconds. A request without a token waits until one is there. From the end of one answer to
the start of the next request, the budget also keeps a random pause from ``PAUSE_S - PAUSE_JITTER_S`` to
``PAUSE_S + PAUSE_JITTER_S`` seconds. A request starts only when it has both the token and the pause.

HTTP 429, a "try again later" page or a bot check opens a breaker: each request with cookies raises ``BreakerOpen``
until the pause ends. Pauses start at ``FIRST_PAUSES``, double up to ``LONGEST_PAUSE``, and reset after
``LEVEL_RESET``.

The state lives in the one row of the table ``request_budget``, so the web process and each child that work with one
database share one budget, and a restart keeps the breaker. The times of the row come from the wall clock in UTC,
because a monotonic clock counts from another start in each process. A wall clock can go back. A time of the row that
lies after the current time then counts as the current time, so a change of the clock adds no tokens, and never makes
a wait longer than one token or one pause, or a pause longer than its own length.

The module never imports yt-dlp, so the web process can use it.
"""

import datetime
import functools
import logging
import time
from collections.abc import Callable
from random import uniform
from zoneinfo import ZoneInfo

from sqlalchemy import Insert, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager import notify
from youtube_automanager.db.models import BreakerReason, RequestBudgetState, utc_now
from youtube_automanager.request_pause import REASON_TEXTS, RequestPause
from youtube_automanager.scheduler import stored_timezone
from youtube_automanager.settings import DEFAULT_TZ

LOG = logging.getLogger(__name__)

# The most tokens that the bucket holds: the requests that can go one after another without a wait for a token.
BUCKET_SIZE = 15
# The time in which the bucket gets one new token.
REFILL_S = 4.0
# The middle of the random pause from the end of one answer to the start of the next request, and the most that the
# pause differs from it on each side.
PAUSE_S = 0.75
PAUSE_JITTER_S = 0.5

NO_WAIT = datetime.timedelta(0)

# The HTTP status of an answer that opens the breaker.
TOO_MANY_REQUESTS = 429
# yt-dlp: a rate limit lasts up to an hour. A bot check needs longer.
FIRST_PAUSES = {
    BreakerReason.HTTP_429: datetime.timedelta(hours=1),
    BreakerReason.TRY_AGAIN_LATER: datetime.timedelta(hours=1),
    BreakerReason.BOT_CHECK: datetime.timedelta(hours=6),
}
LONGEST_PAUSE = datetime.timedelta(hours=24)
# The time after the start of the last pause from which a new pause has the first length again.
LEVEL_RESET = datetime.timedelta(days=3)

# The read locks the row on PostgreSQL. SQLite renders no FOR UPDATE, and ``_locked_row`` locks it there.
ROW_QUERY = select(RequestBudgetState).where(RequestBudgetState.id == 1).with_for_update()

# The current time as a timezone-aware UTC datetime of the wall clock.
Clock = Callable[[], datetime.datetime]
# A sleep for the given number of seconds.
Sleep = Callable[[float], object]
# A random float from the first argument to the second, as ``random.uniform`` gives it.
Uniform = Callable[[float, float], float]
# A send of a notification.
Send = Callable[[notify.Notification], object]


class BreakerOpen(Exception):
    """The breaker is open. ``pause`` holds its reason, its end and the time zone of the service, and the text names
    them."""

    def __init__(self, pause: RequestPause) -> None:
        self.pause = pause
        super().__init__(str(pause))


def refilled_tokens(tokens: float, refilled_at: datetime.datetime, now: datetime.datetime) -> float:
    """Return the tokens of the bucket at ``now``, when it held ``tokens`` at ``refilled_at``.

    A ``now`` before ``refilled_at`` adds no tokens, because the clock went back.
    """
    elapsed_s = max(0.0, (now - refilled_at).total_seconds())
    return min(float(BUCKET_SIZE), tokens + elapsed_s / REFILL_S)


def minimum_seconds(requests: int) -> float:
    """Return the shortest time from the first to the last of ``requests`` requests that the budget allows.

    A full bucket lets the first ``BUCKET_SIZE`` requests go without a wait for a token, and each later request waits
    for a new one. Each request after the first also waits for the shortest pause. The answers and the requests of the
    other processes only make the time longer.
    """
    if requests < 2:
        return 0.0
    return max((requests - BUCKET_SIZE) * REFILL_S, (requests - 1) * (PAUSE_S - PAUSE_JITTER_S))


def next_pause(
    row: RequestBudgetState, now: datetime.datetime, reason: BreakerReason
) -> tuple[int, datetime.timedelta]:
    """Return the level and length of a pause for ``reason`` at ``now``: the first length after ``LEVEL_RESET``, else
    twice the last pause, within the first length and ``LONGEST_PAUSE``."""
    first = FIRST_PAUSES[reason]
    start, end = row.breaker_opened_at, row.breaker_until
    if start is None or end is None or now - start >= LEVEL_RESET:
        return 1, first
    return (row.breaker_level or 0) + 1, min(LONGEST_PAUSE, max(first, 2 * (end - start)))


def _is_open(row: RequestBudgetState, now: datetime.datetime) -> bool:
    return row.breaker_reason is not None and row.breaker_until is not None and now < row.breaker_until


def _has_ended(row: RequestBudgetState, now: datetime.datetime) -> bool:
    """Return True when a pause ended, and no answer after it showed yet that the requests work again."""
    return row.breaker_reason is not None and row.breaker_until is not None and row.breaker_until <= now


def _move_pause_after_clock_change(row: RequestBudgetState, now: datetime.datetime) -> None:
    """Start a pause that starts after ``now`` at ``now``, so a clock that went back never makes it longer."""
    start, end = row.breaker_opened_at, row.breaker_until
    if start is not None and end is not None and start > now:
        row.breaker_opened_at, row.breaker_until = now, now + (end - start)


def _take_token(
    row: RequestBudgetState, now: datetime.datetime, pause: datetime.timedelta
) -> tuple[datetime.timedelta, datetime.timedelta]:
    """Refill the bucket of ``row`` at ``now``. Take a token when the request can go. Return the wait for a token and
    the whole wait, which is no wait when the request can go."""
    tokens = refilled_tokens(row.tokens, row.refilled_at, now)
    row.tokens = tokens
    row.refilled_at = now
    if row.last_answer_at is not None and row.last_answer_at > now:
        row.last_answer_at = now
    # A wait is a whole number of microseconds, as each time of the database is. After a wait for a token, a rounding
    # error of a float can leave the bucket a little below 1, and the next wait then rounds to no wait, so the request
    # goes.
    token_wait = NO_WAIT
    if tokens < 1:
        token_wait = datetime.timedelta(seconds=(1 - tokens) * REFILL_S)
    pause_wait = NO_WAIT
    if row.last_answer_at is not None:
        pause_wait = row.last_answer_at + pause - now
    wait = max(token_wait, pause_wait)
    if wait <= NO_WAIT:
        row.tokens = max(0.0, tokens - 1)
    return token_wait, wait


def _insert_if_missing(session: Session) -> Insert:
    """Return an insert of the row of the budget that does nothing when the row exists."""
    insert = postgresql_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    return insert(RequestBudgetState).on_conflict_do_nothing(index_elements=["id"])


def _locked_row(session: Session, clock: Clock) -> tuple[RequestBudgetState, datetime.datetime]:
    """Lock the row of the budget, make it with a full bucket at the first use, and return it with the current time.

    The insert of a missing row comes first, so two first uses at once make one row. On SQLite, that write takes the
    write lock of the database at once, so no other process changes the row before the end of the transaction. On
    PostgreSQL, the read locks the row. The clock goes after the lock, so the time is not older than the row when the
    lock waited for another process.
    """
    session.execute(_insert_if_missing(session).values(id=1, tokens=float(BUCKET_SIZE), refilled_at=clock()))
    row = session.scalars(ROW_QUERY).one()
    now = clock()
    _move_pause_after_clock_change(row, now)
    return row, now


class RequestBudget:
    """The budget of the requests with the cookies of the account, shared through the database.

    ``sessions`` makes the sessions of the database. ``clock`` gives the current UTC time of the wall clock, and
    ``sleep`` waits. ``random`` gives the pause before each request from the range of its two arguments, so a test
    can pass the ``uniform`` method of a seeded ``random.Random``. ``send`` sends the notifications of the breaker,
    by default to each target of the database. ``default_timezone`` is the time zone of the service when the database
    holds no schedule.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Clock = utc_now,
        sleep: Sleep = time.sleep,
        random: Uniform = uniform,
        send: Send | None = None,
        default_timezone: str = DEFAULT_TZ,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._sleep = sleep
        self._random = random
        self._send = send or functools.partial(notify.send_to_stored_targets, sessions)
        self._default_timezone = default_timezone
        # A request of this budget got an answer after the end of a pause, and no answer opened a new pause since.
        self._answered_after_pause = False

    def before_request(self) -> None:
        """Wait for a token and for the pause after the last answer, then take the token.

        Raise ``BreakerOpen`` at once while the breaker is open. The wait sleeps outside the transaction. After the
        wait, the budget reads the row again, because another process can take a token, get an answer or open the
        breaker in the meantime.
        """
        pause = datetime.timedelta(seconds=self._random(PAUSE_S - PAUSE_JITTER_S, PAUSE_S + PAUSE_JITTER_S))
        while True:
            with self._sessions.begin() as session:
                row, now = _locked_row(session, self._clock)
                breaker_open = self._breaker_open(session, row) if _is_open(row, now) else None
                if breaker_open is None:
                    token_wait, wait = _take_token(row, now, pause)
                    if wait <= NO_WAIT:
                        return
            # The raise comes after the commit, which keeps a pause that a change of the clock moved.
            if breaker_open is not None:
                raise breaker_open
            if token_wait > NO_WAIT:
                LOG.info(
                    "A request with the cookies of the account waits %.2f s for a token of the budget",
                    wait.total_seconds(),
                )
            self._sleep(wait.total_seconds())

    def after_answer(self, status: int | None = None) -> None:
        """Keep the end of an answer, so the pause before the next request counts from it, also in another process.

        ``status`` is the HTTP status of the answer, or None when the request got no answer. HTTP 429 opens the
        breaker. Another answer after the end of a pause lets ``end_if_answered`` close the breaker.
        """
        notification = None
        with self._sessions.begin() as session:
            row, now = _locked_row(session, self._clock)
            row.last_answer_at = now
            if status == TOO_MANY_REQUESTS:
                _, notification = self._open(session, row, now, BreakerReason.HTTP_429)
            elif status is not None and _has_ended(row, now):
                self._answered_after_pause = True
        self._notify(notification)

    def end_if_answered(self) -> None:
        """Close a pause that ended and send the notification of the end, when a request of this budget got an answer
        after the end. The caller calls it when the call that sent the request ended without a limit of YouTube."""
        if not self._answered_after_pause:
            return
        self._answered_after_pause = False
        notification = None
        with self._sessions.begin() as session:
            row, now = _locked_row(session, self._clock)
            if _has_ended(row, now):
                row.breaker_reason = None
                notification = notify.request_breaker_closed()
                LOG.info("A request with cookies after the pause got an answer, so the breaker is closed")
        self._notify(notification)

    def open_breaker(self, reason: BreakerReason) -> BreakerOpen:
        """Open the breaker for ``reason``, unless it is open already. Return the error of the open breaker."""
        with self._sessions.begin() as session:
            row, now = _locked_row(session, self._clock)
            breaker_open, notification = self._open(session, row, now, reason)
        self._notify(notification)
        return breaker_open

    def paused(self) -> BreakerOpen | None:
        """Return the error of the open breaker, or None while the breaker is closed."""
        with self._sessions.begin() as session:
            row, now = _locked_row(session, self._clock)
            return self._breaker_open(session, row) if _is_open(row, now) else None

    def _open(
        self, session: Session, row: RequestBudgetState, now: datetime.datetime, reason: BreakerReason
    ) -> tuple[BreakerOpen, notify.Notification | None]:
        """Start a pause in ``row``, unless a pause is on. Return its error, and the notification of a new pause."""
        self._answered_after_pause = False
        if _is_open(row, now):
            return self._breaker_open(session, row), None
        zone = self._zone(session)
        level, length = next_pause(row, now, reason)
        until = now + length
        row.breaker_level = level
        row.breaker_reason = reason
        row.breaker_opened_at = now
        row.breaker_until = until
        breaker_open = BreakerOpen(RequestPause(reason, until, zone.key))
        LOG.warning("Pause %d of the requests with cookies: %s", level, breaker_open)
        return breaker_open, notify.request_breaker_opened(REASON_TEXTS[reason], length, until.astimezone(zone))

    def _breaker_open(self, session: Session, row: RequestBudgetState) -> BreakerOpen:
        """Return the error of the pause of ``row``, which has a reason and an end."""
        assert row.breaker_reason is not None and row.breaker_until is not None
        return BreakerOpen(RequestPause(row.breaker_reason, row.breaker_until, self._zone(session).key))

    def _zone(self, session: Session) -> ZoneInfo:
        """Return the time zone of the service, or UTC when its stored name is no known time zone."""
        name = stored_timezone(session, self._default_timezone)
        try:
            return ZoneInfo(name)
        # An unknown name raises ZoneInfoNotFoundError, a KeyError, and a name that is no file name raises
        # ValueError or OSError.
        except KeyError, ValueError, OSError:
            LOG.warning("The time zone %r is not known, so the texts of the breaker use UTC", name)
            return ZoneInfo("UTC")

    def _notify(self, notification: notify.Notification | None) -> None:
        if notification is None:
            return
        try:
            self._send(notification)
        except Exception as error:  # noqa: BLE001
            # A failed notification must never fail the request.
            LOG.warning("Cannot send %s: %s", notification.event.value, type(error).__name__)
