"""Tests of the route that refreshes the account data. A fake child stands in for the refresh process, so no test reads
YouTube."""

import asyncio
import contextlib
import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youtube_automanager import account_refresh
from youtube_automanager.account_refresh import EXIT_ACCOUNT_ERROR, EXIT_LOGGED_OUT, EXIT_UNAVAILABLE
from youtube_automanager.db.models import Channel, Playlist
from youtube_automanager.runner import SOURCE_ROOT, SYNC_LOCK, sync_lock_held
from youtube_automanager.settings import Settings
from youtube_automanager.web.app import create_app
from youtube_automanager.youtube.session import CookieCipher, generate_key, save_cookies

pytestmark = pytest.mark.usefixtures("logging_setups")

COOKIE_VALUE = "fakeSIDvalue"
COOKIES = f"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\t{COOKIE_VALUE}\n"
URL = "/api/account/refresh"
COUNTS = {
    "channels_new": 1,
    "channels_updated": 2,
    "channels_unsubscribed": 3,
    "playlists_new": 4,
    "playlists_updated": 5,
}


class FakeRun:
    """Replaces the start of the refresh child, and records each call."""

    def __init__(self, code=0, stdout=None, stderr="", error=None):
        self.code = code
        self.stdout = json.dumps(COUNTS) + "\n" if stdout is None else stdout
        self.stderr = stderr
        self.error = error
        self.calls = []
        self.in_event_loop = None

    def __call__(self, command, env, timeout_s):
        self.calls.append((list(command), timeout_s))
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.in_event_loop = False
        else:
            self.in_event_loop = True
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(command, self.code, self.stdout, self.stderr)


@pytest.fixture
def settings(tmp_path):
    home = tmp_path / "data"
    return Settings(
        home=home,
        database_url=f"sqlite:///{home / 'service.sqlite'}",
        ytdlp_dir=home / "yt-dlp",
        tz="Europe/Kyiv",
    )


