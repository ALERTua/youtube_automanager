"""The shared budget of the requests that carry the cookies of the account.

Each such request takes one token from a bucket of ``BUCKET_SIZE`` tokens. The bucket gets one new token each
``REFILL_S`` seconds, continuously, so a full bucket lets a short action go at once, and a long run gets at most one
request each ``REFILL_S`` seconds. A request without a token waits until one is there. From the end of one answer to
the start of the next request, the budget also keeps a random pause from ``PAUSE_S - PAUSE_JITTER_S`` to
``PAUSE_S + PAUSE_JITTER_S`` seconds. A request starts only when it has both the token and the pause.

The state lives in the one row of the table ``request_budget``, so the web process and each child that work with one
database share one budget. The times of the row come from the wall clock in UTC, because a monotonic clock counts
from another start in each process. A wall clock can go back. A time of the row that lies after the current time then
counts as the current time, so a change of the clock adds no tokens and never makes a wait longer than one token or
one pause.

The module never imports yt-dlp, so the web process can use it.
"""

import datetime
import logging
import time
from collections.abc import Callable
from random import uniform

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import RequestBudgetState, utc_now

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

# The read of the row locks it until the end of the transaction where the database has a lock of a row, as PostgreSQL
# does. SQLite has none, and the read, the change and the write of the row there go in one transaction.
ROW_QUERY = select(RequestBudgetState).where(RequestBudgetState.id == 1).with_for_update()

# The current time as a timezone-aware UTC datetime of the wall clock.
Clock = Callable[[], datetime.datetime]
# A sleep for the given number of seconds.
Sleep = Callable[[float], object]
# A random float from the first argument to the second, as ``random.uniform`` gives it.
Uniform = Callable[[float, float], float]


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
    can pass the ``uniform`` method of a seeded ``random.Random``.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Clock = utc_now,
        sleep: Sleep = time.sleep,
        random: Uniform = uniform,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._sleep = sleep
        self._random = random

    def before_request(self) -> None:
        """Wait for a token and for the pause after the last answer, then take the token.

        The wait sleeps outside the transaction. After the wait, the budget reads the row again, because another
        process can take a token or get an answer in the meantime.
        """
        pause = datetime.timedelta(seconds=self._random(PAUSE_S - PAUSE_JITTER_S, PAUSE_S + PAUSE_JITTER_S))
        while True:
            with self._sessions.begin() as session:
                row, now = _locked_row(session, self._clock)
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

    def after_answer(self) -> None:
        """Keep the end of an answer, so the pause before the next request counts from it, also in another process."""
        with self._sessions.begin() as session:
            row, now = _locked_row(session, self._clock)
            row.last_answer_at = now
