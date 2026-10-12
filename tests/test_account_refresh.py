"""Tests of the refresh of the account data in a child process. No test reads YouTube."""

import json
import os
import subprocess
import sys
import time

import pytest
from sqlalchemy import select

from youtube_automanager.account_cache import REFRESHED_AT_KEY, RefreshResult
from youtube_automanager.account_refresh import (
    EXIT_ACCOUNT_ERROR,
    EXIT_LOGGED_OUT,
    EXIT_UNAVAILABLE,
    REFRESH_TIMEOUT_SECONDS,
    RefreshAccountError,
    RefreshError,
    RefreshLoggedOut,
    RefreshProcessError,
    RefreshUnavailable,
    refresh_in_child,
    refresh_once,
    run_child,
)
from youtube_automanager.db.models import Channel, Playlist, Setting
from youtube_automanager.engine import sync as sync_engine
from youtube_automanager.engine.sync import AccountUnavailable
from youtube_automanager.runner import SOURCE_ROOT
from youtube_automanager.settings import Settings
from youtube_automanager.youtube.account import AccountError, AccountPlaylist, NotLoggedInError, SubscribedChannel

A = "UCaaaaaaaaaaaaaaaaaaaaaa"
COOKIE_VALUE = "fakeSIDvalue"
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
    return Settings(home=tmp_path, database_url=db_url, tz="UTC", ytdlp_dir=tmp_path / "yt-dlp")


class FakeClient:
    def __init__(self, error=None):
        self.error = error

    def subscribed_channels(self):
        if self.error is not None:
            raise self.error
        return [SubscribedChannel(A, "Alpha", "@alpha", None)]

    def playlists(self):
        return [AccountPlaylist("WL", "Watch later", True)]


def use_client(monkeypatch, client=None, error=None):
    """Replace ``open_account`` of the sync engine: it gives ``client``, or raises ``error``."""

    def open_account(sessions, given_settings):
        if error is not None:
            raise error
        return client

    monkeypatch.setattr(sync_engine, "open_account", open_account)


def stored(sessions):
    with sessions() as session:
        refreshed = session.get(Setting, REFRESHED_AT_KEY)
        return (
            set(session.scalars(select(Channel.channel_id))),
            set(session.scalars(select(Playlist.playlist_id))),
            None if refreshed is None else refreshed.value,
        )


# The body of the child.


def test_the_child_writes_the_account_data_and_prints_the_counts_as_the_last_line_of_stdout(
    settings, db_sessions, monkeypatch, capsys
):
    use_client(monkeypatch, FakeClient())

    assert refresh_once(settings) == 0

    last_line = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(last_line) == {
        "channels_new": 1,
        "channels_updated": 0,
        "channels_unsubscribed": 0,
        "playlists_new": 1,
        "playlists_updated": 0,
    }
    channel_ids, playlist_ids, refreshed = stored(db_sessions)
    assert (channel_ids, playlist_ids) == ({A}, {"WL"})
    assert refreshed is not None


@pytest.mark.parametrize(
    ("error", "exit_code"),
    [
        (AccountUnavailable("The run needs the YouTube account, but COOKIES_KEY is not set."), EXIT_UNAVAILABLE),
        (NotLoggedInError("YouTube answered the page as to an anonymous user"), EXIT_LOGGED_OUT),
        (AccountError("yt-dlp failed to read the channels"), EXIT_ACCOUNT_ERROR),
    ],
)
def test_the_child_gives_each_failure_its_own_exit_code_and_writes_the_cause_last_to_stderr(
    settings, db_sessions, monkeypatch, capsys, error, exit_code
):
    use_client(monkeypatch, error=error)

    assert refresh_once(settings) == exit_code

    captured = capsys.readouterr()
    assert captured.err.strip().splitlines()[-1] == str(error)
    assert not any(line.startswith("{") for line in captured.out.splitlines())
    assert stored(db_sessions) == (set(), set(), None)


