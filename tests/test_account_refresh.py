"""Tests of the refresh of the account data in a child process. No test reads YouTube."""

import json
import os
import sys
import threading

import pytest
from sqlalchemy import select

from youtube_automanager import account_refresh
from youtube_automanager.account_cache import REFRESHED_AT_KEY, RefreshResult
from youtube_automanager.account_refresh import (
    EXIT_ACCOUNT_ERROR,
    EXIT_LOGGED_OUT,
    EXIT_UNAVAILABLE,
    NO_ACCEPTED_VERSION,
    REFRESH_TIMEOUT_SECONDS,
    RefreshAccountError,
    RefreshBusy,
    RefreshError,
    RefreshLoggedOut,
    RefreshProcessError,
    RefreshUnavailable,
    refresh_command,
    refresh_in_child,
    refresh_once,
)
from youtube_automanager.db.models import (
    Account,
    Channel,
    CookieVersion,
    CookieVersionState,
    Playlist,
    Run,
    RunTrigger,
    SessionState,
    Setting,
)
from youtube_automanager.engine import account_session
from youtube_automanager.runner import (
    BUSY_TEXT,
    SCHEDULED_WAIT_S,
    SOURCE_ROOT,
    UNAVAILABLE_EXIT_CODE,
    SyncRunner,
)
from youtube_automanager.settings import Settings
from youtube_automanager.youtube.account import AccountError, AccountPlaylist, NotLoggedInError, SubscribedChannel
from youtube_automanager.youtube.session import CookieCipher, generate_key, save_cookies

A = "UCaaaaaaaaaaaaaaaaaaaaaa"
WAIT_SECONDS = 10
COOKIE_VALUE = "fake-sapisid"
COOKIES = (
    "# Netscape HTTP Cookie File\n"
    f".youtube.com\tTRUE\t/\tTRUE\t4000000000\tSAPISID\t{COOKIE_VALUE}\n"
    ".youtube.com\tTRUE\t/\tFALSE\t4000000000\tSIDCC\tfake-sidcc-0\n"
)
COUNTS = {
    "channels_new": 1,
    "channels_updated": 2,
    "channels_unsubscribed": 3,
    "playlists_new": 4,
    "playlists_updated": 5,
}
COUNTS_LINE = json.dumps(COUNTS)


@pytest.fixture
def settings(tmp_path, db_url):
    return Settings(
        home=tmp_path, database_url=db_url, tz="UTC", ytdlp_dir=tmp_path / "yt-dlp", cookies_key=generate_key()
    )


def store_cookies(sessions, settings, text=COOKIES):
    with sessions.begin() as session:
        save_cookies(session, CookieCipher(settings.cookies_key.get_secret_value()), text)


@pytest.fixture
def fake_client(monkeypatch):
    """Replaces the account client of the account session. A test sets ``check_error`` or ``read_error``."""

    class FakeClient:
        check_error = None
        read_error = None

        def __init__(self, text):
            self.text = text

        def check_session(self):
            if FakeClient.check_error is not None:
                raise FakeClient.check_error

        def use_cookies(self, text):
            self.text = text

        def cookies_text(self):
            return self.text.replace("fake-sidcc-0", "fake-sidcc-1")

        def subscribed_channels(self):
            if FakeClient.read_error is not None:
                raise FakeClient.read_error
            return [SubscribedChannel(A, "Alpha", "@alpha", None)]

        def playlists(self):
            return [AccountPlaylist("WL", "Watch later", True)]

    monkeypatch.setattr(account_session, "AccountClient", FakeClient)
    return FakeClient


def stored(sessions):
    """The rows that a refresh writes: the channel ids, the playlist ids and the stored refresh time."""
    with sessions() as session:
        refreshed = session.get(Setting, REFRESHED_AT_KEY)
        return (
            set(session.scalars(select(Channel.channel_id))),
            set(session.scalars(select(Playlist.playlist_id))),
            None if refreshed is None else refreshed.value,
        )


def nothing_stored():
    return (set(), set(), None)


def last_stderr_line(capsys):
    return capsys.readouterr().err.strip().splitlines()[-1]


# The body of the child.


def test_the_exit_codes_are_distinct_and_the_unavailable_code_is_the_one_of_the_session_check():
    assert EXIT_UNAVAILABLE == UNAVAILABLE_EXIT_CODE
    # 0 is success, 1 is an uncaught error of Python, and 2 is a wrong command line.
    assert len({0, 1, 2, EXIT_UNAVAILABLE, EXIT_LOGGED_OUT, EXIT_ACCOUNT_ERROR}) == 6