@pytest.fixture
def fake_run(monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(account_refresh, "run_child", run)
    return run


@contextlib.contextmanager
def service(settings, fake_starter):
    with TestClient(create_app(settings, start=fake_starter)) as client:
        yield client


def test_a_refresh_runs_the_child_and_answers_its_counts(settings, fake_starter, fake_run):
    with service(settings, fake_starter) as client:
        answer = client.post(URL)

    assert (answer.status_code, answer.json()) == (200, COUNTS)
    ((command, timeout),) = fake_run.calls
    assert command == [sys.executable, "-m", "youtube_automanager", "refresh-account"]
    assert timeout == account_refresh.REFRESH_TIMEOUT_SECONDS


def test_the_child_runs_while_the_route_holds_the_sync_lock(settings, fake_starter, monkeypatch):
    held = []

    def run(command, env, timeout_s):
        held.append(SYNC_LOCK.locked())
        return subprocess.CompletedProcess(command, 0, json.dumps(COUNTS) + "\n", "")

    monkeypatch.setattr(account_refresh, "run_child", run)

    with service(settings, fake_starter) as client:
        answer = client.post(URL)

    assert (answer.status_code, held, SYNC_LOCK.locked()) == (200, [True], False)


def test_the_route_waits_for_the_child_in_a_thread_and_not_in_the_event_loop(settings, fake_starter, fake_run):
    with service(settings, fake_starter) as client:
        answer = client.post(URL)

    assert answer.status_code == 200
    assert fake_run.in_event_loop is False


def test_an_account_that_is_not_available_answers_409_with_the_cause_of_the_child(settings, fake_starter, fake_run):
    fake_run.code = EXIT_UNAVAILABLE
    fake_run.stdout = ""
    fake_run.stderr = "The run needs the YouTube account, but COOKIES_KEY is not set. Set COOKIES_KEY.\n"

    with service(settings, fake_starter) as client:
        answer = client.post(URL)

    assert answer.status_code == 409
    assert answer.json() == {
        "detail": "The run needs the YouTube account, but COOKIES_KEY is not set. Set COOKIES_KEY."
    }


def test_a_logged_out_answer_of_youtube_answers_409_with_the_advice_to_upload_new_cookies(
    settings, fake_starter, fake_run
):
    fake_run.code = EXIT_LOGGED_OUT
    fake_run.stdout = ""
    fake_run.stderr = "YouTube answered https://www.youtube.com/feed/channels as to an anonymous user\n"

    with service(settings, fake_starter) as client:
        answer = client.post(URL)

    assert answer.status_code == 409
    assert answer.json()["detail"] == (
        "YouTube does not accept the stored cookies any more "
        "(YouTube answered https://www.youtube.com/feed/channels as to an anonymous user). "
        "Export new YouTube cookies from your browser and upload them to the service."
    )


def test_another_failure_of_the_read_answers_502_with_the_cause_of_the_child(settings, fake_starter, fake_run):
    fake_run.code = EXIT_ACCOUNT_ERROR
    fake_run.stdout = ""
    fake_run.stderr = "yt-dlp failed to read the channels: HTTP Error 429\n"

    with service(settings, fake_starter) as client:
        answer = client.post(URL)

    assert answer.status_code == 502
    assert answer.json() == {
        "detail": "YouTube did not give the account data: yt-dlp failed to read the channels: HTTP Error 429"
    }


def test_a_crash_of_the_child_answers_502_without_its_stderr(settings, fake_starter, fake_run):
    fake_run.code = 1
    fake_run.stdout = ""
    fake_run.stderr = f"Traceback (most recent call last):\nValueError: {COOKIE_VALUE}\n"

    with service(settings, fake_starter) as client:
        answer = client.post(URL)

    assert answer.status_code == 502
    assert answer.json() == {"detail": "The refresh process exited with code 1."}
    assert COOKIE_VALUE not in answer.text


def test_a_child_that_does_not_end_in_time_answers_502(settings, fake_starter, fake_run):
    fake_run.error = subprocess.TimeoutExpired(["refresh-account"], 300)

    with service(settings, fake_starter) as client:
        answer = client.post(URL)

    assert answer.status_code == 502
    assert answer.json() == {"detail": "The refresh did not end within 300 s, so the service stopped it."}
    assert SYNC_LOCK.locked() is False


def test_a_child_without_the_counts_answers_502(settings, fake_starter, fake_run):
    fake_run.stdout = "INFO a log line only\n"

    with service(settings, fake_starter) as client:
        answer = client.post(URL)

    assert answer.status_code == 502
    assert "without the counts" in answer.json()["detail"]


def test_an_active_run_answers_409_without_a_child_and_the_lock_is_free_after_each_refresh(
    settings, fake_starter, fake_run
):
    with service(settings, fake_starter) as client:
        with sync_lock_held():
            busy = client.post(URL)
            held_after_busy = SYNC_LOCK.locked()
        assert (busy.status_code, fake_run.calls) == (409, [])
        assert "sync run or a yt-dlp update is active" in busy.json()["detail"]
        assert held_after_busy is True

        # A failed refresh and a successful one each give the lock back.
        fake_run.code = EXIT_ACCOUNT_ERROR
        failed = client.post(URL)
        assert (failed.status_code, SYNC_LOCK.locked()) == (502, False)
        fake_run.code = 0
        succeeded = client.post(URL)
        assert (succeeded.status_code, SYNC_LOCK.locked()) == (200, False)


def test_the_route_is_not_taken_by_the_json_404_of_unknown_api_paths(settings, fake_starter, fake_run):
    with service(settings, fake_starter) as client:
        refresh = client.post(URL)
        unknown = client.post("/api/account/unknown")

    assert refresh.status_code == 200
    assert (unknown.status_code, unknown.json()) == (404, {"detail": "Not Found"})


@pytest.mark.parametrize(
    ("case", "expected"),
    [("no key", "COOKIES_KEY is not set"), ("invalid cookies", "stored cookies are not valid")],
)
def test_a_real_child_that_cannot_open_the_account_answers_409_without_a_cookie_value(
    settings, sessions, fake_starter, monkeypatch, capsys, case, expected
):
    """Starts the real `refresh-account` child. It fails before any request to YouTube."""
    if case == "no key":
        monkeypatch.delenv("COOKIES_KEY", raising=False)
    else:
        key = generate_key()
        monkeypatch.setenv("COOKIES_KEY", key)
        with sessions.begin() as session:
            save_cookies(session, CookieCipher(key), f"SID={COOKIE_VALUE}")

    with service(settings, fake_starter) as client:
        answer = client.post(URL)

    assert answer.status_code == 409
    assert expected in answer.json()["detail"]
    assert COOKIE_VALUE not in answer.text
    captured = capsys.readouterr()
    assert COOKIE_VALUE not in captured.out + captured.err
    with sessions() as session:
        assert session.scalars(select(Channel)).all() == []
        assert session.scalars(select(Playlist)).all() == []


def test_the_web_process_does_not_import_yt_dlp_for_a_refresh():
    """The refresh runs in a child process, so a yt-dlp update takes effect at the next refresh."""
    code = (
        "import pathlib, subprocess, sys, tempfile\n"
        "from fastapi.testclient import TestClient\n"
        "import youtube_automanager.account_refresh as account_refresh\n"
        "from youtube_automanager.settings import Settings\n"
        "from youtube_automanager.web.app import create_app\n"
        f"counts = {json.dumps(COUNTS)!r}\n"
        "account_refresh.run_child = lambda command, env, timeout_s: "
        "subprocess.CompletedProcess(command, 0, counts + '\\n', '')\n"
        "home = pathlib.Path(tempfile.mkdtemp())\n"
        "settings = Settings(home=home, database_url=f'sqlite:///{home / \"service.sqlite\"}', tz='UTC')\n"
        "with TestClient(create_app(settings)) as client:\n"
        "    answer = client.post('/api/account/refresh')\n"
        "assert answer.status_code == 200, answer.text\n"
        "assert answer.json()['channels_new'] == 1, answer.text\n"
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
