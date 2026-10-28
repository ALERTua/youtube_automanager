"""Tests of the channel actions: the search and the subscription change in a child process, and a channel for a rule.

No test reads or changes YouTube: fakes stand in for the yt-dlp readers, the account client and the child process.
"""

import datetime
import io
import json
import os
import subprocess
import sys
import threading

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from yt_dlp import YoutubeDL
from yt_dlp.networking.common import Response

from youtube_automanager import channel_actions
from youtube_automanager.channel_actions import (
    RETRY_WAITS_SECONDS,
    SEARCH_LIMIT,
    SEARCH_TIMEOUT_SECONDS,
    SUBSCRIPTION_TIMEOUT_SECONDS,
    AddOutcome,
    BatchChannel,
    ChannelForRule,
    ChannelOutcome,
    FoundChannel,
    OutcomeKind,
    SubscriptionBatchResult,
    add_channel_to_rule,
    add_channels_to_rule,
    batch_request,
    batch_timeout_seconds,
    change_subscription_once,
    change_subscriptions_command,
    change_subscriptions_in_child,
    change_subscriptions_once,
    find_channels_command,
    find_channels_in_child,
    find_channels_once,
    subscribe_command,
    subscribe_in_child,
    unsubscribe_command,
    unsubscribe_in_child,
)
from youtube_automanager.db.models import (
    Account,
    BreakerReason,
    Channel,
    CookieVersion,
    CookieVersionState,
    Playlist,
    Rule,
    RuleChannel,
    RunTrigger,
    SessionState,
)
from youtube_automanager.engine import account_session
from youtube_automanager.pacing import BUCKET_SIZE, REFILL_S, RequestBudget, minimum_seconds
from youtube_automanager.request_pause import EXIT_PAUSED, RequestPause
from youtube_automanager.runner import BUSY_TEXT, SCHEDULED_WAIT_S, SOURCE_ROOT, SyncRunner
from youtube_automanager.settings import Settings
from youtube_automanager.short_child import (
    EXIT_LOGGED_OUT,
    EXIT_UNAVAILABLE,
    EXIT_YOUTUBE_ERROR,
    NO_ACCEPTED_VERSION,
    ChildBusy,
    ChildError,
    ChildFailed,
    ChildLoggedOut,
    ChildPaused,
    ChildUnavailable,
    ChildYouTubeError,
)
from youtube_automanager.youtube import public
from youtube_automanager.youtube.account import (
    AccountError,
    NetworkOrServerError,
    NotLoggedInError,
    RequestsPausedError,
    WriteRejectedError,
)
from youtube_automanager.youtube.public import ChannelInfo, PublicSourceError
from youtube_automanager.youtube.session import CookieCipher, generate_key, save_cookies

A = "UCaaaaaaaaaaaaaaaaaaaaaa"
B = "UCbbbbbbbbbbbbbbbbbbbbbb"
C = "UCcccccccccccccccccccccc"
WAIT_SECONDS = 10
COOKIE_VALUE = "fake-sapisid"
COOKIES = (
    "# Netscape HTTP Cookie File\n"
    f".youtube.com\tTRUE\t/\tTRUE\t4000000000\tSAPISID\t{COOKIE_VALUE}\n"
    ".youtube.com\tTRUE\t/\tFALSE\t4000000000\tSIDCC\tfake-sidcc-0\n"
)


def found_dict(channel_id=A, title="Alpha", handle="@alpha", count=10, thumbnail="https://yt3.example/a.jpg") -> dict:
    return {
        "channel_id": channel_id,
        "title": title,
        "handle": handle,
        "subscriber_count": count,
        "thumbnail_url": thumbnail,
    }


def last_stderr_line(capsys) -> str:
    return capsys.readouterr().err.strip().splitlines()[-1]


@pytest.fixture
def settings(tmp_path, db_url):
    return Settings(
        home=tmp_path, database_url=db_url, tz="UTC", ytdlp_dir=tmp_path / "yt-dlp", cookies_key=generate_key()
    )


def store_cookies(sessions, settings, text=COOKIES):
    with sessions.begin() as session:
        save_cookies(session, CookieCipher(settings.cookies_key.get_secret_value()), text)


def store(sessions, *rows):
    with sessions.begin() as session:
        session.add_all(rows)


def stored_channel(sessions, channel_id):
    with sessions() as session:
        return session.get(Channel, channel_id)


# The commands.


def test_the_commands_name_the_child_and_keep_each_value_one_argument():
    assert find_channels_command("-rf @x y") == [
        sys.executable,
        "-m",
        "youtube_automanager",
        "find-channels",
        "--query=-rf @x y",
    ]
    assert subscribe_command(A, "- Title") == [
        sys.executable,
        "-m",
        "youtube_automanager",
        "subscribe",
        f"--channel-id={A}",
        "--title=- Title",
    ]
    assert (
        subscribe_command(A)
        == subscribe_command(A, "")
        == [
            sys.executable,
            "-m",
            "youtube_automanager",
            "subscribe",
            f"--channel-id={A}",
        ]
    )
    assert unsubscribe_command(A) == [sys.executable, "-m", "youtube_automanager", "unsubscribe", f"--channel-id={A}"]


def test_the_timeouts_of_the_children_are_shorter_than_the_wait_of_a_scheduled_run():
    assert SEARCH_TIMEOUT_SECONDS < SCHEDULED_WAIT_S
    assert SUBSCRIPTION_TIMEOUT_SECONDS < SCHEDULED_WAIT_S


# The body of the search child.


def test_the_search_child_writes_the_found_channels_as_the_last_line_of_stderr(monkeypatch, capsys):
    calls = []

    def fake_find(text, limit):
        calls.append((text, limit))
        return [
            ChannelInfo(A, "Alpha", "@alpha", f"https://www.youtube.com/channel/{A}", "https://yt3.example/a.jpg", 10),
            ChannelInfo(B, "Bravo", None, f"https://www.youtube.com/channel/{B}", None, None),
        ]

    monkeypatch.setattr(public, "find_channels", fake_find)

    assert find_channels_once("science") == 0

    assert calls == [("science", SEARCH_LIMIT)]
    assert json.loads(last_stderr_line(capsys)) == [
        found_dict(),
        found_dict(B, "Bravo", None, None, None),
    ]


def test_a_failed_search_ends_with_the_youtube_error_code_and_the_cause_on_one_line(monkeypatch, capsys):
    def failing_find(text, limit):
        raise PublicSourceError("yt-dlp failed to read the search:\n  HTTP Error 429")

    monkeypatch.setattr(public, "find_channels", failing_find)

    assert find_channels_once("science") == EXIT_YOUTUBE_ERROR

    assert last_stderr_line(capsys) == "yt-dlp failed to read the search: HTTP Error 429"


# The body of the subscription child.


@pytest.fixture
def fake_client(monkeypatch):
    """Replaces the account client of the account session.

    A test sets ``check_error``, ``config_error`` or ``change_error``. ``errors`` maps a channel id to the outcomes of
    its next changes in order: an error to raise, or None for a confirmed change. Each change records the page config
    that it got.
    """

    class FakeClient:
        check_error = None
        config_error = None
        change_error = None

        def __init__(self, text, *, budget):
            self.text = text

        def check_session(self):
            FakeClient.checks += 1
            if FakeClient.check_error is not None:
                raise FakeClient.check_error

        def home_config(self):
            FakeClient.configs += 1
            if FakeClient.config_error is not None:
                raise FakeClient.config_error
            return {"config": FakeClient.configs}

        def use_cookies(self, text):
            self.text = text

        def cookies_text(self):
            return self.text.replace("fake-sidcc-0", "fake-sidcc-1")

        def subscribe(self, channel_id, *, ytcfg=None):
            self._change("subscribe", channel_id, ytcfg)

        def unsubscribe(self, channel_id, *, ytcfg=None):
            self._change("unsubscribe", channel_id, ytcfg)

        def _change(self, action, channel_id, ytcfg):
            FakeClient.changes.append((action, channel_id))
            FakeClient.ytcfgs.append(ytcfg)
            planned = FakeClient.errors.get(channel_id)
            if planned:
                error = planned.pop(0)
                if error is not None:
                    raise error
            elif FakeClient.change_error is not None:
                raise FakeClient.change_error

    FakeClient.changes = []
    FakeClient.ytcfgs = []
    FakeClient.errors = {}
    FakeClient.checks = FakeClient.configs = 0
    monkeypatch.setattr(account_session, "AccountClient", FakeClient)
    return FakeClient


