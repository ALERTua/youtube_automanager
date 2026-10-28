import datetime
import logging
import random

import pytest
from sqlalchemy.dialects import postgresql

from youtube_automanager import notify, pacing
from youtube_automanager.db import make_engine, make_session_factory
from youtube_automanager.db.models import BreakerReason, RequestBudgetState, Schedule, ScheduleKind
from youtube_automanager.notify import Event
from youtube_automanager.pacing import BUCKET_SIZE, BreakerOpen, RequestBudget, minimum_seconds, refilled_tokens

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


# The breaker.

HOUR = datetime.timedelta(hours=1)
OPENED_TITLE = "YouTube AutoManager: the requests with cookies are paused"
CLOSED_TITLE = "YouTube AutoManager: the requests with cookies work again"


class Sent(list):
    """The notifications of the breaker, in the order of the sends."""

    def titles(self) -> list[str]:
        return [notification.title for notification in self]


@pytest.fixture
def sent():
    return Sent()


def breaker_budget(sessions, fake_time, sent, default_timezone="UTC"):
    return RequestBudget(
        sessions,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
        random=fixed_pause(0.0),
        send=sent.append,
        default_timezone=default_timezone,
    )


def pauses_of_openings(budget, fake_time, reasons) -> list[float]:
    """Open the breaker for each reason at the end of the pause before, and return each pause in hours."""
    pauses = []
    for reason in reasons:
        paused = budget.open_breaker(reason)
        pauses.append((paused.pause.until - fake_time.now) / HOUR)
        fake_time.now = paused.pause.until
    return pauses


@pytest.mark.parametrize(
    ("reason", "hours"),
    [(BreakerReason.HTTP_429, 1), (BreakerReason.TRY_AGAIN_LATER, 1), (BreakerReason.BOT_CHECK, 6)],
)
def test_each_reason_opens_the_breaker_with_its_first_pause(db_sessions, fake_time, sent, reason, hours):
    paused = breaker_budget(db_sessions, fake_time, sent).open_breaker(reason)

    assert (paused.pause.reason, paused.pause.until) == (reason, START + hours * HOUR)
    row = stored(db_sessions)
    assert (row.breaker_reason, row.breaker_level) == (reason, 1)
    assert (row.breaker_opened_at, row.breaker_until) == (START, START + hours * HOUR)


def test_an_answer_with_http_429_opens_the_breaker_for_1_hour(db_sessions, fake_time, sent):
    budget = breaker_budget(db_sessions, fake_time, sent)
    budget.before_request()

    budget.after_answer(429)

    row = stored(db_sessions)
    assert (row.breaker_reason, row.breaker_until, row.last_answer_at) == (BreakerReason.HTTP_429, START + HOUR, START)


@pytest.mark.parametrize("status", [200, 401, 403, 500, None])
def test_another_answer_or_no_answer_leaves_the_breaker_closed(db_sessions, fake_time, sent, status):
    budget = breaker_budget(db_sessions, fake_time, sent)
    budget.before_request()

    budget.after_answer(status)

    row = stored(db_sessions)
    assert (row.breaker_reason, row.breaker_until, row.breaker_level) == (None, None, None)
    assert sent == []


def test_a_request_during_the_pause_fails_at_once_with_the_end_and_takes_no_token(db_sessions, fake_time, sent):
    budget = breaker_budget(db_sessions, fake_time, sent)
    budget.open_breaker(BreakerReason.HTTP_429)
    tokens = stored(db_sessions).tokens
    fake_time.now = START + HOUR - datetime.timedelta(microseconds=1)

    with pytest.raises(BreakerOpen) as raised:
        budget.before_request()

    assert str(raised.value) == (
        "The service sends no request with the cookies of the account until 2026-10-28 13:00 UTC, because YouTube "
        "answered a request with HTTP 429 (too many requests)."
    )
    assert (raised.value.pause.reason, raised.value.pause.until) == (BreakerReason.HTTP_429, START + HOUR)
    assert fake_time.sleeps == []
    assert stored(db_sessions).tokens == tokens


def test_the_first_request_at_the_end_of_the_pause_goes(db_sessions, fake_time, sent):
    budget = breaker_budget(db_sessions, fake_time, sent)
    budget.open_breaker(BreakerReason.TRY_AGAIN_LATER)
    fake_time.now = START + HOUR

    budget.before_request()

    assert stored(db_sessions).tokens == BUCKET_SIZE - 1


