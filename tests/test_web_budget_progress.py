"""The state of the request budget in the progress block of a bulk action: the math, the texts and the read."""

import datetime
import sqlite3
import threading
import time

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from youtube_automanager import pacing
from youtube_automanager.db import make_engine, make_session_factory
from youtube_automanager.db.models import BreakerReason, RequestBudgetState, Schedule, ScheduleKind
from youtube_automanager.i18n import set_language
from youtube_automanager.pacing import BUCKET_SIZE, RequestBudget
from youtube_automanager.request_pause import RequestPause
from youtube_automanager.web.ui import budget_progress
from youtube_automanager.web.ui.budget_progress import (
    BudgetView,
    budget_view,
    clock_text,
    next_wait_seconds,
    pause_left_text,
    pause_until_text,
    read_budget,
    remaining_seconds,
    remaining_text,
    tokens_text,
    wait_text,
)
from youtube_automanager.web.ui.rule_editor import REQUESTS_PER_ADD, apply_progress_text

START = datetime.datetime(2026, 10, 28, 12, 0, tzinfo=datetime.UTC)
ZONE = "Europe/Kyiv"


def at(seconds: float) -> datetime.datetime:
    return START + datetime.timedelta(seconds=seconds)


def budget_row(tokens: float, refilled_at: datetime.datetime = START, **breaker) -> RequestBudgetState:
    return RequestBudgetState(id=1, tokens=tokens, refilled_at=refilled_at, **breaker)


def store_row(sessions, row: RequestBudgetState) -> None:
    with sessions.begin() as session:
        session.merge(row)


def writer_of(settings) -> sqlite3.Connection:
    """Return a connection to the database of the tests that controls its own transactions and never waits for a
    lock, as a batch child that would fail at once when the page held a lock."""
    return sqlite3.connect(settings.home / "service.sqlite", timeout=0, isolation_level=None)


def row_count(sessions) -> int:
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(RequestBudgetState))


# The tokens of the bucket at a given clock.


@pytest.mark.parametrize(
    ("tokens", "seconds", "expected"),
    [
        # An empty bucket at the time of its last refill.
        (0.0, 0, 0.0),
        # A partial bucket: one token each 4 s.
        (0.0, 6, 1.5),
        (5.0, 9.6, 7.4),
        # A full bucket stays full.
        (15.0, 0, 15.0),
        (15.0, 60, 15.0),
        # The bucket never holds more than 15 tokens.
        (0.0, 3600, 15.0),
        (14.0, 8, 15.0),
        # A clock that went back adds no tokens.
        (3.0, -60, 3.0),
    ],
)
def test_the_tokens_of_the_block_are_the_tokens_of_the_bucket_at_the_clock(tokens, seconds, expected):
    view = budget_view(budget_row(tokens), ZONE, at(seconds))

    assert view.tokens == pytest.approx(expected)
    assert view.pause is None


def test_a_missing_row_is_a_full_bucket_without_a_pause():
    view = budget_view(None, ZONE, START)

    assert view == BudgetView(float(BUCKET_SIZE), None, None, START)
    assert view.next_wait == 0


@pytest.mark.parametrize(
    ("tokens", "wait"),
    [(0.0, 4.0), (0.6, 1.6), (0.99, 0.04), (1.0, 0.0), (7.4, 0.0), (15.0, 0.0)],
)
def test_the_next_request_waits_for_the_rest_of_one_token(tokens, wait):
    assert next_wait_seconds(tokens) == pytest.approx(wait)


@pytest.mark.parametrize(
    ("requests", "tokens", "seconds"),
    [
        # The first state of the mockup: 14 channels left and 7.4 tokens.
        (14, 7.4, 26.4),
        # An empty bucket: each request waits 4 s.
        (6, 0.0, 24.0),
        (6, 0.6, 21.6),
        # The tokens cover each request.
        (3, 7.0, 0.0),
        (0, 0.0, 0.0),
    ],
)
def test_the_remaining_time_is_the_wait_for_the_tokens_that_the_bucket_lacks(requests, tokens, seconds):
    assert remaining_seconds(requests, tokens) == pytest.approx(seconds)