def test_the_child_writes_the_account_data_and_the_counts_as_the_last_line_of_stderr(
    settings, db_sessions, fake_client, capsys
):
    store_cookies(db_sessions, settings)

    assert refresh_once(settings) == 0

    assert json.loads(last_stderr_line(capsys)) == {
        "channels_new": 1,
        "channels_updated": 0,
        "channels_unsubscribed": 0,
        "playlists_new": 1,
        "playlists_updated": 0,
    }
    channel_ids, playlist_ids, refreshed = stored(db_sessions)
    assert (channel_ids, playlist_ids) == ({A}, {"WL"})
    assert refreshed is not None


def test_the_child_checks_the_session_and_saves_the_updated_cookies_like_a_run(
    settings, db_sessions, fake_client, capsys
):
    store_cookies(db_sessions, settings)

    assert refresh_once(settings) == 0

    with db_sessions() as session:
        assert session.scalars(select(Account)).one().session_state is SessionState.OK
        states = [version.state for version in session.scalars(select(CookieVersion).order_by(CookieVersion.id))]
    # The checked version is good, and the cookies that the client updated are a new version.
    assert states == [CookieVersionState.GOOD, CookieVersionState.UNCHECKED]


@pytest.mark.parametrize(
    ("with_key", "with_cookies", "expected"),
    [
        (False, False, "COOKIES_KEY is not set"),
        (True, False, "no YouTube cookies are stored"),
    ],
)
def test_without_a_key_or_cookies_the_child_ends_with_the_unavailable_code(
    settings, db_sessions, fake_client, capsys, with_key, with_cookies, expected
):
    if not with_key:
        settings = settings.model_copy(update={"cookies_key": None})
    if with_cookies:
        store_cookies(db_sessions, settings)

    assert refresh_once(settings) == EXIT_UNAVAILABLE

    assert expected in last_stderr_line(capsys)
    assert stored(db_sessions) == nothing_stored()


def test_stored_text_that_is_not_a_cookie_file_ends_with_the_unavailable_code_without_the_cookie_value(
    settings, db_sessions, capsys
):
    store_cookies(db_sessions, settings, f"SID={COOKIE_VALUE}")

    assert refresh_once(settings) == EXIT_UNAVAILABLE

    captured = capsys.readouterr()
    assert "stored cookies are not valid" in captured.err.strip().splitlines()[-1]
    assert COOKIE_VALUE not in captured.out + captured.err
    assert stored(db_sessions) == nothing_stored()


def test_a_dead_session_at_the_check_ends_with_the_logged_out_code_and_writes_the_session_as_dead(
    settings, db_sessions, fake_client, capsys
):
    store_cookies(db_sessions, settings)
    fake_client.check_error = NotLoggedInError("YouTube answered the page as to an anonymous user")

    assert refresh_once(settings) == EXIT_LOGGED_OUT

    assert last_stderr_line(capsys) == NO_ACCEPTED_VERSION
    with db_sessions() as session:
        assert session.scalars(select(Account)).one().session_state is SessionState.DEAD
    assert stored(db_sessions) == nothing_stored()


def test_a_session_check_without_an_answer_ends_with_the_account_error_code(settings, db_sessions, fake_client, capsys):
    store_cookies(db_sessions, settings)
    fake_client.check_error = AccountError("yt-dlp failed to check the session: HTTP Error 503")

    assert refresh_once(settings) == EXIT_ACCOUNT_ERROR

    assert last_stderr_line(capsys).endswith("HTTP Error 503")
    assert stored(db_sessions) == nothing_stored()


def test_a_logged_out_answer_of_a_read_ends_with_the_logged_out_code_and_the_session_dies(
    settings, db_sessions, fake_client, capsys
):
    store_cookies(db_sessions, settings)
    fake_client.read_error = NotLoggedInError("YouTube refused the session with HTTP 401")

    assert refresh_once(settings) == EXIT_LOGGED_OUT

    assert last_stderr_line(capsys) == "YouTube refused the session with HTTP 401"
    with db_sessions() as session:
        assert session.scalars(select(Account)).one().session_state is SessionState.DEAD
        # The dead session saves no updated cookies.
        assert [version.state for version in session.scalars(select(CookieVersion))] == [CookieVersionState.DEAD]
    assert stored(db_sessions) == nothing_stored()


def test_another_failure_of_a_read_ends_with_the_account_error_code_and_still_saves_the_cookies(
    settings, db_sessions, fake_client, capsys
):
    store_cookies(db_sessions, settings)
    fake_client.read_error = AccountError("yt-dlp failed to read the channels:\n  HTTP Error 429\n")

    assert refresh_once(settings) == EXIT_ACCOUNT_ERROR

    # The cause is one line.
    assert last_stderr_line(capsys) == "yt-dlp failed to read the channels: HTTP Error 429"
    with db_sessions() as session:
        assert session.scalars(select(Account)).one().session_state is SessionState.OK
        assert len(session.scalars(select(CookieVersion)).all()) == 2
    assert stored(db_sessions) == nothing_stored()