def test_a_subscribe_writes_the_flag_of_a_stored_channel_and_keeps_the_rest_of_its_row(
    settings, db_sessions, fake_client, capsys
):
    store_cookies(db_sessions, settings)
    store(db_sessions, Channel(channel_id=A, title="Stored", handle="@stored", is_subscribed=False))

    assert change_subscription_once(settings, A, subscribe=True, title="Another title") == 0

    assert fake_client.changes == [("subscribe", A)]
    assert json.loads(last_stderr_line(capsys)) == {"channel_id": A, "subscribed": True}
    row = stored_channel(db_sessions, A)
    assert (row.title, row.handle, row.is_subscribed, row.mark_date, row.last_checked_at) == (
        "Stored",
        "@stored",
        True,
        None,
        None,
    )


@pytest.mark.parametrize(("title", "expected"), [("  Found  ", "Found"), (None, A), ("   ", A)])
def test_a_subscribe_adds_a_missing_channel_row_with_the_title_or_the_id(
    settings, db_sessions, fake_client, capsys, title, expected
):
    store_cookies(db_sessions, settings)

    assert change_subscription_once(settings, A, subscribe=True, title=title) == 0

    row = stored_channel(db_sessions, A)
    assert (row.title, row.handle, row.is_subscribed) == (expected, None, True)


def test_an_unsubscribe_writes_the_flag_and_adds_no_row(settings, db_sessions, fake_client, capsys):
    store_cookies(db_sessions, settings)
    store(db_sessions, Channel(channel_id=A, title="Stored", is_subscribed=True))

    assert change_subscription_once(settings, A, subscribe=False) == 0
    assert change_subscription_once(settings, B, subscribe=False) == 0

    assert fake_client.changes == [("unsubscribe", A), ("unsubscribe", B)]
    assert json.loads(last_stderr_line(capsys)) == {"channel_id": B, "subscribed": False}
    assert stored_channel(db_sessions, A).is_subscribed is False
    assert stored_channel(db_sessions, B) is None


def test_the_subscription_child_checks_the_session_and_saves_the_updated_cookies(
    settings, db_sessions, fake_client, capsys
):
    store_cookies(db_sessions, settings)

    assert change_subscription_once(settings, A, subscribe=True) == 0

    with db_sessions() as session:
        assert session.scalars(select(Account)).one().session_state is SessionState.OK
        states = [version.state for version in session.scalars(select(CookieVersion).order_by(CookieVersion.id))]
    assert states == [CookieVersionState.GOOD, CookieVersionState.UNCHECKED]


@pytest.mark.parametrize(
    ("with_key", "expected"),
    [(False, "COOKIES_KEY is not set"), (True, "no YouTube cookies are stored")],
)
def test_without_a_key_or_cookies_the_subscription_child_ends_with_the_unavailable_code(
    settings, db_sessions, fake_client, capsys, with_key, expected
):
    if not with_key:
        settings = settings.model_copy(update={"cookies_key": None})

    assert change_subscription_once(settings, A, subscribe=True, title="Alpha") == EXIT_UNAVAILABLE

    assert expected in last_stderr_line(capsys)
    assert fake_client.changes == []
    assert stored_channel(db_sessions, A) is None


def test_a_dead_session_at_the_check_changes_nothing(settings, db_sessions, fake_client, capsys):
    store_cookies(db_sessions, settings)
    fake_client.check_error = NotLoggedInError("YouTube answered the page as to an anonymous user")

    assert change_subscription_once(settings, A, subscribe=True) == EXIT_LOGGED_OUT

    assert last_stderr_line(capsys) == NO_ACCEPTED_VERSION
    assert fake_client.changes == []
    assert stored_channel(db_sessions, A) is None


def test_a_logged_out_answer_of_the_change_ends_with_the_logged_out_code_and_the_session_dies(
    settings, db_sessions, fake_client, capsys
):
    store_cookies(db_sessions, settings)
    store(db_sessions, Channel(channel_id=A, title="Stored", is_subscribed=False))
    fake_client.change_error = NotLoggedInError("YouTube refused the session with HTTP 401")

    assert change_subscription_once(settings, A, subscribe=True) == EXIT_LOGGED_OUT

    assert last_stderr_line(capsys) == "YouTube refused the session with HTTP 401"
    assert stored_channel(db_sessions, A).is_subscribed is False
    with db_sessions() as session:
        assert session.scalars(select(Account)).one().session_state is SessionState.DEAD
        assert [version.state for version in session.scalars(select(CookieVersion))] == [CookieVersionState.DEAD]


@pytest.mark.parametrize(
    "error",
    [
        WriteRejectedError("YouTube did not confirm that it could subscribe to channel x: subscribed states []"),
        AccountError("yt-dlp failed to subscribe to channel x:\n  HTTP Error 500"),
    ],
)
def test_a_change_that_youtube_does_not_confirm_writes_no_flag_and_still_saves_the_cookies(
    settings, db_sessions, fake_client, capsys, error
):
    store_cookies(db_sessions, settings)
    store(db_sessions, Channel(channel_id=A, title="Stored", is_subscribed=False))
    fake_client.change_error = error

    assert change_subscription_once(settings, A, subscribe=True) == EXIT_YOUTUBE_ERROR

    assert last_stderr_line(capsys) == " ".join(str(error).split())
    assert stored_channel(db_sessions, A).is_subscribed is False
    with db_sessions() as session:
        assert session.scalars(select(Account)).one().session_state is SessionState.OK
        assert len(session.scalars(select(CookieVersion)).all()) == 2


def test_during_the_pause_of_the_breaker_the_change_sends_no_request_and_gives_the_pause_to_the_parent(
    settings, db_sessions, capsys, monkeypatch, runner, fake_starter
):
    store_cookies(db_sessions, settings)
    store(db_sessions, Channel(channel_id=A, title="Stored", is_subscribed=False))
    paused = RequestBudget(db_sessions, send=lambda notification: None).open_breaker(BreakerReason.HTTP_429)
    requests = []
    monkeypatch.setattr(YoutubeDL, "urlopen", lambda ydl, request: requests.append(request.url))

    code = change_subscription_once(settings, A, subscribe=True)

    assert code == EXIT_PAUSED
    line = last_stderr_line(capsys)
    assert RequestPause.from_line(line) == RequestPause(BreakerReason.HTTP_429, paused.pause.until, "UTC")
    assert requests == []
    assert stored_channel(db_sessions, A).is_subscribed is False
    with db_sessions() as session:
        # The pause is no sign of a dead session.
        assert session.scalars(select(Account)).one().session_state is SessionState.UNKNOWN
        assert [version.state for version in session.scalars(select(CookieVersion))] == [CookieVersionState.UNCHECKED]
    # The parent reads the line back.
    fake_starter.plan.append({"exit_code": code, "stderr": f"a warning\n{line}\n"})
    with pytest.raises(ChildPaused) as raised:
        subscribe_in_child(runner, A, "Alpha")
    assert raised.value.pause == RequestPause(BreakerReason.HTTP_429, paused.pause.until, "UTC")
    assert str(raised.value) == str(paused)


def test_a_pause_that_starts_during_the_change_gives_the_pause_to_the_parent(
    settings, db_sessions, fake_client, capsys
):
    store_cookies(db_sessions, settings)
    pause = RequestPause(BreakerReason.BOT_CHECK, datetime.datetime(2026, 10, 28, 18, 0, tzinfo=datetime.UTC), "UTC")
    fake_client.change_error = RequestsPausedError(pause)

    assert change_subscription_once(settings, A, subscribe=True) == EXIT_PAUSED

    assert RequestPause.from_line(last_stderr_line(capsys)) == pause