def test_each_next_pause_of_http_429_doubles_the_pause_before_up_to_24_hours(db_sessions, fake_time, sent):
    budget = breaker_budget(db_sessions, fake_time, sent)

    pauses = pauses_of_openings(budget, fake_time, [BreakerReason.HTTP_429] * 7)

    assert pauses == [1, 2, 4, 8, 16, 24, 24]
    assert stored(db_sessions).breaker_level == 7


def test_each_next_pause_of_a_bot_check_doubles_6_hours_up_to_24_hours(db_sessions, fake_time, sent):
    budget = breaker_budget(db_sessions, fake_time, sent)

    assert pauses_of_openings(budget, fake_time, [BreakerReason.BOT_CHECK] * 4) == [6, 12, 24, 24]


def test_a_next_pause_doubles_the_pause_before_and_is_never_shorter_than_the_first_pause_of_its_reason(
    db_sessions, fake_time, sent
):
    budget = breaker_budget(db_sessions, fake_time, sent)
    reasons = [BreakerReason.HTTP_429, BreakerReason.BOT_CHECK, BreakerReason.TRY_AGAIN_LATER]

    assert pauses_of_openings(budget, fake_time, reasons) == [1, 6, 12]


@pytest.mark.parametrize(
    ("after_opening", "hours", "level"),
    [
        (datetime.timedelta(days=3) - datetime.timedelta(microseconds=1), 2, 2),
        (datetime.timedelta(days=3), 1, 1),
        (datetime.timedelta(days=30), 1, 1),
    ],
)
def test_the_pause_goes_back_to_the_first_length_3_days_after_the_last_opening(
    db_sessions, fake_time, sent, after_opening, hours, level
):
    budget = breaker_budget(db_sessions, fake_time, sent)
    budget.open_breaker(BreakerReason.HTTP_429)
    fake_time.now = START + after_opening

    paused = budget.open_breaker(BreakerReason.HTTP_429)

    assert paused.pause.until - fake_time.now == hours * HOUR
    assert stored(db_sessions).breaker_level == level


def test_an_opening_during_the_pause_changes_nothing_and_sends_nothing(db_sessions, fake_time, sent):
    budget = breaker_budget(db_sessions, fake_time, sent)
    budget.open_breaker(BreakerReason.HTTP_429)
    fake_time.advance(600)

    paused = budget.open_breaker(BreakerReason.BOT_CHECK)

    assert (paused.pause.reason, paused.pause.until) == (BreakerReason.HTTP_429, START + HOUR)
    row = stored(db_sessions)
    assert (row.breaker_reason, row.breaker_level, row.breaker_until) == (BreakerReason.HTTP_429, 1, START + HOUR)
    assert len(sent) == 1


def test_a_restart_keeps_the_open_breaker(db_url, fake_time, sent):
    engine = make_engine(db_url)
    try:
        breaker_budget(make_session_factory(engine), fake_time, sent).open_breaker(BreakerReason.BOT_CHECK)
    finally:
        engine.dispose()
    fake_time.advance(3600)

    engine = make_engine(db_url)
    try:
        with pytest.raises(BreakerOpen) as raised:
            breaker_budget(make_session_factory(engine), fake_time, sent).before_request()
    finally:
        engine.dispose()

    assert (raised.value.pause.reason, raised.value.pause.until) == (BreakerReason.BOT_CHECK, START + 6 * HOUR)


def test_a_breaker_that_another_process_opens_during_the_wait_stops_the_request(db_sessions, fake_time, sent):
    store(db_sessions, 0.0, START)
    other_process = breaker_budget(db_sessions, fake_time, sent)
    fake_time.during_sleep = lambda: other_process.open_breaker(BreakerReason.HTTP_429)

    with pytest.raises(BreakerOpen):
        breaker_budget(db_sessions, fake_time, sent).before_request()

    assert fake_time.sleeps == [4.0]


def test_the_opening_sends_one_notification_with_the_end_in_the_time_zone_of_the_service(db_sessions, fake_time, sent):
    budget = breaker_budget(db_sessions, fake_time, sent, default_timezone="Europe/Kyiv")

    budget.after_answer(429)
    budget.open_breaker(BreakerReason.HTTP_429)

    assert sent.titles() == [OPENED_TITLE]
    assert sent[0].event is Event.REQUEST_BREAKER
    assert "with HTTP 429 (too many requests). " in sent[0].body
    assert " for 1 hour, until 2026-10-28 15:00 EET. " in sent[0].body