def test_the_cause_of_a_failure_is_one_line(settings, monkeypatch, capsys):
    use_client(monkeypatch, error=AccountError("yt-dlp failed:\n  first part\n\tsecond part\n"))

    assert refresh_once(settings) == EXIT_ACCOUNT_ERROR

    assert capsys.readouterr().err == "yt-dlp failed: first part second part\n"


def test_the_child_reports_a_logged_out_answer_of_a_read(settings, db_sessions, monkeypatch, capsys):
    use_client(monkeypatch, FakeClient(NotLoggedInError("YouTube refused the session with HTTP 401")))

    assert refresh_once(settings) == EXIT_LOGGED_OUT

    assert capsys.readouterr().err.strip() == "YouTube refused the session with HTTP 401"
    assert stored(db_sessions) == (set(), set(), None)


def test_an_unexpected_error_of_the_child_is_not_hidden_behind_an_exit_code(settings, monkeypatch):
    use_client(monkeypatch, error=RuntimeError("a bug"))

    with pytest.raises(RuntimeError, match="a bug"):
        refresh_once(settings)


# The start of the child and its result.


class FakeRun:
    """Replaces the start of the child: records each call, and gives ``result`` or raises ``error``."""

    def __init__(self, code=0, stdout=COUNTS_LINE + "\n", stderr="", error=None):
        self.result = subprocess.CompletedProcess(["fake"], code, stdout, stderr)
        self.error = error
        self.calls = []

    def __call__(self, command, env, timeout_s):
        self.calls.append((list(command), dict(env), timeout_s))
        if self.error is not None:
            raise self.error
        return self.result


def test_the_child_gets_the_command_the_environment_and_the_timeout_of_a_run(settings, tmp_path):
    run = FakeRun()

    refresh_in_child(settings, run=run)
    refresh_in_child(settings, timeout_s=7, run=run)

    ((command, env, timeout), (_, _, custom_timeout)) = run.calls
    assert command == [sys.executable, "-m", "youtube_automanager", "refresh-account"]
    assert env["PYTHONPATH"].split(os.pathsep)[:2] == [str(tmp_path / "yt-dlp"), str(SOURCE_ROOT)]
    assert env["DATABASE_URL"] == settings.database_url
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert (timeout, custom_timeout) == (REFRESH_TIMEOUT_SECONDS, 7)


def test_the_timeout_of_the_child_is_shorter_than_the_wait_of_a_scheduled_run():
    from youtube_automanager.runner import SCHEDULED_WAIT_SECONDS

    assert REFRESH_TIMEOUT_SECONDS == 300
    assert REFRESH_TIMEOUT_SECONDS < SCHEDULED_WAIT_SECONDS


def test_the_counts_come_from_the_last_line_of_stdout_and_the_output_goes_to_the_log_of_the_service(settings, capsys):
    first = json.dumps({**COUNTS, "channels_new": 99})
    stdout = f"INFO the log of the child\n{first}\nINFO more log\n\n{COUNTS_LINE}\n\n"

    result = refresh_in_child(settings, run=FakeRun(stdout=stdout, stderr="a warning\n"))

    assert result == RefreshResult(**COUNTS)
    captured = capsys.readouterr()
    assert "INFO the log of the child" in captured.out
    assert "a warning" in captured.err


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "\n\n",
        "INFO only a log line\n",
        '{"channels_new": 1}\n',
        json.dumps({**COUNTS, "extra": 1}) + "\n",
        json.dumps({**COUNTS, "channels_new": "1"}) + "\n",
        json.dumps({**COUNTS, "channels_new": True}) + "\n",
        json.dumps({**COUNTS, "channels_new": 1.5}) + "\n",
        "[1, 2, 3, 4, 5]\n",
        COUNTS_LINE + "\nINFO a log line after the counts\n",
    ],
)
def test_an_exit_code_0_without_the_counts_is_a_process_error(settings, stdout):
    with pytest.raises(RefreshProcessError, match="without the counts"):
        refresh_in_child(settings, run=FakeRun(stdout=stdout))