def test_an_unexpected_error_of_the_child_is_not_hidden_behind_an_exit_code(settings, db_sessions, fake_client):
    store_cookies(db_sessions, settings)
    fake_client.read_error = RuntimeError("a bug")

    with pytest.raises(RuntimeError, match="a bug"):
        refresh_once(settings)


# The start of the child and its result.


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


def in_thread(function):
    """Run ``function`` in a thread. The result is ``outcome[0]``, and it is the error when the function raised."""
    outcome = []

    def target():
        try:
            outcome.append(function())
        except Exception as error:  # noqa: BLE001
            outcome.append(error)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    return worker, outcome


def test_the_child_gets_the_command_and_the_environment_of_a_run(runner, fake_starter, settings, tmp_path):
    fake_starter.plan.append({"stderr": COUNTS_LINE + "\n"})

    refresh_in_child(runner)

    ((command, env),) = fake_starter.calls
    assert command == [sys.executable, "-m", "youtube_automanager", "refresh-account"] == refresh_command()
    assert env["PYTHONPATH"].split(os.pathsep)[:2] == [str(tmp_path / "yt-dlp"), str(SOURCE_ROOT)]
    assert env["DATABASE_URL"] == settings.database_url
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_the_child_gets_the_timeout_of_the_refresh(db_sessions, db_url, tmp_path, fake_starter):
    waits = []

    def start(command, env):
        child = fake_starter(command, env)
        wait = child.wait

        def recording_wait(timeout=None):
            waits.append(timeout)
            return wait(timeout)

        child.wait = recording_wait
        return child

    fake_starter.plan.append({"stderr": COUNTS_LINE + "\n"})
    runner = SyncRunner(
        db_sessions,
        ytdlp_dir=tmp_path / "yt-dlp",
        database_url=db_url,
        child_timeout_s=WAIT_SECONDS,
        start=start,
        lock=threading.Lock(),
    )

    refresh_in_child(runner)

    assert waits == [REFRESH_TIMEOUT_SECONDS]


def test_the_timeout_of_the_child_is_shorter_than_the_wait_of_a_scheduled_run():
    assert REFRESH_TIMEOUT_SECONDS == 300
    assert REFRESH_TIMEOUT_SECONDS < SCHEDULED_WAIT_S


def test_the_counts_come_from_the_last_line_of_stderr(runner, fake_starter):
    first = json.dumps({**COUNTS, "channels_new": 99})
    fake_starter.plan.append({"stderr": f"a warning\n{first}\nanother warning\n\n{COUNTS_LINE}\n\n"})

    assert refresh_in_child(runner) == RefreshResult(**COUNTS)


@pytest.mark.parametrize(
    "stderr",
    [
        "",
        "\n\n",
        "a warning only\n",
        '{"channels_new": 1}\n',
        json.dumps({**COUNTS, "extra": 1}) + "\n",
        json.dumps({**COUNTS, "channels_new": "1"}) + "\n",
        json.dumps({**COUNTS, "channels_new": True}) + "\n",
        json.dumps({**COUNTS, "channels_new": 1.5}) + "\n",
        "[1, 2, 3, 4, 5]\n",
        COUNTS_LINE + "\na warning after the counts\n",
    ],
)
def test_an_exit_code_0_without_the_counts_is_a_process_error(runner, fake_starter, stderr):
    fake_starter.plan.append({"stderr": stderr})

    with pytest.raises(RefreshProcessError, match="without the counts"):
        refresh_in_child(runner)


@pytest.mark.parametrize(
    ("exit_code", "error_class"),
    [
        (EXIT_UNAVAILABLE, RefreshUnavailable),
        (EXIT_LOGGED_OUT, RefreshLoggedOut),
        (EXIT_ACCOUNT_ERROR, RefreshAccountError),
    ],
)
def test_each_failure_exit_code_gives_its_own_error_with_the_last_stderr_line_as_its_text(
    runner, fake_starter, exit_code, error_class
):
    fake_starter.plan.append({"exit_code": exit_code, "stderr": "a warning\nthe cause\n\n"})

    with pytest.raises(error_class) as raised:
        refresh_in_child(runner)

    assert str(raised.value) == "the cause"
    assert isinstance(raised.value, RefreshError)


@pytest.mark.parametrize("exit_code", [EXIT_UNAVAILABLE, EXIT_LOGGED_OUT, EXIT_ACCOUNT_ERROR])
def test_a_failure_exit_code_without_stderr_still_has_a_text(runner, fake_starter, exit_code):
    fake_starter.plan.append({"exit_code": exit_code})

    with pytest.raises(RefreshError) as raised:
        refresh_in_child(runner)

    assert str(raised.value)