def test_the_time_zone_of_the_schedule_is_the_time_zone_of_the_service(db_sessions, fake_time, sent):
    with db_sessions.begin() as session:
        session.add(Schedule(id=1, enabled=True, kind=ScheduleKind.CRON, timezone="America/New_York"))
    budget = breaker_budget(db_sessions, fake_time, sent, default_timezone="Europe/Kyiv")

    paused = budget.open_breaker(BreakerReason.BOT_CHECK)

    assert " until 2026-10-28 14:00 EDT. " in sent[0].body
    assert "until 2026-10-28 14:00 EDT," in str(paused)


def test_an_answer_after_the_pause_lets_the_end_of_the_call_close_the_breaker_once(db_sessions, fake_time, sent):
    budget = breaker_budget(db_sessions, fake_time, sent)
    budget.open_breaker(BreakerReason.HTTP_429)
    fake_time.now = START + HOUR

    budget.before_request()
    budget.after_answer(200)
    # The call that sent the request still runs, so the answer alone closes nothing.
    assert sent.titles() == [OPENED_TITLE]
    budget.end_if_answered()
    budget.before_request()
    budget.after_answer(404)
    budget.end_if_answered()

    assert sent.titles() == [OPENED_TITLE, CLOSED_TITLE]
    row = stored(db_sessions)
    # The pause stays in the row, so the next pause doubles it.
    assert (row.breaker_reason, row.breaker_level, row.breaker_until) == (None, 1, START + HOUR)
    assert budget.open_breaker(BreakerReason.HTTP_429).pause.until == fake_time.now + 2 * HOUR


def test_a_request_without_an_answer_after_the_pause_sends_no_end_notification(db_sessions, fake_time, sent):
    budget = breaker_budget(db_sessions, fake_time, sent)
    budget.open_breaker(BreakerReason.HTTP_429)
    fake_time.now = START + HOUR

    budget.before_request()
    budget.after_answer(None)
    budget.end_if_answered()

    assert sent.titles() == [OPENED_TITLE]
    assert stored(db_sessions).breaker_reason is BreakerReason.HTTP_429
    budget.before_request()
    budget.after_answer(200)
    budget.end_if_answered()
    assert sent.titles() == [OPENED_TITLE, CLOSED_TITLE]


def test_a_new_pause_after_the_answer_and_before_the_end_of_the_call_sends_no_end(db_sessions, fake_time, sent):
    budget = breaker_budget(db_sessions, fake_time, sent)
    budget.open_breaker(BreakerReason.HTTP_429)
    fake_time.now = START + HOUR
    budget.before_request()
    budget.after_answer(200)

    # The call reads a bot check in the answer, so it ends with the limit and does not call ``end_if_answered``.
    budget.open_breaker(BreakerReason.BOT_CHECK)

    assert sent.titles() == [OPENED_TITLE, OPENED_TITLE]
    assert stored(db_sessions).breaker_reason is BreakerReason.BOT_CHECK
    # After the end of the new pause, a call whose request got no answer does not end it: the old answer does not count.
    fake_time.now += 6 * HOUR
    budget.before_request()
    budget.after_answer(None)
    budget.end_if_answered()
    assert sent.titles() == [OPENED_TITLE, OPENED_TITLE]
    assert stored(db_sessions).breaker_reason is BreakerReason.BOT_CHECK


def test_without_a_time_zone_the_budget_names_the_end_in_utc(db_sessions, fake_time, sent):
    budget = RequestBudget(db_sessions, clock=fake_time.clock, sleep=fake_time.sleep, send=sent.append)

    breaker_open = budget.open_breaker(BreakerReason.HTTP_429)

    assert breaker_open.pause.zone == "UTC"
    assert " until 2026-10-28 13:00 UTC. " in sent[0].body


def test_an_answer_of_another_budget_does_not_end_the_pause(db_sessions, fake_time, sent):
    budget = breaker_budget(db_sessions, fake_time, sent)
    budget.open_breaker(BreakerReason.HTTP_429)
    fake_time.now = START + HOUR
    breaker_budget(db_sessions, fake_time, sent).after_answer(200)

    budget.end_if_answered()

    assert sent.titles() == [OPENED_TITLE]


def test_http_429_as_the_first_answer_after_the_pause_opens_a_longer_pause_and_sends_no_end(
    db_sessions, fake_time, sent
):
    budget = breaker_budget(db_sessions, fake_time, sent)
    budget.open_breaker(BreakerReason.HTTP_429)
    fake_time.now = START + HOUR

    budget.before_request()
    budget.after_answer(429)

    assert sent.titles() == [OPENED_TITLE, OPENED_TITLE]
    assert " for 2 hours, " in sent[1].body
    assert stored(db_sessions).breaker_until == START + 3 * HOUR