@pytest.mark.parametrize(
    ("exit_code", "error_class"),
    [
        (EXIT_UNAVAILABLE, RefreshUnavailable),
        (EXIT_LOGGED_OUT, RefreshLoggedOut),
        (EXIT_ACCOUNT_ERROR, RefreshAccountError),
    ],
)
def test_each_failure_exit_code_gives_its_own_error_with_the_last_stderr_line_as_its_text(
    settings, exit_code, error_class
):
    run = FakeRun(code=exit_code, stdout="INFO log\n", stderr="INFO log line\nthe cause\n\n")

    with pytest.raises(error_class) as raised:
        refresh_in_child(settings, run=run)

    assert str(raised.value) == "the cause"
    assert isinstance(raised.value, RefreshError)


@pytest.mark.parametrize("exit_code", [EXIT_UNAVAILABLE, EXIT_LOGGED_OUT, EXIT_ACCOUNT_ERROR])
def test_a_failure_exit_code_without_stderr_still_has_a_text(settings, exit_code):
    with pytest.raises(RefreshError) as raised:
        refresh_in_child(settings, run=FakeRun(code=exit_code))

    assert str(raised.value)


@pytest.mark.parametrize("exit_code", [1, 2, 6, 137, -9])
def test_another_exit_code_is_a_crash_and_its_stderr_stays_out_of_the_error(settings, exit_code, capsys):
    stderr = f"Traceback (most recent call last):\nValueError: cookie {COOKIE_VALUE}\n"

    with pytest.raises(RefreshProcessError) as raised:
        refresh_in_child(settings, run=FakeRun(code=exit_code, stderr=stderr))

    assert str(raised.value) == f"The refresh process exited with code {exit_code}."
    assert COOKIE_VALUE not in str(raised.value)
    # The stderr of the child still reaches the log of the service.
    assert "Traceback" in capsys.readouterr().err


def test_a_child_that_does_not_end_in_time_is_a_process_error(settings):
    run = FakeRun(error=subprocess.TimeoutExpired(["fake"], 5))

    with pytest.raises(RefreshProcessError, match="did not end within 5 s, so the service stopped it"):
        refresh_in_child(settings, timeout_s=5, run=run)


def test_a_child_that_cannot_start_is_a_process_error(settings):
    with pytest.raises(RefreshProcessError, match="cannot start the refresh process: no such program"):
        refresh_in_child(settings, run=FakeRun(error=OSError("no such program")))


def test_a_bug_of_the_start_is_not_hidden(settings):
    with pytest.raises(RuntimeError, match="a bug"):
        refresh_in_child(settings, run=FakeRun(error=RuntimeError("a bug")))


# The real child process.


def test_run_child_captures_stdout_and_stderr_as_utf8_text():
    code = "import sys; print('out \\u0436'); print('err', file=sys.stderr); sys.exit(4)"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    completed = run_child([sys.executable, "-c", code], env, 60)

    assert (completed.returncode, completed.stdout.strip(), completed.stderr.strip()) == (4, "out ж", "err")


def test_run_child_kills_a_child_that_does_not_end_in_time():
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        run_child([sys.executable, "-c", "import time; time.sleep(60)"], dict(os.environ), 0.5)

    assert time.monotonic() - started < 30


def test_the_real_child_without_a_cookie_key_ends_with_the_unavailable_code_and_writes_nothing(
    settings, db_sessions, monkeypatch, capsys
):
    """Starts the real `refresh-account` child: the command, the import path and the database reach the child."""
    monkeypatch.delenv("COOKIES_KEY", raising=False)

    with pytest.raises(RefreshUnavailable, match="COOKIES_KEY is not set"):
        refresh_in_child(settings, timeout_s=120)

    assert stored(db_sessions) == (set(), set(), None)
    assert "COOKIES_KEY is not set" in capsys.readouterr().err