@pytest.mark.parametrize("action", ["subscribe", "unsubscribe"])
def test_the_pause_exit_code_with_a_line_that_is_no_pause_is_a_process_error(runner, fake_starter, action):
    fake_starter.plan.append({"exit_code": EXIT_PAUSED, "stderr": f"ValueError: {COOKIE_VALUE}\n"})

    with pytest.raises(ChildFailed) as raised:
        subscribe_in_child(runner, A, "Alpha") if action == "subscribe" else unsubscribe_in_child(runner, A)

    assert str(raised.value) == (
        "The subscription change process stopped at a pause of the requests, but gave no end of the pause."
    )


@pytest.mark.parametrize("channel_id", ["", "UC", "@handle", f"{A}x", "PLaaaaaaaaaaaaaaaaaaaaaa"])
def test_a_text_that_is_not_a_channel_id_fails_before_any_request(settings, db_sessions, fake_client, channel_id):
    store_cookies(db_sessions, settings)

    with pytest.raises(ValueError, match="not a YouTube channel id"):
        change_subscription_once(settings, channel_id, subscribe=True)

    assert fake_client.changes == []
    with db_sessions() as session:
        assert session.scalars(select(Account)).one().session_state is SessionState.UNKNOWN


# The body of the subscription batch child.

PAUSE = RequestPause(BreakerReason.BOT_CHECK, datetime.datetime(2026, 10, 28, 18, 0, tzinfo=datetime.UTC), "UTC")


def batch_text(subscribe, *channels):
    """The batch line of ``channels``: an id, or a tuple of an id and a title."""
    return batch_request(
        subscribe,
        [BatchChannel(*channel) if isinstance(channel, tuple) else BatchChannel(channel) for channel in channels],
    )


def stderr_lines(capsys) -> list[str]:
    return capsys.readouterr().err.strip().splitlines()


def outcome(channel_id, kind="confirmed", cause=None) -> dict:
    return {"channel_id": channel_id, "outcome": kind, "cause": cause}


def network_error(text="HTTP Error 503: Service Unavailable") -> NetworkOrServerError:
    return NetworkOrServerError(f"yt-dlp failed to subscribe to channel x: {text}")


def test_a_batch_checks_the_session_and_reads_the_page_config_once_and_gives_it_to_each_change(
    settings, db_sessions, fake_client, capsys
):
    store_cookies(db_sessions, settings)
    store(db_sessions, Channel(channel_id=A, title="Stored", handle="@stored", is_subscribed=False))
    sleeps = []

    code = change_subscriptions_once(
        settings, batch_text(True, (A, "Another title"), (B, " Bravo "), C), sleep=sleeps.append
    )

    assert code == 0
    assert (fake_client.checks, fake_client.configs) == (1, 1)
    assert fake_client.changes == [("subscribe", A), ("subscribe", B), ("subscribe", C)]
    assert fake_client.ytcfgs == [{"config": 1}] * 3
    assert sleeps == []
    assert json.loads(last_stderr_line(capsys)) == {
        "subscribe": True,
        "outcomes": [outcome(A), outcome(B), outcome(C)],
        "stopped": None,
    }
    rows = [stored_channel(db_sessions, channel_id) for channel_id in (A, B, C)]
    assert [(row.title, row.handle, row.is_subscribed) for row in rows] == [
        ("Stored", "@stored", True),
        ("Bravo", None, True),
        (C, None, True),
    ]


def test_an_unsubscribe_batch_writes_the_flag_and_adds_no_row(settings, db_sessions, fake_client, capsys):
    store_cookies(db_sessions, settings)
    store(db_sessions, Channel(channel_id=A, title="Stored", is_subscribed=True))

    assert change_subscriptions_once(settings, batch_text(False, A, B)) == 0

    assert fake_client.changes == [("unsubscribe", A), ("unsubscribe", B)]
    assert json.loads(last_stderr_line(capsys))["outcomes"] == [outcome(A), outcome(B)]
    assert stored_channel(db_sessions, A).is_subscribed is False
    assert stored_channel(db_sessions, B) is None


def test_each_confirmed_change_is_stored_before_the_next_change(
    settings, db_sessions, fake_client, monkeypatch, capsys
):
    store_cookies(db_sessions, settings)
    store(
        db_sessions,
        Channel(channel_id=A, title="Alpha", is_subscribed=True),
        Channel(channel_id=B, title="Bravo", is_subscribed=True),
    )
    seen = []
    change = fake_client._change

    def recording_change(self, action, channel_id, ytcfg):
        seen.append(
            (channel_id, stored_channel(db_sessions, A).is_subscribed, stored_channel(db_sessions, B).is_subscribed)
        )
        change(self, action, channel_id, ytcfg)

    monkeypatch.setattr(fake_client, "_change", recording_change)

    assert change_subscriptions_once(settings, batch_text(False, A, B)) == 0

    assert seen == [(A, True, True), (B, False, True)]
    assert stored_channel(db_sessions, B).is_subscribed is False


def test_a_channel_in_the_target_state_already_is_a_success(settings, db_sessions, fake_client, capsys):
    store_cookies(db_sessions, settings)
    store(db_sessions, Channel(channel_id=A, title="Alpha", is_subscribed=True))

    assert change_subscriptions_once(settings, batch_text(True, A)) == 0
    assert change_subscriptions_once(settings, batch_text(True, A)) == 0

    assert json.loads(last_stderr_line(capsys))["outcomes"] == [outcome(A)]
    assert stored_channel(db_sessions, A).is_subscribed is True


def test_each_channel_of_the_batch_goes_once(settings, db_sessions, fake_client, capsys):
    store_cookies(db_sessions, settings)

    assert change_subscriptions_once(settings, batch_text(True, (A, "First"), B, (A, "Second"))) == 0

    assert fake_client.changes == [("subscribe", A), ("subscribe", B)]
    assert json.loads(last_stderr_line(capsys))["outcomes"] == [outcome(A), outcome(B)]
    assert stored_channel(db_sessions, A).title == "First"


def test_a_change_that_youtube_does_not_confirm_is_skipped_and_the_batch_goes_on(
    settings, db_sessions, fake_client, capsys, caplog
):
    store_cookies(db_sessions, settings)
    store(
        db_sessions,
        Channel(channel_id=A, title="Alpha", is_subscribed=False),
        Channel(channel_id=B, title="Bravo", is_subscribed=False),
    )
    cause = f"YouTube did not confirm that it could subscribe to channel {A}: subscribed states [False]"
    fake_client.errors = {A: [WriteRejectedError(cause)]}
    sleeps = []

    assert change_subscriptions_once(settings, batch_text(True, A, B), sleep=sleeps.append) == 0

    assert fake_client.changes == [("subscribe", A), ("subscribe", B)]
    assert sleeps == []
    assert json.loads(last_stderr_line(capsys)) == {
        "subscribe": True,
        "outcomes": [outcome(A, "rejected", cause), outcome(B)],
        "stopped": None,
    }
    assert stored_channel(db_sessions, A).is_subscribed is False
    assert stored_channel(db_sessions, B).is_subscribed is True
    assert f"The subscription batch skips channel {A}: {cause}" in caplog.text


def test_a_network_failure_waits_10_s_then_30_s_and_sends_the_same_change_again(
    settings, db_sessions, fake_client, capsys, caplog
):
    store_cookies(db_sessions, settings)
    first, second = network_error(), network_error("<urlopen error timed out>")
    fake_client.errors = {A: [first, second, None]}
    sleeps = []

    assert change_subscriptions_once(settings, batch_text(True, A, B), sleep=sleeps.append) == 0

    assert RETRY_WAITS_SECONDS == (10, 30)
    assert sleeps == [10, 30]
    assert fake_client.changes == [("subscribe", A)] * 3 + [("subscribe", B)]
    assert fake_client.ytcfgs == [{"config": 1}] * 4
    assert json.loads(last_stderr_line(capsys))["outcomes"] == [outcome(A), outcome(B)]
    assert stored_channel(db_sessions, A).is_subscribed is True
    assert f"The change of channel {A} failed, so it goes again in 10 s: {first}" in caplog.text
    assert f"The change of channel {A} failed, so it goes again in 30 s: {second}" in caplog.text