def test_a_failed_notification_does_not_fail_the_answer(db_sessions, fake_time, caplog):
    def send(notification):
        raise RuntimeError("the target is down")

    budget = RequestBudget(db_sessions, clock=fake_time.clock, sleep=fake_time.sleep, send=send)

    budget.after_answer(429)

    assert stored(db_sessions).breaker_reason is BreakerReason.HTTP_429
    assert "Cannot send request_breaker: RuntimeError" in caplog.text


def test_without_a_send_the_breaker_notifies_each_stored_target(db_sessions, fake_time, monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "send_to_stored_targets", lambda sessions, notification: calls.append(sessions))
    budget = RequestBudget(db_sessions, clock=fake_time.clock, sleep=fake_time.sleep)

    budget.after_answer(429)

    assert calls == [db_sessions]


def test_paused_gives_the_error_of_an_open_breaker_and_none_before_and_after(db_sessions, fake_time, sent):
    budget = breaker_budget(db_sessions, fake_time, sent)
    assert budget.paused() is None

    budget.open_breaker(BreakerReason.TRY_AGAIN_LATER)
    fake_time.now = START + HOUR - datetime.timedelta(microseconds=1)
    paused = budget.paused()

    assert paused is not None
    assert (paused.pause.reason, paused.pause.until) == (BreakerReason.TRY_AGAIN_LATER, START + HOUR)
    fake_time.now = START + HOUR
    assert budget.paused() is None


def test_a_row_that_another_process_makes_during_the_first_use_stays_and_gives_the_token(db_url, fake_time, sent):
    """Two first uses at once: the other process makes the row between the start of this use and its insert."""
    engines = [make_engine(db_url), make_engine(db_url)]
    try:
        mine, other = (make_session_factory(engine) for engine in engines)
        made = []

        def clock():
            if not made:
                made.append(True)
                store(other, 7.0, START)
            return fake_time.now

        RequestBudget(mine, clock=clock, sleep=fake_time.sleep, random=fixed_pause(0.0)).before_request()

        row = stored(mine)
        assert (row.tokens, row.refilled_at) == (6.0, START)
    finally:
        for engine in engines:
            engine.dispose()


def test_a_clock_that_goes_back_never_makes_an_open_pause_longer_than_its_length(db_sessions, fake_time, sent):
    budget = breaker_budget(db_sessions, fake_time, sent)
    budget.open_breaker(BreakerReason.HTTP_429)
    fake_time.now = START - datetime.timedelta(days=2)

    with pytest.raises(BreakerOpen) as raised:
        budget.before_request()

    assert raised.value.pause.until == fake_time.now + HOUR
    row = stored(db_sessions)
    assert (row.breaker_opened_at, row.breaker_until) == (fake_time.now, fake_time.now + HOUR)
    fake_time.now += HOUR
    budget.before_request()


def test_an_opening_after_a_clock_that_went_back_doubles_the_pause_and_does_not_stretch_it(
    db_sessions, fake_time, sent
):
    budget = breaker_budget(db_sessions, fake_time, sent)
    budget.open_breaker(BreakerReason.HTTP_429)
    fake_time.now = START - datetime.timedelta(days=2)
    with pytest.raises(BreakerOpen):
        budget.before_request()
    fake_time.now += HOUR

    paused = budget.open_breaker(BreakerReason.HTTP_429)

    assert paused.pause.until == fake_time.now + 2 * HOUR


@pytest.mark.parametrize("name", ["Mars/Base", "Europe/Kyiv?", "x" * 300])
def test_a_stored_time_zone_that_is_not_known_gives_utc_and_still_opens_the_breaker(
    db_sessions, fake_time, sent, caplog, name
):
    with db_sessions.begin() as session:
        session.add(Schedule(id=1, enabled=True, kind=ScheduleKind.CRON, timezone=name))
    budget = breaker_budget(db_sessions, fake_time, sent, default_timezone="Europe/Kyiv")

    budget.after_answer(429)

    assert stored(db_sessions).breaker_reason is BreakerReason.HTTP_429
    assert sent.titles() == [OPENED_TITLE]
    assert " until 2026-10-28 13:00 UTC. " in sent[0].body
    assert "is not known, so the texts of the breaker use UTC" in caplog.text