def test_the_remaining_time_of_a_bulk_change_counts_one_request_for_each_channel_left():
    total, done = 20, 6
    assert remaining_text(remaining_seconds(total - done, 7.4)) == "at least ~0:27 left"


def test_the_remaining_time_of_an_apply_counts_two_requests_for_each_video_left():
    count, added, tokens = 10, 3, 2.0
    assert REQUESTS_PER_ADD == 2
    assert remaining_seconds((count - added) * REQUESTS_PER_ADD, tokens) == pytest.approx(48.0)
    assert apply_progress_text(added, count) == "Adding videos: 3 of 10"


def test_the_texts_of_the_budget():
    assert clock_text(0) == "0:00"
    assert clock_text(26.4) == "0:27"
    assert clock_text(60) == "1:00"
    assert clock_text(3725) == "62:05"
    assert tokens_text(7.4) == "7.4 of 15"
    assert tokens_text(15) == "15.0 of 15"
    assert wait_text(0) == "at once"
    assert wait_text(1.6) == "in 1.6 s"
    assert pause_left_text(55 * 60) == "55 min left"
    assert pause_left_text(10) == "1 min left"
    assert pause_left_text(60 * 60) == "1 h 0 min left"
    assert pause_left_text(6 * 3600 - 30) == "6 h 0 min left"
    assert pause_left_text(90 * 60 + 1) == "1 h 31 min left"


# The pause.


def open_breaker(until: datetime.datetime, opened_at: datetime.datetime = START, reason=BreakerReason.HTTP_429):
    return {"breaker_reason": reason, "breaker_until": until, "breaker_opened_at": opened_at, "breaker_level": 1}


def test_an_open_breaker_is_a_pause_with_its_end_its_start_and_the_time_zone_of_the_service():
    row = budget_row(3.0, **open_breaker(at(3600)))

    view = budget_view(row, ZONE, at(300))

    assert view.pause == RequestPause(BreakerReason.HTTP_429, at(3600), ZONE)
    assert view.pause_started == START
    assert pause_until_text(view) == "Pause of requests until {time} (55 min left)"


def test_a_pause_that_ended_or_closed_is_no_pause():
    ended = budget_row(3.0, **open_breaker(at(3600)))
    assert budget_view(ended, ZONE, at(3600)).pause is None
    closed = budget_row(3.0, **open_breaker(at(3600)))
    closed.breaker_reason = None
    assert budget_view(closed, ZONE, at(60)).pause is None


def test_a_pause_that_starts_after_the_clock_counts_from_the_clock():
    # The clock went back 2 h after the start of a pause of 1 h.
    row = budget_row(3.0, **open_breaker(at(3 * 3600), opened_at=at(2 * 3600)))

    view = budget_view(row, ZONE, START)

    assert (view.pause_started, view.pause.until) == (START, at(3600))


def test_an_unknown_time_zone_names_the_end_of_a_pause_in_utc():
    row = budget_row(3.0, **open_breaker(at(3600)))
    assert budget_view(row, "Mars/Olympus", START).pause.zone == "UTC"


# The read of the row.


def test_the_read_gives_the_stored_row_and_the_time_zone_of_the_schedule(sessions):
    store_row(sessions, budget_row(0.6, refilled_at=at(0), **open_breaker(at(3600))))
    with sessions.begin() as session:
        session.add(Schedule(id=1, enabled=True, kind=ScheduleKind.CRON, timezone="America/New_York"))

    view = read_budget(sessions, ZONE, clock=lambda: at(2))

    assert view.tokens == pytest.approx(1.1)
    assert view.pause == RequestPause(BreakerReason.HTTP_429, at(3600), "America/New_York")


def test_the_read_of_a_missing_row_gives_a_full_bucket_and_writes_no_row(sessions):
    view = read_budget(sessions, ZONE, clock=lambda: START)

    assert (view.tokens, view.pause) == (float(BUCKET_SIZE), None)
    assert row_count(sessions) == 0