def test_the_third_network_failure_stops_the_batch_with_the_youtube_error_code(
    settings, db_sessions, fake_client, capsys, caplog
):
    store_cookies(db_sessions, settings)
    store(db_sessions, *(Channel(channel_id=channel_id, title="x", is_subscribed=False) for channel_id in (A, B, C)))
    last = network_error("HTTP Error 500:\n Internal Server Error")
    fake_client.errors = {B: [network_error(), network_error(), last]}
    sleeps = []

    code = change_subscriptions_once(settings, batch_text(True, A, B, C), sleep=sleeps.append)

    assert code == EXIT_YOUTUBE_ERROR
    assert sleeps == [10, 30]
    assert fake_client.changes == [("subscribe", A)] + [("subscribe", B)] * 3
    cause = " ".join(str(last).split())
    *_, result_line, cause_line = stderr_lines(capsys)
    assert cause_line == cause
    assert json.loads(result_line) == {
        "subscribe": True,
        "outcomes": [outcome(A), outcome(B, "failed", cause)],
        "stopped": {"reason": "network", "cause": cause},
    }
    assert [stored_channel(db_sessions, channel_id).is_subscribed for channel_id in (A, B, C)] == [True, False, False]
    assert f"The subscription batch stopped after 2 of 3 channel(s), because of network: {cause}" in caplog.text


def test_a_pause_stops_the_batch_at_once_and_keeps_the_confirmed_changes(settings, db_sessions, fake_client, capsys):
    store_cookies(db_sessions, settings)
    store(db_sessions, *(Channel(channel_id=channel_id, title="x", is_subscribed=True) for channel_id in (A, B, C)))
    fake_client.errors = {B: [RequestsPausedError(PAUSE)]}
    sleeps = []

    code = change_subscriptions_once(settings, batch_text(False, A, B, C), sleep=sleeps.append)

    assert code == EXIT_PAUSED
    assert sleeps == []
    assert fake_client.changes == [("unsubscribe", A), ("unsubscribe", B)]
    *_, result_line, pause_line = stderr_lines(capsys)
    assert RequestPause.from_line(pause_line) == PAUSE
    assert json.loads(result_line) == {
        "subscribe": False,
        "outcomes": [outcome(A), outcome(B, "failed", str(PAUSE))],
        "stopped": {"reason": "paused", "cause": str(PAUSE)},
    }
    assert [stored_channel(db_sessions, channel_id).is_subscribed for channel_id in (A, B, C)] == [False, True, True]
    with db_sessions() as session:
        assert session.scalars(select(Account)).one().session_state is SessionState.OK


def test_a_logged_out_answer_stops_the_batch_kills_the_session_and_keeps_the_confirmed_changes(
    settings, db_sessions, fake_client, capsys
):
    store_cookies(db_sessions, settings)
    store(db_sessions, *(Channel(channel_id=channel_id, title="x", is_subscribed=False) for channel_id in (A, B, C)))
    fake_client.errors = {B: [NotLoggedInError("YouTube refused the session with HTTP 401")]}

    code = change_subscriptions_once(settings, batch_text(True, A, B, C))

    assert code == EXIT_LOGGED_OUT
    assert fake_client.changes == [("subscribe", A), ("subscribe", B)]
    *_, result_line, cause_line = stderr_lines(capsys)
    assert cause_line == "YouTube refused the session with HTTP 401"
    assert json.loads(result_line)["stopped"] == {
        "reason": "logged_out",
        "cause": "YouTube refused the session with HTTP 401",
    }
    assert [stored_channel(db_sessions, channel_id).is_subscribed for channel_id in (A, B, C)] == [True, False, False]
    with db_sessions() as session:
        assert session.scalars(select(Account)).one().session_state is SessionState.DEAD


def test_another_error_stops_the_batch_without_a_retry(settings, db_sessions, fake_client, capsys):
    store_cookies(db_sessions, settings)
    error = AccountError("yt-dlp 2026.10.26 does not fit the client: YoutubeTab._extract_response raised KeyError: 'x'")
    fake_client.errors = {B: [error]}
    sleeps = []

    code = change_subscriptions_once(settings, batch_text(True, A, B, C), sleep=sleeps.append)

    assert code == EXIT_YOUTUBE_ERROR
    assert sleeps == []
    assert fake_client.changes == [("subscribe", A), ("subscribe", B)]
    *_, result_line, cause_line = stderr_lines(capsys)
    assert cause_line == str(error)
    assert json.loads(result_line) == {
        "subscribe": True,
        "outcomes": [outcome(A), outcome(B, "failed", str(error))],
        "stopped": {"reason": "error", "cause": str(error)},
    }
    assert stored_channel(db_sessions, A).is_subscribed is True


@pytest.mark.parametrize(
    ("error", "code", "reason"),
    [(RequestsPausedError(PAUSE), EXIT_PAUSED, "paused"), (network_error(), EXIT_YOUTUBE_ERROR, "network")],
    ids=["pause", "network"],
)
def test_a_failed_read_of_the_page_config_stops_the_batch_before_any_change(
    settings, db_sessions, fake_client, capsys, error, code, reason
):
    store_cookies(db_sessions, settings)
    fake_client.config_error = error
    sleeps = []

    assert change_subscriptions_once(settings, batch_text(True, A, B), sleep=sleeps.append) == code

    assert fake_client.changes == []
    assert sleeps == []
    *_, result_line, _last_line = stderr_lines(capsys)
    assert json.loads(result_line) == {
        "subscribe": True,
        "outcomes": [],
        "stopped": {"reason": reason, "cause": str(error)},
    }


@pytest.mark.parametrize(
    "text",
    [
        "not JSON",
        json.dumps([A]),
        json.dumps({"subscribe": True}),
        json.dumps({"subscribe": True, "channels": [{"channel_id": A}], "extra": 1}),
        json.dumps({"subscribe": 1, "channels": [{"channel_id": A}]}),
        json.dumps({"subscribe": "true", "channels": [{"channel_id": A}]}),
        json.dumps({"subscribe": True, "channels": []}),
        json.dumps({"subscribe": True, "channels": {"channel_id": A}}),
        json.dumps({"subscribe": True, "channels": [{"channel_id": A}, {"channel_id": "@handle"}]}),
        json.dumps({"subscribe": True, "channels": [{"channel_id": A}, {"channel_id": f"{B}x"}]}),
        json.dumps({"subscribe": True, "channels": [{"channel_id": A}, B]}),
        json.dumps({"subscribe": True, "channels": [{"title": "Alpha"}]}),
        json.dumps({"subscribe": True, "channels": [{"channel_id": A, "title": 5}]}),
        json.dumps({"subscribe": True, "channels": [{"channel_id": A, "handle": "@alpha"}]}),
    ],
)
def test_a_batch_that_is_not_valid_fails_before_any_request(settings, db_sessions, fake_client, text):
    store_cookies(db_sessions, settings)

    with pytest.raises(ValueError, match="subscription batch|channel id|channel title"):
        change_subscriptions_once(settings, text)

    assert (fake_client.checks, fake_client.configs, fake_client.changes) == (0, 0, [])
    with db_sessions() as session:
        assert session.scalars(select(Account)).one().session_state is SessionState.UNKNOWN


# A logged-in page config with only the keys that yt-dlp reads, and fake values.
YTCFG = {
    "LOGGED_IN": True,
    "INNERTUBE_API_KEY": "fakeApiKey",
    "INNERTUBE_CLIENT_NAME": "WEB",
    "INNERTUBE_CLIENT_VERSION": "2.20261010.00.00",
    "INNERTUBE_CONTEXT_CLIENT_NAME": 1,
    "INNERTUBE_CONTEXT": {
        "client": {"clientName": "WEB", "clientVersion": "2.20261010.00.00", "hl": "en", "visitorData": "fakeVisitor"}
    },
    "VISITOR_DATA": "fakeVisitor",
    "DATASYNC_ID": "fakeUserSession||",
    "SESSION_INDEX": "0",
    "USER_SESSION_ID": "fakeUserSession",
}


