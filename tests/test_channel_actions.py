"""Tests of the channel actions: the search and the subscription change in a child process, and a channel for a rule.

No test reads or changes YouTube: fakes stand in for the yt-dlp readers, the account client and the child process.
"""

import json
import os
import subprocess
import sys
import threading

import pytest
from sqlalchemy import select

from youtube_automanager import channel_actions
from youtube_automanager.channel_actions import (
    SEARCH_LIMIT,
    SEARCH_TIMEOUT_SECONDS,
    SUBSCRIPTION_TIMEOUT_SECONDS,
    AddOutcome,
    FoundChannel,
    add_channel_to_rule,
    change_subscription_once,
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
    ChildUnavailable,
    ChildYouTubeError,
)
from youtube_automanager.youtube import public
from youtube_automanager.youtube.account import AccountError, NotLoggedInError, WriteRejectedError
from youtube_automanager.youtube.public import ChannelInfo, PublicSourceError
from youtube_automanager.youtube.session import CookieCipher, generate_key, save_cookies

A = "UCaaaaaaaaaaaaaaaaaaaaaa"
B = "UCbbbbbbbbbbbbbbbbbbbbbb"
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
    """Replaces the account client of the account session. A test sets ``check_error`` or ``change_error``."""

    class FakeClient:
        check_error = None
        change_error = None

        def __init__(self, text):
            self.text = text

        def check_session(self):
            if FakeClient.check_error is not None:
                raise FakeClient.check_error

        def use_cookies(self, text):
            self.text = text

        def cookies_text(self):
            return self.text.replace("fake-sidcc-0", "fake-sidcc-1")

        def subscribe(self, channel_id):
            self._change("subscribe", channel_id)

        def unsubscribe(self, channel_id):
            self._change("unsubscribe", channel_id)

        def _change(self, action, channel_id):
            FakeClient.changes.append((action, channel_id))
            if FakeClient.change_error is not None:
                raise FakeClient.change_error

    FakeClient.changes = []
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


@pytest.mark.parametrize("channel_id", ["", "UC", "@handle", f"{A}x", "PLaaaaaaaaaaaaaaaaaaaaaa"])
def test_a_text_that_is_not_a_channel_id_fails_before_any_request(settings, db_sessions, fake_client, channel_id):
    store_cookies(db_sessions, settings)

    with pytest.raises(ValueError, match="not a YouTube channel id"):
        change_subscription_once(settings, channel_id, subscribe=True)

    assert fake_client.changes == []
    with db_sessions() as session:
        assert session.scalars(select(Account)).one().session_state is SessionState.UNKNOWN


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


@pytest.mark.parametrize("exit_code", [1, 2, 6, 137])
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


def test_a_missing_rule_adds_no_row(db_sessions, rule_id):
    assert add_channel_to_rule(db_sessions, rule_id + 1, A, title="Alpha") is AddOutcome.NO_RULE

    assert rule_links(db_sessions) == set()
    assert stored_channel(db_sessions, A) is None


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
    code = (
        "import io, pathlib, sys, tempfile\n"
        "from fastapi.testclient import TestClient\n"
        "from youtube_automanager.channel_actions import find_channels_in_child, subscribe_in_child, "
        "unsubscribe_in_child\n"
        "from youtube_automanager.settings import Settings\n"
        "from youtube_automanager.web.app import create_app\n"
        "from youtube_automanager.web.ui import mount_ui\n"
        "\n"
        "LINES = {\n"
        f"    'find-channels': {found_line!r},\n"
        f"    'subscribe': {subscribed_line!r},\n"
        f"    'unsubscribe': {unsubscribed_line!r},\n"
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
