import datetime
import logging
import random

import pytest
from sqlalchemy.dialects import postgresql

from youtube_automanager import pacing
from youtube_automanager.db import make_engine, make_session_factory
from youtube_automanager.db.models import RequestBudgetState
from youtube_automanager.pacing import BUCKET_SIZE, RequestBudget, minimum_seconds, refilled_tokens

START = datetime.datetime(2026, 10, 28, 12, 0, tzinfo=datetime.UTC)


def at(seconds: float) -> datetime.datetime:
    return START + datetime.timedelta(seconds=seconds)


class FakeTime:
    """A UTC wall clock and a sleep that only moves the clock. ``sleeps`` holds each wait in seconds.

    ``during_sleep`` runs after the clock moved, as another process that works while the budget sleeps.
    """

    def __init__(self):
        self.now = START
        self.sleeps = []
        self.during_sleep = None

    def clock(self) -> datetime.datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        # A budget that never stops waiting fails the test instead of a hang.
        assert len(self.sleeps) < 50, f"the budget keeps waiting: {self.sleeps[:5]}"
        self.sleeps.append(seconds)
        self.now += datetime.timedelta(seconds=seconds)
        if self.during_sleep is not None:
            action, self.during_sleep = self.during_sleep, None
            action()

    def advance(self, seconds: float) -> None:
        self.now += datetime.timedelta(seconds=seconds)


def fixed_pause(seconds: float):
    """A random source that always gives the same pause."""
    return lambda low, high: seconds


@pytest.fixture
def fake_time():
    return FakeTime()


def make_budget(sessions, fake_time, pause=0.0):
    return RequestBudget(sessions, clock=fake_time.clock, sleep=fake_time.sleep, random=fixed_pause(pause))


def store(sessions, tokens, refilled_at, last_answer_at=None):
    with sessions.begin() as session:
        session.merge(RequestBudgetState(id=1, tokens=tokens, refilled_at=refilled_at, last_answer_at=last_answer_at))


def stored(sessions) -> RequestBudgetState | None:
    with sessions() as session:
        return session.get(RequestBudgetState, 1)


# The refill.


@pytest.mark.parametrize(
    ("tokens", "elapsed_s", "expected"),
    [
        (0.0, 0.0, 0.0),
        (0.0, 2.0, 0.5),
        (0.5, 1.0, 0.75),
        (0.0, 4.0, 1.0),
        # The cap of the bucket: just below it, at it and just above it.
        (14.0, 3.996, 14.999),
        (14.0, 4.0, 15.0),
        (14.0, 4.004, 15.0),
        (0.0, 3600.0, 15.0),
        # The clock went back: no tokens come.
        (3.0, -3600.0, 3.0),
        (3.0, -0.001, 3.0),
    ],
)
def test_the_bucket_gets_one_token_each_4_s_up_to_15(tokens, elapsed_s, expected):
    assert refilled_tokens(tokens, START, at(elapsed_s)) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("requests", "expected"),
    [
        (0, 0.0),
        (1, 0.0),
        # Inside the bucket only the shortest pause of 0.25 s after each answer counts.
        (2, 0.25),
        (15, 3.5),
        # From the 16th request, the wait for a new token each 4 s counts.
        (16, 4.0),
        (200, 740.0),
    ],
)
def test_the_shortest_time_of_the_requests_comes_from_the_pause_and_the_bucket(requests, expected):
    assert minimum_seconds(requests) == pytest.approx(expected)


def test_the_fastest_run_of_a_budget_takes_the_shortest_time(sessions, fake_time):
    # Each answer comes at once, and each pause is the shortest that the random pause gives.
    budget = make_budget(sessions, fake_time, pause=pacing.PAUSE_S - pacing.PAUSE_JITTER_S)
    for _ in range(40):
        budget.before_request()
        budget.after_answer()
    assert (fake_time.now - START).total_seconds() == pytest.approx(minimum_seconds(40))


@pytest.mark.parametrize(
    ("tokens", "elapsed_s", "waits"),
    [
        # An empty bucket that got half a token waits for the other half.
        (0.0, 2.0, [2.0]),
        (0.5, 1.0, [1.0]),
        # A bucket with a token after the refill goes at once.
        (0.0, 4.0, []),
        (14.0, 3600.0, []),
    ],
)
def test_a_request_without_a_token_waits_for_the_rest_of_one_token(db_sessions, fake_time, tokens, elapsed_s, waits):
    store(db_sessions, tokens, at(-elapsed_s))

    make_budget(db_sessions, fake_time).before_request()

    assert fake_time.sleeps == waits
    row = stored(db_sessions)
    expected_tokens = min(BUCKET_SIZE, tokens + elapsed_s / 4) - 1
    assert row.tokens == pytest.approx(max(0.0, expected_tokens), abs=1e-6)
    assert row.refilled_at == fake_time.now


def test_the_cap_of_15_holds_after_a_long_quiet_time(db_sessions, fake_time):
    store(db_sessions, 14.0, at(-3600))
    budget = make_budget(db_sessions, fake_time)

    for _ in range(15):
        budget.before_request()

    assert fake_time.sleeps == []
    assert stored(db_sessions).tokens == pytest.approx(0.0)