def test_a_batch_through_yt_dlp_reads_the_home_page_twice_and_sends_one_change_for_each_channel(
    settings, db_sessions, monkeypatch, capsys
):
    """The real account client and yt-dlp, with a fake transport: the check and the config read the home page."""
    store_cookies(db_sessions, settings)
    real_budget = account_session.RequestBudget
    # The real budget waits between the requests, and this test needs no wait.
    monkeypatch.setattr(
        account_session,
        "RequestBudget",
        lambda *args, **kwargs: real_budget(*args, sleep=lambda seconds: None, **kwargs),
    )
    requests = []

    def answer(ydl, request):
        path = request.url.split("?")[0]
        requests.append(path)
        if path.endswith("/youtubei/v1/subscription/unsubscribe"):
            channel_id = json.loads(request.data)["channelIds"][0]
            action = {"updateSubscribeButtonAction": {"subscribed": False, "channelId": channel_id}}
            body, content_type = json.dumps({"actions": [action]}), "application/json; charset=UTF-8"
        else:
            body, content_type = f"<html><script>ytcfg.set({json.dumps(YTCFG)});</script></html>", "text/html"
        return Response(io.BytesIO(body.encode()), request.url, {"Content-Type": content_type})

    monkeypatch.setattr(YoutubeDL, "urlopen", answer)

    assert change_subscriptions_once(settings, batch_text(False, A, B, C)) == 0

    unsubscribe = "https://www.youtube.com/youtubei/v1/subscription/unsubscribe"
    assert requests == ["https://www.youtube.com"] * 2 + [unsubscribe] * 3
    assert json.loads(last_stderr_line(capsys))["outcomes"] == [outcome(A), outcome(B), outcome(C)]


# The parent.


@pytest.fixture
def runner(db_sessions, db_url, tmp_path, fake_starter):
    return SyncRunner(
        db_sessions,
        ytdlp_dir=tmp_path / "yt-dlp",
        database_url=db_url,
        child_timeout_s=WAIT_SECONDS,
        start=fake_starter,
        lock=threading.Lock(),
    )


def recorded_waits(fake_starter):
    """Wrap ``fake_starter``, so each child records the timeout of each wait. Return the starter and the waits."""
    waits = []

    def start(command, env, input_text):
        child = fake_starter(command, env, input_text)
        wait = child.wait

        def recording_wait(timeout=None):
            waits.append(timeout)
            return wait(timeout)

        child.wait = recording_wait
        return child

    return start, waits


def test_the_search_gives_the_query_to_the_child_and_reads_the_found_channels(runner, fake_starter, tmp_path):
    fake_starter.plan.append({"stderr": "a warning\n" + json.dumps([found_dict(), found_dict(B, "Bravo")]) + "\n\n"})

    channels = find_channels_in_child(runner, "@alpha")

    assert channels == [
        FoundChannel(A, "Alpha", "@alpha", 10, "https://yt3.example/a.jpg"),
        FoundChannel(B, "Bravo", "@alpha", 10, "https://yt3.example/a.jpg"),
    ]
    ((command, env),) = fake_starter.calls
    assert command == find_channels_command("@alpha")
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path / "yt-dlp")


def test_the_children_get_their_timeouts(db_sessions, db_url, tmp_path, fake_starter):
    start, waits = recorded_waits(fake_starter)
    fake_starter.plan[:] = [
        {"stderr": "[]\n"},
        {"stderr": json.dumps({"channel_id": A, "subscribed": True}) + "\n"},
        {"stderr": json.dumps({"channel_id": A, "subscribed": False}) + "\n"},
    ]
    runner = SyncRunner(
        db_sessions,
        ytdlp_dir=tmp_path / "yt-dlp",
        database_url=db_url,
        child_timeout_s=WAIT_SECONDS,
        start=start,
        lock=threading.Lock(),
    )

    find_channels_in_child(runner, "x")
    subscribe_in_child(runner, A)
    unsubscribe_in_child(runner, A)

    assert waits == [SEARCH_TIMEOUT_SECONDS, SUBSCRIPTION_TIMEOUT_SECONDS, SUBSCRIPTION_TIMEOUT_SECONDS]


def test_a_long_list_of_found_channels_comes_whole(runner, fake_starter):
    """The runner keeps only the end of a long stderr as the error text, but the result line comes in full."""
    channels = [
        found_dict(
            f"UC{number:022d}", f"Channel {number} " + "x" * 40, f"@channel{number}", number, "https://" + "y" * 150
        )
        for number in range(SEARCH_LIMIT)
    ]
    line = json.dumps(channels)
    assert len(line) > 4000
    fake_starter.plan.append({"stderr": line + "\n"})

    found = find_channels_in_child(runner, "channel")

    assert [channel.channel_id for channel in found] == [channel["channel_id"] for channel in channels]


def test_each_found_channel_comes_once_in_the_order_of_youtube(runner, fake_starter):
    line = json.dumps([found_dict(A, "First"), found_dict(B, "Bravo"), found_dict(A, "Second")])
    fake_starter.plan.append({"stderr": line + "\n"})

    assert [(channel.channel_id, channel.title) for channel in find_channels_in_child(runner, "x")] == [
        (A, "First"),
        (B, "Bravo"),
    ]


def test_an_empty_list_is_a_result(runner, fake_starter):
    fake_starter.plan.append({"stderr": "[]\n"})

    assert find_channels_in_child(runner, "nothing") == []


@pytest.mark.parametrize(
    "stderr",
    [
        "",
        "a warning only\n",
        "{}\n",
        json.dumps(found_dict()) + "\n",
        json.dumps([found_dict(), "not a channel"]) + "\n",
        json.dumps([{**found_dict(), "extra": 1}]) + "\n",
        json.dumps([{key: value for key, value in found_dict().items() if key != "handle"}]) + "\n",
        json.dumps([found_dict(channel_id="")]) + "\n",
        json.dumps([found_dict(channel_id=5)]) + "\n",
        json.dumps([found_dict(title=None)]) + "\n",
        json.dumps([found_dict(handle=5)]) + "\n",
        json.dumps([found_dict(count="10")]) + "\n",
        json.dumps([found_dict(count=True)]) + "\n",
        json.dumps([found_dict(count=1.5)]) + "\n",
        json.dumps([found_dict(thumbnail=["x"])]) + "\n",
        json.dumps([found_dict()]) + "\na warning after the result\n",
    ],
)
def test_an_exit_code_0_without_the_found_channels_is_a_process_error(runner, fake_starter, stderr):
    fake_starter.plan.append({"stderr": stderr})

    with pytest.raises(ChildFailed, match="ended without the found channels"):
        find_channels_in_child(runner, "x")


@pytest.mark.parametrize(
    ("exit_code", "error_class"),
    [
        (EXIT_UNAVAILABLE, ChildUnavailable),
        (EXIT_LOGGED_OUT, ChildLoggedOut),
        (EXIT_YOUTUBE_ERROR, ChildYouTubeError),
    ],
)
@pytest.mark.parametrize("action", ["search", "subscribe", "unsubscribe"])
def test_each_failure_exit_code_gives_its_own_error_with_the_last_stderr_line(
    runner, fake_starter, exit_code, error_class, action
):
    fake_starter.plan.append({"exit_code": exit_code, "stderr": "a warning\nthe cause\n\n"})
    call = {
        "search": lambda: find_channels_in_child(runner, "x"),
        "subscribe": lambda: subscribe_in_child(runner, A, "Alpha"),
        "unsubscribe": lambda: unsubscribe_in_child(runner, A),
    }[action]

    with pytest.raises(error_class) as raised:
        call()

    assert str(raised.value) == "the cause"