@pytest.mark.parametrize("exit_code", [1, 2, 6, 137])
def test_another_exit_code_is_a_crash_and_its_stderr_stays_out_of_the_error(runner, fake_starter, exit_code, capsys):
    stderr = f"Traceback (most recent call last):\nValueError: cookie {COOKIE_VALUE}\n"
    fake_starter.plan.append({"exit_code": exit_code, "stderr": stderr})

    with pytest.raises(RefreshProcessError) as raised:
        refresh_in_child(runner)

    assert str(raised.value) == f"The refresh process exited with code {exit_code}."
    assert COOKIE_VALUE not in str(raised.value)
    # The stderr of the child still reaches the log of the service.
    assert "Traceback" in capsys.readouterr().err


def test_a_child_that_does_not_end_in_time_is_killed(runner, fake_starter, monkeypatch):
    monkeypatch.setattr(account_refresh, "REFRESH_TIMEOUT_SECONDS", 0.05)
    fake_starter.plan.append({"blocking": True})

    with pytest.raises(RefreshProcessError, match="did not end within 0.05 s, so the service stopped it"):
        refresh_in_child(runner)

    assert fake_starter.children[0].killed
    assert not runner.is_active()


def test_a_child_that_cannot_start_is_a_process_error(runner, fake_starter):
    fake_starter.error = OSError("no such program")

    with pytest.raises(RefreshProcessError, match="cannot start the refresh process: no such program"):
        refresh_in_child(runner)

    assert not runner.is_active()


def test_a_refresh_while_a_run_is_active_starts_nothing_and_is_busy(runner, fake_starter):
    run_id = runner.claim(RunTrigger.MANUAL)
    try:
        with pytest.raises(RefreshBusy) as raised:
            refresh_in_child(runner)
    finally:
        runner.execute(run_id)

    assert str(raised.value) == BUSY_TEXT
    # Only the run started a child.
    assert len(fake_starter.calls) == 1
    assert fake_starter.calls[0][0][-2:] == ["--run-id", str(run_id)]


def test_a_run_does_not_start_while_a_refresh_is_active_and_a_refresh_is_not_a_run(runner, fake_starter, db_sessions):
    fake_starter.plan.append({"blocking": True, "stderr": COUNTS_LINE + "\n"})
    worker, outcome = in_thread(lambda: refresh_in_child(runner))
    assert fake_starter.child_started.wait(WAIT_SECONDS)

    assert runner.claim(RunTrigger.MANUAL) is None
    with pytest.raises(RefreshBusy):
        refresh_in_child(runner)

    fake_starter.children[0].finish(0)
    worker.join(WAIT_SECONDS)
    assert outcome == [RefreshResult(**COUNTS)]
    assert not runner.is_active()
    with db_sessions() as session:
        assert session.scalars(select(Run)).all() == []


def test_a_stop_of_the_service_stops_an_active_refresh_child(runner, fake_starter):
    fake_starter.plan.append({"blocking": True})
    worker, outcome = in_thread(lambda: refresh_in_child(runner))
    assert fake_starter.child_started.wait(WAIT_SECONDS)

    assert runner.stop(timeout=WAIT_SECONDS) is True

    worker.join(WAIT_SECONDS)
    assert fake_starter.children[0].terminated
    (error,) = outcome
    assert isinstance(error, RefreshProcessError)
    assert str(error) == "The service stopped during the refresh."


def test_a_refresh_after_the_stop_of_the_service_starts_no_child(runner, fake_starter):
    runner.stop(timeout=WAIT_SECONDS)

    with pytest.raises(RefreshProcessError, match="stopped"):
        refresh_in_child(runner)

    assert fake_starter.calls == []


# The real child process.


def test_the_real_child_without_a_cookie_key_ends_with_the_unavailable_code_and_writes_nothing(
    db_sessions, db_url, tmp_path, monkeypatch, capfd
):
    """Starts the real `refresh-account` child: the command, the import path and the database reach the child."""
    monkeypatch.delenv("COOKIES_KEY", raising=False)
    real_runner = SyncRunner(
        db_sessions,
        ytdlp_dir=tmp_path / "yt-dlp",
        database_url=db_url,
        child_timeout_s=90,
        lock=threading.Lock(),
    )

    with pytest.raises(RefreshUnavailable, match="COOKIES_KEY is not set"):
        refresh_in_child(real_runner)

    assert stored(db_sessions) == nothing_stored()
    assert "COOKIES_KEY is not set" in capfd.readouterr().err