def test_the_read_sends_only_a_select(sessions):
    store_row(sessions, budget_row(3.0))
    statements = []
    engine = sessions.kw["bind"]

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        read_budget(sessions, ZONE)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert not any("FOR UPDATE" in statement.upper() for statement in statements)


def test_the_read_does_not_wait_for_a_writer_that_holds_the_write_lock(sessions, settings):
    """A batch child holds the write lock of the database while it changes the row. The read of the page goes on at
    once with the last committed row, and the writer commits without a wait for the page."""
    store_row(sessions, budget_row(3.0, refilled_at=at(0)))
    # The writer waits for no lock: it fails at once when the page holds one.
    writer = writer_of(settings)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE request_budget SET tokens = 1.0 WHERE id = 1")

        started = time.monotonic()
        view = read_budget(sessions, ZONE, clock=lambda: START)
        assert time.monotonic() - started < 1
        assert view.tokens == pytest.approx(3.0)

        writer.execute("COMMIT")
        # After the read, the page holds no lock, so the writer takes the exclusive lock at once.
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("COMMIT")
    finally:
        writer.close()
    assert read_budget(sessions, ZONE, clock=lambda: START).tokens == pytest.approx(1.0)


def test_a_locked_read_of_the_budget_would_wait_for_the_writer(sessions, settings):
    """The contrast of the test above: the read of ``pacing`` takes the write lock, so it fails against a writer when it
    may not wait. The page therefore never uses it."""
    store_row(sessions, budget_row(3.0, refilled_at=at(0)))
    engine = make_engine(settings.database_url)
    impatient = make_session_factory(engine)
    writer = writer_of(settings)
    try:
        writer.execute("BEGIN IMMEDIATE")

        @event.listens_for(engine, "connect")
        def no_wait(dbapi_connection, connection_record):
            dbapi_connection.execute("PRAGMA busy_timeout = 0")

        with pytest.raises(OperationalError, match="locked"), impatient.begin() as session:
            pacing._locked_row(session, lambda: START)
        assert read_budget(impatient, ZONE, clock=lambda: START).tokens == pytest.approx(3.0)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
        engine.dispose()


def test_the_reads_of_the_page_do_not_slow_down_the_requests_of_a_batch(sessions):
    """A batch takes a token for each request while the page reads the row many times. Each take still ends fast."""
    store_row(sessions, budget_row(float(BUCKET_SIZE), refilled_at=datetime.datetime.now(datetime.UTC)))
    budget = RequestBudget(sessions, sleep=lambda seconds: None, random=lambda low, high: 0.0, send=lambda note: None)
    done = threading.Event()
    reads = []
    errors = []

    def page() -> None:
        try:
            while not done.is_set():
                reads.append(read_budget(sessions, ZONE).tokens)
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    reader = threading.Thread(target=page)
    reader.start()
    try:
        slowest = 0.0
        for _request in range(10):
            started = time.monotonic()
            budget.before_request()
            budget.after_answer(200)
            slowest = max(slowest, time.monotonic() - started)
    finally:
        done.set()
        reader.join()

    assert errors == []
    assert reads
    assert slowest < 1
    assert read_budget(sessions, ZONE).tokens == pytest.approx(BUCKET_SIZE - 10, abs=0.5)


def test_the_texts_of_the_budget_have_a_ukrainian_translation(real_uk_catalog):
    set_language("uk")

    assert tokens_text(7.4) == "7,4 з 15"
    assert wait_text(0) == "одразу"
    assert wait_text(1.6) == "через 1,6 с"
    assert remaining_text(26.4) == "лишилось щонайменше ~0:27"
    assert pause_left_text(55 * 60) == "ще 55 хв"
    assert pause_left_text(90 * 60) == "ще 1 год 30 хв"
    view = budget_view(budget_row(3.0, **open_breaker(at(3600))), ZONE, at(300))
    assert pause_until_text(view) == "Пауза запитів до {time} (ще 55 хв)"
    assert apply_progress_text(1, 2) == "Додавання відео: 1 з 2"
    assert budget_progress.decimal_text(0.5) == "0,5"