@pytest.mark.parametrize("exit_code", [1, 2, 7, 137])
def test_another_exit_code_is_a_crash_and_its_stderr_stays_out_of_the_error(runner, fake_starter, exit_code):
    fake_starter.plan[:] = [
        {"exit_code": exit_code, "stderr": f"Traceback (most recent call last):\nValueError: {COOKIE_VALUE}\n"}
    ] * 2

    with pytest.raises(ChildFailed) as search_error:
        find_channels_in_child(runner, "x")
    with pytest.raises(ChildFailed) as subscribe_error:
        subscribe_in_child(runner, A)

    assert str(search_error.value) == f"The channel search process exited with code {exit_code}."
    assert str(subscribe_error.value) == f"The subscription change process exited with code {exit_code}."


@pytest.mark.parametrize(
    "result",
    [
        None,
        {"channel_id": A, "subscribed": False},
        {"channel_id": B, "subscribed": True},
        {"channel_id": A, "subscribed": True, "extra": 1},
        {"channel_id": A, "subscribed": 1},
    ],
)
def test_a_subscribe_without_the_confirmation_of_the_same_change_is_a_process_error(runner, fake_starter, result):
    fake_starter.plan.append({"stderr": "" if result is None else json.dumps(result) + "\n"})

    with pytest.raises(ChildFailed, match="ended without the confirmation of the change"):
        subscribe_in_child(runner, A)


def test_a_subscribe_and_an_unsubscribe_give_the_channel_to_the_child(runner, fake_starter):
    fake_starter.plan[:] = [
        {"stderr": json.dumps({"channel_id": A, "subscribed": True}) + "\n"},
        {"stderr": json.dumps({"channel_id": B, "subscribed": False}) + "\n"},
    ]

    subscribe_in_child(runner, A, "Alpha")
    unsubscribe_in_child(runner, B)

    assert [command for command, _env in fake_starter.calls] == [subscribe_command(A, "Alpha"), unsubscribe_command(B)]


@pytest.mark.parametrize("action", ["search", "subscribe", "unsubscribe"])
def test_an_action_while_a_run_is_active_starts_nothing_and_is_busy(runner, fake_starter, action):
    call = {
        "search": lambda: find_channels_in_child(runner, "x"),
        "subscribe": lambda: subscribe_in_child(runner, A),
        "unsubscribe": lambda: unsubscribe_in_child(runner, A),
    }[action]
    run_id = runner.claim(RunTrigger.MANUAL)
    try:
        with pytest.raises(ChildBusy) as raised:
            call()
    finally:
        runner.execute(run_id)

    assert str(raised.value) == BUSY_TEXT
    assert isinstance(raised.value, ChildError)
    # Only the run started a child.
    assert len(fake_starter.calls) == 1


def test_a_stop_of_the_service_stops_an_active_search(runner, fake_starter):
    fake_starter.plan.append({"blocking": True})
    outcome = []

    def search():
        try:
            find_channels_in_child(runner, "x")
        except ChildError as error:
            outcome.append(error)

    worker = threading.Thread(target=search, daemon=True)
    worker.start()
    assert fake_starter.child_started.wait(WAIT_SECONDS)

    assert runner.stop(timeout=WAIT_SECONDS) is True

    worker.join(WAIT_SECONDS)
    assert fake_starter.children[0].terminated
    (error,) = outcome
    assert isinstance(error, ChildFailed)
    assert str(error) == "The service stopped during the channel search."


def test_a_child_that_does_not_end_in_time_is_killed(runner, fake_starter, monkeypatch):
    monkeypatch.setattr(channel_actions, "SUBSCRIPTION_TIMEOUT_SECONDS", 0.05)
    fake_starter.plan.append({"blocking": True})

    with pytest.raises(ChildFailed, match="The subscription change did not end within 0.05 s"):
        subscribe_in_child(runner, A)

    assert fake_starter.children[0].killed
    assert not runner.is_active()


def batch_line(subscribe, *outcomes, stopped=None) -> str:
    return json.dumps({"subscribe": subscribe, "outcomes": list(outcomes), "stopped": stopped}) + "\n"


def test_the_time_limit_of_a_batch_grows_with_its_channels():
    assert batch_timeout_seconds(1) == SUBSCRIPTION_TIMEOUT_SECONDS + minimum_seconds(3) + 40
    assert batch_timeout_seconds(100) == SUBSCRIPTION_TIMEOUT_SECONDS + (102 - BUCKET_SIZE) * REFILL_S + 100 * 40
    assert sum(RETRY_WAITS_SECONDS) == 40


def test_a_batch_gives_its_channels_to_the_child_on_the_standard_input_and_reads_the_outcomes(
    db_sessions, db_url, tmp_path, fake_starter
):
    start, waits = recorded_waits(fake_starter)
    inputs = []

    def recording_start(command, env, input_text):
        inputs.append(input_text)
        return start(command, env, input_text)

    runner = SyncRunner(
        db_sessions,
        ytdlp_dir=tmp_path / "yt-dlp",
        database_url=db_url,
        child_timeout_s=WAIT_SECONDS,
        start=recording_start,
        lock=threading.Lock(),
    )
    cause = "YouTube did not confirm that it could subscribe to channel B: subscribed states []"
    fake_starter.plan.append(
        {"stderr": "a warning\n" + batch_line(True, outcome(A), outcome(B, "rejected", cause), outcome(C))}
    )

    result = change_subscriptions_in_child(
        runner, True, [BatchChannel(A, "Alpha"), BatchChannel(B), BatchChannel(A, "Again"), BatchChannel(C, "Чарлі")]
    )

    assert result == SubscriptionBatchResult(
        True,
        (
            ChannelOutcome(A, OutcomeKind.CONFIRMED, None),
            ChannelOutcome(B, OutcomeKind.REJECTED, cause),
            ChannelOutcome(C, OutcomeKind.CONFIRMED, None),
        ),
    )
    ((command, _env),) = fake_starter.calls
    assert command == change_subscriptions_command()
    assert [json.loads(text) for text in inputs] == [
        {
            "subscribe": True,
            "channels": [
                {"channel_id": A, "title": "Alpha"},
                {"channel_id": B, "title": None},
                {"channel_id": C, "title": "Чарлі"},
            ],
        }
    ]
    assert waits == [batch_timeout_seconds(3)]


def test_a_batch_without_channels_starts_no_child(runner, fake_starter):
    assert change_subscriptions_in_child(runner, False, []) == SubscriptionBatchResult(False, ())

    assert fake_starter.calls == []


def test_the_parent_reads_the_result_that_the_child_writes(
    settings, db_sessions, fake_client, capsys, runner, fake_starter
):
    store_cookies(db_sessions, settings)
    fake_client.errors = {B: [WriteRejectedError("YouTube did not confirm the change")]}
    assert change_subscriptions_once(settings, batch_text(False, A, B)) == 0
    fake_starter.plan.append({"stderr": last_stderr_line(capsys) + "\n"})

    result = change_subscriptions_in_child(runner, False, [BatchChannel(A), BatchChannel(B)])

    assert result.outcomes == (
        ChannelOutcome(A, OutcomeKind.CONFIRMED, None),
        ChannelOutcome(B, OutcomeKind.REJECTED, "YouTube did not confirm the change"),
    )


