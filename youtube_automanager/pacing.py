"""The shared budget of the requests that carry the cookies of the account.

Each such request takes one token from a bucket of ``BUCKET_SIZE`` tokens. The bucket gets one new token each
``REFILL_S`` seconds, continuously, so a full bucket lets a short action go at once, and a long run gets at most one
request each ``REFILL_S`` seconds. A request without a token waits until one is there. From the end of one answer to
the start of the next request, the budget also keeps a random pause from ``PAUSE_S - PAUSE_JITTER_S`` to
``PAUSE_S + PAUSE_JITTER_S`` seconds. A request starts only when it has both the token and the pause.

The budget also has a breaker. When YouTube answers a request with cookies with HTTP 429, a "try again later" page or
a bot check, the breaker opens: each later request with cookies fails at once with ``RequestsPaused``, without the
network, until the pause ends. The request that hit the limit is not sent again. The first pause is
``FIRST_PAUSES`` of the reason. Each next pause doubles the pause before it, up to ``LONGEST_PAUSE``, and it is never
shorter than the first pause of its reason. After ``LEVEL_RESET`` without a new pause, the pauses start again from the
first length. The service sends one notification when the breaker opens, and one more when the first request after
the end of the pause gets an answer that does not open the breaker again.

The state lives in the one row of the table ``request_budget``, so the web process and each child that work with one
database share one budget, and a restart keeps the breaker. The times of the row come from the wall clock in UTC,
because a monotonic clock counts from another start in each process. A wall clock can go back. A time of the token
bucket that lies after the current time then counts as the current time, so a change of the clock adds no tokens and
never makes a wait longer than one token or one pause.

The module never imports yt-dlp, so the web process can use it.
"""

import datetime
import functools
import logging
import time
from collections.abc import Callable
from random import uniform
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager import notify
from youtube_automanager.db.models import BreakerReason, RequestBudgetState, utc_now
from youtube_automanager.request_pause import REASON_TEXTS, RequestPause
from youtube_automanager.scheduler import stored_timezone

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
# The first pause after each reason: yt-dlp says that a rate limit of YouTube lasts up to an hour, and a bot check
# needs a longer rest.
FIRST_PAUSES = {
    BreakerReason.HTTP_429: datetime.timedelta(hours=1),
    BreakerReason.TRY_AGAIN_LATER: datetime.timedelta(hours=1),
    BreakerReason.BOT_CHECK: datetime.timedelta(hours=6),
}
LONGEST_PAUSE = datetime.timedelta(hours=24)
# The time after the start of the last pause from which a new pause has the first length again.
LEVEL_RESET = datetime.timedelta(days=3)

# The read of the row locks it until the end of the transaction where the database has a lock of a row, as PostgreSQL
# does. SQLite has none, and the read, the change and the write of the row there go in one transaction.
ROW_QUERY = select(RequestBudgetState).where(RequestBudgetState.id == 1).with_for_update()

# The current time as a timezone-aware UTC datetime of the wall clock.
Clock = Callable[[], datetime.datetime]
# A sleep for the given number of seconds.
Sleep = Callable[[float], object]
# A random float from the first argument to the second, as ``random.uniform`` gives it.
Uniform = Callable[[float, float], float]
# A send of a notification.
Send = Callable[[notify.Notification], object]


class RequestsPaused(Exception):
    """The breaker is open: the service sends no request with the cookies of the account until ``until``.

    The text names the end of the pause in the time zone of the service, and the answer of YouTube that started it.
    ``pause`` holds the same facts for a child process that passes them to the web process.
    """

    def __init__(self, reason: BreakerReason, until: datetime.datetime, zone: ZoneInfo) -> None:
        self.reason = reason
        self.until = until
        self.pause = RequestPause(reason, until, zone.key)
        super().__init__(str(self.pause))


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
    """Return the level and the length of a pause that ``reason`` starts at ``now``, after the pauses of ``row``.

    The first pause, and the first pause after ``LEVEL_RESET`` from the start of the last pause, has the length of
    ``FIRST_PAUSES``. Each other pause doubles the last pause, up to ``LONGEST_PAUSE``, and it is never shorter than
    the first pause of ``reason``.
    """
    first = FIRST_PAUSES[reason]
    last_start, last_end = row.breaker_opened_at, row.breaker_until
    if row.breaker_level is None or last_start is None or last_end is None or now - last_start >= LEVEL_RESET:
        return 1, first
    return row.breaker_level + 1, min(LONGEST_PAUSE, max(first, 2 * (last_end - last_start)))


def _is_open(row: RequestBudgetState, now: datetime.datetime) -> bool:
    return row.breaker_reason is not None and row.breaker_until is not None and now < row.breaker_until


def _has_ended(row: RequestBudgetState, now: datetime.datetime) -> bool:
    """Return True when a pause ended, and no answer after it showed yet that the requests work again."""
    return row.breaker_reason is not None and row.breaker_until is not None and row.breaker_until <= now


def _locked_row(session: Session, clock: Clock) -> tuple[RequestBudgetState, datetime.datetime]:
    """Read and lock the row of the budget, and return it with the current time. Make the row with a full bucket at
    the first use.

    The clock goes after the lock, so the time is not older than the row when the lock waited for another process.
    """
    row = session.scalars(ROW_QUERY).one_or_none()
    now = clock()
    if row is None:
        row = RequestBudgetState(id=1, tokens=float(BUCKET_SIZE), refilled_at=now, last_answer_at=None)
        session.add(row)
    return row, now