def test_a_clock_that_went_back_adds_no_tokens_and_waits_for_one_token_only(db_sessions, fake_time):
    store(db_sessions, 0.0, at(3600))

    make_budget(db_sessions, fake_time).before_request()

    assert fake_time.sleeps == [4.0]
    row = stored(db_sessions)
    assert row.refilled_at == at(4)
    assert row.tokens == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize(
    ("tokens", "waits"),
    [
        # The rest of the token takes less than half a microsecond, so the wait rounds to none.
        (1 - 1e-12, []),
        # The rest takes 1.4 microseconds, so the wait rounds to 1, and the refill after it stays a little below 1.
        (1 - 0.35e-6, [1e-06]),
    ],
)
def test_a_bucket_a_little_below_one_token_waits_whole_microseconds_and_goes(db_sessions, fake_time, tokens, waits):
    store(db_sessions, tokens, START)

    make_budget(db_sessions, fake_time).before_request()

    assert fake_time.sleeps == waits
    assert stored(db_sessions).tokens == 0.0


def test_a_last_answer_after_the_current_time_keeps_only_one_pause(db_sessions, fake_time):
    store(db_sessions, 15.0, START, last_answer_at=at(3600))

    make_budget(db_sessions, fake_time, pause=0.5).before_request()

    assert fake_time.sleeps == [0.5]


def test_15_requests_go_without_a_wait_and_the_16th_waits_for_the_rest_of_4_s(db_sessions, fake_time, caplog):
    budget = make_budget(db_sessions, fake_time)
    for _ in range(15):
        budget.before_request()
    assert fake_time.sleeps == []
    fake_time.advance(1)

    with caplog.at_level(logging.INFO, logger="youtube_automanager.pacing"):
        budget.before_request()

    assert fake_time.sleeps == [3.0]
    assert "waits 3.00 s for a token" in caplog.text
    budget.before_request()
    assert fake_time.sleeps == [3.0, 4.0]


# The row.


def test_the_first_request_makes_the_row_with_a_full_bucket_and_takes_one_token(db_sessions, fake_time):
    assert stored(db_sessions) is None

    make_budget(db_sessions, fake_time).before_request()

    row = stored(db_sessions)
    assert (row.id, row.tokens, row.refilled_at, row.last_answer_at) == (1, BUCKET_SIZE - 1, START, None)
    assert fake_time.sleeps == []


def test_the_first_answer_makes_the_row_with_a_full_bucket(db_sessions, fake_time):
    make_budget(db_sessions, fake_time).after_answer()

    row = stored(db_sessions)
    assert (row.tokens, row.refilled_at, row.last_answer_at) == (BUCKET_SIZE, START, START)


def test_the_read_of_the_row_locks_it_on_postgresql():
    assert "FOR UPDATE" in str(pacing.ROW_QUERY.compile(dialect=postgresql.dialect()))


# The pause.


def test_the_pause_after_an_answer_is_a_random_time_from_0_25_to_1_25_s(db_sessions, fake_time):
    budget = RequestBudget(db_sessions, clock=fake_time.clock, sleep=fake_time.sleep, random=random.Random(44).uniform)
    expected = random.Random(44)
    pauses = [datetime.timedelta(seconds=expected.uniform(0.25, 1.25)).total_seconds() for _ in range(12)]

    for _ in range(12):
        budget.before_request()
        budget.after_answer()

    # The first request has no answer before it, so its pause does not count.
    assert fake_time.sleeps == pauses[1:]
    assert all(0.25 <= pause <= 1.25 for pause in fake_time.sleeps)
    assert len(set(fake_time.sleeps)) > 1


@pytest.mark.parametrize(("pick", "pause"), [(min, 0.25), (max, 1.25)])
def test_the_pause_range_has_its_ends_at_0_25_and_1_25_s(db_sessions, fake_time, pick, pause):
    budget = RequestBudget(
        db_sessions, clock=fake_time.clock, sleep=fake_time.sleep, random=lambda low, high: pick(low, high)
    )
    budget.after_answer()

    budget.before_request()

    assert fake_time.sleeps == [pause]


def test_the_pause_counts_from_the_end_of_the_answer(db_sessions, fake_time):
    budget = make_budget(db_sessions, fake_time, pause=1.0)
    budget.before_request()
    fake_time.advance(5)
    budget.after_answer()
    fake_time.advance(0.25)

    budget.before_request()

    assert fake_time.sleeps == [0.75]


def test_the_pause_counts_from_the_answer_of_another_process_on_the_same_database(db_url, fake_time):
    engines = [make_engine(db_url), make_engine(db_url)]
    try:
        first, second = (make_budget(make_session_factory(engine), fake_time, pause=1.0) for engine in engines)
        first.before_request()
        first.after_answer()
        fake_time.advance(0.3)

        second.before_request()

        assert fake_time.sleeps == [0.7]
        fake_time.advance(2)
        second.before_request()
        assert fake_time.sleeps == [0.7]
    finally:
        for engine in engines:
            engine.dispose()


def test_a_wait_for_a_token_also_keeps_the_pause_and_does_not_add_it(db_sessions, fake_time):
    store(db_sessions, 0.0, START, last_answer_at=START)

    make_budget(db_sessions, fake_time, pause=1.0).before_request()

    assert fake_time.sleeps == [4.0]


def test_after_the_wait_the_budget_reads_the_row_again(db_sessions, fake_time):
    store(db_sessions, 0.0, START)
    other_process = make_budget(db_sessions, fake_time)
    # Another process gets an answer at the end of the wait for the token.
    fake_time.during_sleep = other_process.after_answer

    make_budget(db_sessions, fake_time, pause=0.5).before_request()

    assert fake_time.sleeps == [4.0, 0.5]