@pytest.mark.parametrize(
    "stderr",
    [
        "",
        "a warning only\n",
        "[]\n",
        batch_line(False, outcome(A), outcome(B)),
        json.dumps({"subscribe": 1, "outcomes": [outcome(A), outcome(B)], "stopped": None}) + "\n",
        json.dumps({"subscribe": True, "outcomes": [outcome(A), outcome(B)]}) + "\n",
        json.dumps({"subscribe": True, "outcomes": {}, "stopped": None}) + "\n",
        batch_line(True, outcome(A), outcome(B), stopped={"reason": "network", "cause": "x"}),
        batch_line(True, outcome(A)),
        batch_line(True, outcome(B), outcome(A)),
        batch_line(True, outcome(A), outcome(B), outcome(C)),
        batch_line(True, outcome(A), outcome(B, "failed", "x")),
        batch_line(True, outcome(A), outcome(B, "skipped", "x")),
        batch_line(True, outcome(A), outcome(B, "rejected", 5)),
        batch_line(True, outcome(A), {**outcome(B), "extra": 1}),
        batch_line(True, outcome(A), [B]),
        batch_line(True, outcome(A), outcome(B)) + "a warning after the result\n",
    ],
)
def test_an_exit_code_0_without_the_outcome_of_each_channel_is_a_process_error(runner, fake_starter, stderr):
    fake_starter.plan.append({"stderr": stderr})

    with pytest.raises(ChildFailed, match="ended without the outcome of each channel"):
        change_subscriptions_in_child(runner, True, [BatchChannel(A), BatchChannel(B)])


@pytest.mark.parametrize(
    ("exit_code", "error_class"),
    [
        (EXIT_UNAVAILABLE, ChildUnavailable),
        (EXIT_LOGGED_OUT, ChildLoggedOut),
        (EXIT_YOUTUBE_ERROR, ChildYouTubeError),
    ],
)
def test_a_stopped_batch_gives_the_error_of_its_exit_code_with_the_last_stderr_line(
    runner, fake_starter, exit_code, error_class
):
    result = batch_line(True, outcome(A, "failed", "the cause"), stopped={"reason": "error", "cause": "the cause"})
    fake_starter.plan.append({"exit_code": exit_code, "stderr": f"{result}the cause\n"})

    with pytest.raises(error_class) as raised:
        change_subscriptions_in_child(runner, True, [BatchChannel(A)])

    assert str(raised.value) == "the cause"


def test_a_batch_that_stops_at_a_pause_gives_the_pause_to_the_parent(runner, fake_starter):
    result = batch_line(True, outcome(A), outcome(B, "failed", str(PAUSE)), stopped={"reason": "paused", "cause": "x"})
    fake_starter.plan.append({"exit_code": EXIT_PAUSED, "stderr": f"{result}{PAUSE.to_line()}\n"})

    with pytest.raises(ChildPaused) as raised:
        change_subscriptions_in_child(runner, True, [BatchChannel(A), BatchChannel(B)])

    assert raised.value.pause == PAUSE


def test_a_batch_while_a_run_is_active_starts_nothing_and_is_busy(runner, fake_starter):
    run_id = runner.claim(RunTrigger.MANUAL)
    try:
        with pytest.raises(ChildBusy):
            change_subscriptions_in_child(runner, True, [BatchChannel(A)])
    finally:
        runner.execute(run_id)

    assert len(fake_starter.calls) == 1


# A channel for a rule.


@pytest.fixture
def rule_id(db_sessions):
    with db_sessions.begin() as session:
        session.add(Playlist(playlist_id="WL", title="Watch later", is_own=True))
        rule = Rule(
            name="Science", enabled=True, position=1, condition={"==": [1, 1]}, target_playlist_id="WL", no_shorts=False
        )
        session.add(rule)
        session.flush()
        return rule.id


def rule_links(sessions):
    with sessions() as session:
        return {(link.rule_id, link.channel_id) for link in session.scalars(select(RuleChannel))}


def test_a_channel_that_is_not_stored_gets_its_row_and_goes_into_the_rule(db_sessions, rule_id):
    outcome = add_channel_to_rule(
        db_sessions, rule_id, A, title=" Alpha ", handle="@alpha", thumbnail_url="https://yt3.example/a.jpg"
    )

    assert outcome is AddOutcome.ADDED
    assert rule_links(db_sessions) == {(rule_id, A)}
    row = stored_channel(db_sessions, A)
    assert (row.title, row.handle, row.thumbnail_url, row.is_subscribed, row.mark_date, row.last_checked_at) == (
        "Alpha",
        "@alpha",
        "https://yt3.example/a.jpg",
        False,
        None,
        None,
    )


def test_a_channel_without_a_title_gets_its_id_as_the_title(db_sessions, rule_id):
    add_channel_to_rule(db_sessions, rule_id, A, title="  ")

    assert stored_channel(db_sessions, A).title == A


def test_a_stored_channel_keeps_its_row(db_sessions, rule_id):
    store(db_sessions, Channel(channel_id=A, title="Stored", handle="@stored", is_subscribed=True))

    outcome = add_channel_to_rule(db_sessions, rule_id, A, title="New", handle="@new", thumbnail_url="https://x")

    assert outcome is AddOutcome.ADDED
    row = stored_channel(db_sessions, A)
    assert (row.title, row.handle, row.thumbnail_url, row.is_subscribed) == ("Stored", "@stored", None, True)


def test_a_channel_that_the_rule_reads_already_changes_nothing(db_sessions, rule_id):
    add_channel_to_rule(db_sessions, rule_id, A, title="Alpha")

    assert add_channel_to_rule(db_sessions, rule_id, A, title="Other") is AddOutcome.ALREADY_IN_RULE

    assert rule_links(db_sessions) == {(rule_id, A)}
    assert stored_channel(db_sessions, A).title == "Alpha"


def catch_up_since(sessions, rule_id):
    with sessions() as session:
        return session.get(Rule, rule_id).catch_up_since


def test_a_new_channel_gives_the_rule_its_catch_up_day(db_sessions, rule_id):
    day = datetime.date(2026, 10, 5)

    add_channel_to_rule(db_sessions, rule_id, A, title="Alpha", catch_up_since=day)

    assert catch_up_since(db_sessions, rule_id) == day


def test_a_rule_with_an_earlier_catch_up_day_keeps_it_and_a_later_day_moves_back(db_sessions, rule_id):
    early, late = datetime.date(2026, 9, 1), datetime.date(2026, 10, 5)
    with db_sessions.begin() as session:
        session.get(Rule, rule_id).catch_up_since = early

    add_channel_to_rule(db_sessions, rule_id, A, title="Alpha", catch_up_since=late)
    assert catch_up_since(db_sessions, rule_id) == early

    with db_sessions.begin() as session:
        session.get(Rule, rule_id).catch_up_since = late + datetime.timedelta(days=3)
    add_channel_to_rule(db_sessions, rule_id, B, title="Beta", catch_up_since=late)
    assert catch_up_since(db_sessions, rule_id) == late


def test_a_channel_that_the_rule_reads_already_keeps_the_catch_up_day(db_sessions, rule_id):
    add_channel_to_rule(db_sessions, rule_id, A, title="Alpha")

    add_channel_to_rule(db_sessions, rule_id, A, title="Alpha", catch_up_since=datetime.date(2026, 10, 5))

    assert catch_up_since(db_sessions, rule_id) is None


def test_a_missing_rule_adds_no_row(db_sessions, rule_id):
    assert add_channel_to_rule(db_sessions, rule_id + 1, A, title="Alpha") is AddOutcome.NO_RULE

    assert rule_links(db_sessions) == set()
    assert stored_channel(db_sessions, A) is None


# Many channels for a rule.


def commits_of(sessions, work):
    """Run ``work`` and return the number of transactions that it committed."""
    commits = []

    def record(session):
        commits.append(session)

    event.listen(sessions, "after_commit", record)
    try:
        work()
    finally:
        event.remove(sessions, "after_commit", record)
    return len(commits)


def test_many_channels_go_into_the_rule_in_one_transaction(db_sessions, rule_id):
    store(db_sessions, Channel(channel_id=A, title="Stored", handle="@stored", is_subscribed=True))
    channels = [
        ChannelForRule(A, "New title of A", "@new"),
        ChannelForRule(B, " Beta ", "@beta", "https://yt3.example/b.jpg"),
        ChannelForRule(C, "  "),
    ]
    added = []

    commits = commits_of(
        db_sessions,
        lambda: added.append(
            add_channels_to_rule(db_sessions, rule_id, channels, catch_up_since=datetime.date(2026, 10, 5))
        ),
    )

    assert (added, commits) == ([3], 1)
    assert rule_links(db_sessions) == {(rule_id, A), (rule_id, B), (rule_id, C)}
    assert catch_up_since(db_sessions, rule_id) == datetime.date(2026, 10, 5)
    # A stored row stays as it is, and a channel that is not stored gets its row.
    stored = stored_channel(db_sessions, A)
    assert (stored.title, stored.handle, stored.is_subscribed) == ("Stored", "@stored", True)
    beta = stored_channel(db_sessions, B)
    assert (beta.title, beta.handle, beta.thumbnail_url, beta.is_subscribed) == (
        "Beta",
        "@beta",
        "https://yt3.example/b.jpg",
        False,
    )
    assert stored_channel(db_sessions, C).title == C