class RequestBudget:
    """The budget of the requests with the cookies of the account, shared through the database.

    ``sessions`` makes the sessions of the database. ``clock`` gives the current UTC time of the wall clock, and
    ``sleep`` waits. ``random`` gives the pause before each request from the range of its two arguments, so a test
    can pass the ``uniform`` method of a seeded ``random.Random``. ``send`` sends the notifications of the breaker,
    by default to each target of the database. ``default_timezone`` is the time zone of the service when the database
    holds no schedule, for the end of a pause in a text.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Clock = utc_now,
        sleep: Sleep = time.sleep,
        random: Uniform = uniform,
        send: Send | None = None,
        default_timezone: str = "UTC",
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._sleep = sleep
        self._random = random
        self._send = send or functools.partial(notify.send_to_stored_targets, sessions)
        self._default_timezone = default_timezone

    def before_request(self) -> None:
        """Wait for a token and for the pause after the last answer, then take the token.

        Raise ``RequestsPaused`` at once while the breaker is open. The wait sleeps outside the transaction. After the
        wait, the budget reads the row again, because another process can take a token, get an answer or open the
        breaker in the meantime.
        """
        pause = datetime.timedelta(seconds=self._random(PAUSE_S - PAUSE_JITTER_S, PAUSE_S + PAUSE_JITTER_S))
        while True:
            with self._sessions.begin() as session:
                row, now = _locked_row(session, self._clock)
                if _is_open(row, now):
                    raise self._paused(session, row)
                tokens = refilled_tokens(row.tokens, row.refilled_at, now)
                row.tokens = tokens
                row.refilled_at = now
                if row.last_answer_at is not None and row.last_answer_at > now:
                    row.last_answer_at = now
                # A wait is a whole number of microseconds, as each time of the database is. After a wait for a token,
                # a rounding error of a float can leave the bucket a little below 1, and the next wait then rounds
                # to no wait, so the request goes.
                token_wait = NO_WAIT
                if tokens < 1:
                    token_wait = datetime.timedelta(seconds=(1 - tokens) * REFILL_S)
                pause_wait = NO_WAIT
                if row.last_answer_at is not None:
                    pause_wait = row.last_answer_at + pause - now
                wait = max(token_wait, pause_wait)
                if wait <= NO_WAIT:
                    row.tokens = max(0.0, tokens - 1)
                    return
            if token_wait > NO_WAIT:
                LOG.info(
                    "A request with the cookies of the account waits %.2f s for a token of the budget",
                    wait.total_seconds(),
                )
            self._sleep(wait.total_seconds())

    def after_answer(self, status: int | None = None) -> None:
        """Keep the end of an answer, so the pause before the next request counts from it, also in another process.

        ``status`` is the HTTP status of the answer, or None when the request got no answer. HTTP 429 opens the
        breaker. Another answer after the end of a pause closes the breaker and sends the notification of the end.
        """
        notification = None
        with self._sessions.begin() as session:
            row, now = _locked_row(session, self._clock)
            row.last_answer_at = now
            if status == TOO_MANY_REQUESTS:
                _, notification = self._open(session, row, now, BreakerReason.HTTP_429)
            elif status is not None and _has_ended(row, now):
                row.breaker_reason = None
                notification = notify.request_breaker_closed()
                LOG.info("The first request with cookies after the pause got an answer, so the breaker is closed")
        self._notify(notification)

    def open_breaker(self, reason: BreakerReason) -> RequestsPaused:
        """Open the breaker for ``reason``, unless it is open already. Return the error of the open breaker."""
        with self._sessions.begin() as session:
            row, now = _locked_row(session, self._clock)
            paused, notification = self._open(session, row, now, reason)
        self._notify(notification)
        return paused

    def paused(self) -> RequestsPaused | None:
        """Return the error of the open breaker, or None while the breaker is closed."""
        with self._sessions() as session:
            row = session.get(RequestBudgetState, 1)
            if row is None or not _is_open(row, self._clock()):
                return None
            return self._paused(session, row)

    def _open(
        self, session: Session, row: RequestBudgetState, now: datetime.datetime, reason: BreakerReason
    ) -> tuple[RequestsPaused, notify.Notification | None]:
        """Start a pause in ``row``, unless a pause is on. Return its error, and the notification of a new pause."""
        if _is_open(row, now):
            return self._paused(session, row), None
        level, pause = next_pause(row, now, reason)
        until = now + pause
        row.breaker_level = level
        row.breaker_reason = reason
        row.breaker_opened_at = now
        row.breaker_until = until
        zone = self._zone(session)
        paused = RequestsPaused(reason, until, zone)
        LOG.warning("Pause %d of the requests with cookies: %s", level, paused)
        return paused, notify.request_breaker_opened(REASON_TEXTS[reason], pause, until.astimezone(zone))

    def _paused(self, session: Session, row: RequestBudgetState) -> RequestsPaused:
        """Return the error of the pause of ``row``, which has a reason and an end."""
        assert row.breaker_reason is not None and row.breaker_until is not None
        return RequestsPaused(row.breaker_reason, row.breaker_until, self._zone(session))

    def _zone(self, session: Session) -> ZoneInfo:
        return ZoneInfo(stored_timezone(session, self._default_timezone))

    def _notify(self, notification: notify.Notification | None) -> None:
        if notification is None:
            return
        try:
            self._send(notification)
        except Exception as error:  # noqa: BLE001
            # A failed notification must never fail the request.
            LOG.warning("Cannot send %s: %s", notification.event.value, type(error).__name__)