def test_the_channels_that_the_rule_reads_already_do_not_count(db_sessions, rule_id):
    add_channel_to_rule(db_sessions, rule_id, A, title="Alpha")

    added = add_channels_to_rule(
        db_sessions, rule_id, [ChannelForRule(A, "Other"), ChannelForRule(B, "Beta"), ChannelForRule(B, "Beta again")]
    )

    # A channel that comes twice counts once.
    assert added == 1
    assert rule_links(db_sessions) == {(rule_id, A), (rule_id, B)}
    assert (stored_channel(db_sessions, A).title, stored_channel(db_sessions, B).title) == ("Alpha", "Beta")


def test_a_rule_that_reads_each_channel_already_changes_nothing(db_sessions, rule_id):
    add_channel_to_rule(db_sessions, rule_id, A, title="Alpha")

    added = add_channels_to_rule(
        db_sessions, rule_id, [ChannelForRule(A, "Alpha")], catch_up_since=datetime.date(2026, 10, 5)
    )

    assert added == 0
    assert rule_links(db_sessions) == {(rule_id, A)}
    assert catch_up_since(db_sessions, rule_id) is None


def test_many_channels_for_a_missing_rule_add_no_row(db_sessions, rule_id):
    assert (
        add_channels_to_rule(db_sessions, rule_id + 1, [ChannelForRule(A, "Alpha"), ChannelForRule(B, "Beta")]) is None
    )

    assert rule_links(db_sessions) == set()
    assert stored_channel(db_sessions, A) is None


def test_a_failed_link_adds_no_channel_row_and_no_link(db_sessions, rule_id, monkeypatch):
    original = Session.flush

    def flush_then_fail(session, *args, **kwargs):
        # The links go to the database after the rows of the channels, in the flush of the commit.
        links = any(isinstance(row, RuleChannel) for row in session.new)
        original(session, *args, **kwargs)
        if links:
            raise RuntimeError("the database failed")

    monkeypatch.setattr(Session, "flush", flush_then_fail)

    with pytest.raises(RuntimeError, match="the database failed"):
        add_channels_to_rule(db_sessions, rule_id, [ChannelForRule(A, "Alpha"), ChannelForRule(B, "Beta")])
    monkeypatch.undo()

    # The transaction holds the rows of the channels and the links, so none of them stays.
    assert rule_links(db_sessions) == set()
    assert (stored_channel(db_sessions, A), stored_channel(db_sessions, B)) == (None, None)


# The real child process.


def test_the_real_subscribe_child_without_a_cookie_key_is_unavailable_and_changes_nothing(
    db_sessions, db_url, tmp_path, monkeypatch, capfd
):
    """Starts the real `subscribe` child: the command, the import path and the database reach the child."""
    monkeypatch.delenv("COOKIES_KEY", raising=False)
    real_runner = SyncRunner(
        db_sessions, ytdlp_dir=tmp_path / "yt-dlp", database_url=db_url, child_timeout_s=90, lock=threading.Lock()
    )

    with pytest.raises(ChildUnavailable, match="COOKIES_KEY is not set"):
        subscribe_in_child(real_runner, A, "Alpha")

    assert stored_channel(db_sessions, A) is None
    assert "COOKIES_KEY is not set" in capfd.readouterr().err


def test_the_real_batch_child_without_a_cookie_key_is_unavailable_and_changes_nothing(
    db_sessions, db_url, tmp_path, monkeypatch, capfd
):
    """Starts the real `change-subscriptions` child: the batch reaches it on its standard input."""
    monkeypatch.delenv("COOKIES_KEY", raising=False)
    real_runner = SyncRunner(
        db_sessions, ytdlp_dir=tmp_path / "yt-dlp", database_url=db_url, child_timeout_s=90, lock=threading.Lock()
    )

    with pytest.raises(ChildUnavailable, match="COOKIES_KEY is not set"):
        change_subscriptions_in_child(real_runner, True, [BatchChannel(A, "Alpha"), BatchChannel(B)])

    assert stored_channel(db_sessions, A) is None
    out = capfd.readouterr().out
    assert "The subscription batch of 2 channel(s) failed:" in out
    assert "COOKIES_KEY is not set" in out


def test_the_real_search_child_with_an_empty_query_finds_nothing_without_a_request(
    db_sessions, db_url, tmp_path, capfd
):
    """Starts the real `find-channels` child. An empty query sends no request, so the test needs no network."""
    real_runner = SyncRunner(
        db_sessions, ytdlp_dir=tmp_path / "yt-dlp", database_url=db_url, child_timeout_s=90, lock=threading.Lock()
    )

    assert find_channels_in_child(real_runner, "  ") == []


def test_the_web_process_does_not_import_yt_dlp_for_a_search_or_a_subscription_change():
    """The search and the subscription change run in child processes, so a yt-dlp update reaches each of them."""
    found_line = json.dumps([found_dict()])
    subscribed_line = json.dumps({"channel_id": A, "subscribed": True})
    unsubscribed_line = json.dumps({"channel_id": A, "subscribed": False})
    batch_result_line = json.dumps({"subscribe": True, "outcomes": [outcome(A)], "stopped": None})
    code = (
        "import io, pathlib, sys, tempfile\n"
        "from fastapi.testclient import TestClient\n"
        "from youtube_automanager.channel_actions import BatchChannel, change_subscriptions_in_child, "
        "find_channels_in_child, subscribe_in_child, unsubscribe_in_child\n"
        "from youtube_automanager.settings import Settings\n"
        "from youtube_automanager.web.app import create_app\n"
        "from youtube_automanager.web.ui import mount_ui\n"
        "\n"
        "LINES = {\n"
        f"    'find-channels': {found_line!r},\n"
        f"    'subscribe': {subscribed_line!r},\n"
        f"    'unsubscribe': {unsubscribed_line!r},\n"
        f"    'change-subscriptions': {batch_result_line!r},\n"
        "}\n"
        "\n"
        "class Child:\n"
        "    returncode = 0\n"
        "    def __init__(self, line):\n"
        "        self.stderr = io.StringIO(line + '\\n')\n"
        "    def wait(self, timeout=None):\n"
        "        return 0\n"
        "    def poll(self):\n"
        "        return 0\n"
        "    def terminate(self):\n"
        "        pass\n"
        "    kill = terminate\n"
        "\n"
        "home = pathlib.Path(tempfile.mkdtemp())\n"
        "settings = Settings(home=home, database_url=f'sqlite:///{home / \"service.sqlite\"}', tz='UTC')\n"
        "app = create_app(settings, start=lambda command, env, input_text: Child(LINES[command[3]]))\n"
        "mount_ui(app)\n"
        "with TestClient(app) as client:\n"
        "    page = client.get('/subscriptions')\n"
        "    runner = client.app.state.services.runner\n"
        "    found = find_channels_in_child(runner, '@alpha')\n"
        f"    subscribe_in_child(runner, {A!r}, 'Alpha')\n"
        f"    unsubscribe_in_child(runner, {A!r})\n"
        f"    change_subscriptions_in_child(runner, True, [BatchChannel({A!r})])\n"
        "assert page.status_code == 200, page.text\n"
        f"assert [channel.channel_id for channel in found] == [{A!r}], found\n"
        "assert 'yt_dlp' not in sys.modules, sorted(name for name in sys.modules if name.startswith('yt_dlp'))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=SOURCE_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
