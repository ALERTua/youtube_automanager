"""Tests of the route that refreshes the account data. A fake child stands in for the refresh process, so no test reads
YouTube."""

import asyncio
import contextlib
import json
import subprocess
import sys
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youtube_automanager import account_refresh
from youtube_automanager.account_refresh import EXIT_ACCOUNT_ERROR, EXIT_LOGGED_OUT, EXIT_UNAVAILABLE
from youtube_automanager.db.models import Channel, Playlist
from youtube_automanager.runner import BUSY_TEXT, SOURCE_ROOT, SYNC_LOCK, sync_lock_held
from youtube_automanager.settings import Settings
from youtube_automanager.web.app import create_app
from youtube_automanager.youtube.session import CookieCipher, generate_key, save_cookies

pytestmark = pytest.mark.usefixtures("logging_setups")

COOKIE_VALUE = "fakeSIDvalue"
URL = "/api/account/refresh"
COUNTS = {
    "channels_new": 1,
    "channels_updated": 2,
    "channels_unsubscribed": 3,
    "playlists_new": 4,
    "playlists_updated": 5,
}
COUNTS_LINE = json.dumps(COUNTS) + "\n"


class ProbedStarter:
    """Wraps the fake starter of the child process, and records the state of the sync lock and of the event loop."""

    def __init__(self, inner):
        self.inner = inner
        self.lock_held = []
        self.in_event_loop = []

    def __call__(self, command, env, input_text):
        self.lock_held.append(SYNC_LOCK.locked())
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.in_event_loop.append(False)
        else:
            self.in_event_loop.append(True)
        return self.inner(command, env, input_text)


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
def starter(fake_starter):
    """The starter of the child processes, with a child that gives the counts. A test changes ``fake_starter.plan``."""
    fake_starter.plan.append({"stderr": COUNTS_LINE})
    return ProbedStarter(fake_starter)


@contextlib.contextmanager
def service(settings, start=None):
    """Run the service. Without ``start``, it starts real child processes."""
    app = create_app(settings) if start is None else create_app(settings, start=start)
    with TestClient(app) as client:
        yield client


def stored_rows(sessions):
    with sessions() as session:
        return session.scalars(select(Channel)).all(), session.scalars(select(Playlist)).all()


def test_a_refresh_runs_the_child_and_answers_its_counts(settings, starter, fake_starter):
    with service(settings, starter) as client:
        answer = client.post(URL)

    assert (answer.status_code, answer.json()) == (200, COUNTS)
    ((command, _),) = fake_starter.calls
    assert command == [sys.executable, "-m", "youtube_automanager", "refresh-account"]


def test_the_child_runs_while_the_sync_lock_is_held_and_the_lock_is_free_after_it(settings, starter):
    with service(settings, starter) as client:
        answer = client.post(URL)

    assert (answer.status_code, starter.lock_held, SYNC_LOCK.locked()) == (200, [True], False)


def test_the_route_waits_for_the_child_in_a_thread_and_not_in_the_event_loop(settings, starter):
    with service(settings, starter) as client:
        answer = client.post(URL)

    assert answer.status_code == 200
    assert starter.in_event_loop == [False]


def test_an_account_that_is_not_available_answers_409_with_the_cause_of_the_child(settings, starter, fake_starter):
    fake_starter.plan[:] = [
        {
            "exit_code": EXIT_UNAVAILABLE,
            "stderr": "The service needs the YouTube account, but COOKIES_KEY is not set. Set COOKIES_KEY.\n",
        }
    ]

    with service(settings, starter) as client:
        answer = client.post(URL)

    assert answer.status_code == 409
    assert answer.json() == {
        "detail": "The service needs the YouTube account, but COOKIES_KEY is not set. Set COOKIES_KEY."
    }


def test_a_logged_out_answer_of_youtube_answers_409_with_the_advice_to_upload_new_cookies(
    settings, starter, fake_starter
):
    fake_starter.plan[:] = [
        {
            "exit_code": EXIT_LOGGED_OUT,
            "stderr": "YouTube answered https://www.youtube.com/feed/channels as to an anonymous user\n",
        }
    ]

    with service(settings, starter) as client:
        answer = client.post(URL)

    assert answer.status_code == 409
    assert answer.json()["detail"] == (
        "YouTube does not accept the stored cookies any more "
        "(YouTube answered https://www.youtube.com/feed/channels as to an anonymous user). "
        "Export new YouTube cookies from your browser and upload them to the service."
    )


def test_another_failure_of_the_read_answers_502_with_the_cause_of_the_child(settings, starter, fake_starter):
    fake_starter.plan[:] = [
        {"exit_code": EXIT_ACCOUNT_ERROR, "stderr": "yt-dlp failed to read the channels: HTTP Error 429\n"}
    ]

    with service(settings, starter) as client:
        answer = client.post(URL)

    assert answer.status_code == 502
    assert answer.json() == {
        "detail": "YouTube did not give the account data: yt-dlp failed to read the channels: HTTP Error 429"
    }


def test_a_crash_of_the_child_answers_502_without_its_stderr(settings, starter, fake_starter):
    fake_starter.plan[:] = [
        {"exit_code": 1, "stderr": f"Traceback (most recent call last):\nValueError: {COOKIE_VALUE}\n"}
    ]

    with service(settings, starter) as client:
        answer = client.post(URL)

    assert answer.status_code == 502
    assert answer.json() == {"detail": "The refresh process exited with code 1."}
    assert COOKIE_VALUE not in answer.text


def test_a_child_that_does_not_end_in_time_answers_502_and_frees_the_lock(settings, starter, fake_starter, monkeypatch):
    monkeypatch.setattr(account_refresh, "REFRESH_TIMEOUT_SECONDS", 0.05)
    fake_starter.plan[:] = [{"blocking": True}]

    with service(settings, starter) as client:
        answer = client.post(URL)

    assert answer.status_code == 502
    assert answer.json() == {"detail": "The refresh did not end within 0.05 s, so the service stopped it."}
    assert fake_starter.children[0].killed
    assert SYNC_LOCK.locked() is False


def test_a_child_without_the_counts_answers_502(settings, starter, fake_starter):
    fake_starter.plan[:] = [{"stderr": "a warning only\n"}]

    with service(settings, starter) as client:
        answer = client.post(URL)

    assert answer.status_code == 502
    assert "without the counts" in answer.json()["detail"]


def test_a_child_that_cannot_start_answers_502(settings, starter, fake_starter):
    fake_starter.error = OSError("no such program")

    with service(settings, starter) as client:
        answer = client.post(URL)

    assert answer.status_code == 502
    assert answer.json() == {"detail": "The service cannot start the refresh process: no such program"}


def test_an_active_run_answers_409_without_a_child_and_the_lock_is_free_after_each_refresh(
    settings, starter, fake_starter
):
    with service(settings, starter) as client:
        with sync_lock_held():
            busy = client.post(URL)
            held_after_busy = SYNC_LOCK.locked()
        assert (busy.status_code, busy.json()) == (409, {"detail": BUSY_TEXT})
        assert fake_starter.calls == []
        assert held_after_busy is True

        # A failed refresh and a successful one each give the lock back.
        fake_starter.plan[:] = [{"exit_code": EXIT_ACCOUNT_ERROR, "stderr": "failed\n"}, {"stderr": COUNTS_LINE}]
        failed = client.post(URL)
        assert (failed.status_code, SYNC_LOCK.locked()) == (502, False)
        succeeded = client.post(URL)
        assert (succeeded.status_code, SYNC_LOCK.locked()) == (200, False)


def test_a_stop_of_the_service_stops_a_refresh_child_and_the_route_answers_502(settings, starter, fake_starter):
    fake_starter.plan[:] = [{"blocking": True}]
    answers = []

    with service(settings, starter) as client:
        worker = threading.Thread(target=lambda: answers.append(client.post(URL)), daemon=True)
        worker.start()
        assert fake_starter.child_started.wait(10)

        assert client.app.state.services.runner.stop(timeout=10) is True

        worker.join(10)

    assert fake_starter.children[0].terminated
    (answer,) = answers
    assert (answer.status_code, answer.json()) == (502, {"detail": "The service stopped during the refresh."})


def test_the_route_is_not_taken_by_the_json_404_of_unknown_api_paths(settings, starter):
    with service(settings, starter) as client:
        refresh = client.post(URL)
        unknown = client.post("/api/account/unknown")

    assert refresh.status_code == 200
    assert (unknown.status_code, unknown.json()) == (404, {"detail": "Not Found"})


@pytest.mark.parametrize(
    ("case", "expected"),
    [("no key", "COOKIES_KEY is not set"), ("invalid cookies", "stored cookies are not valid")],
)
def test_a_real_child_that_cannot_open_the_account_answers_409_without_a_cookie_value(
    settings, sessions, monkeypatch, capfd, case, expected
):
    """Starts the real `refresh-account` child. It fails before any request to YouTube."""
    if case == "no key":
        monkeypatch.delenv("COOKIES_KEY", raising=False)
    else:
        key = generate_key()
        monkeypatch.setenv("COOKIES_KEY", key)
        with sessions.begin() as session:
            save_cookies(session, CookieCipher(key), f"SID={COOKIE_VALUE}")

    with service(settings) as client:
        answer = client.post(URL)

    assert answer.status_code == 409
    assert expected in answer.json()["detail"]
    assert COOKIE_VALUE not in answer.text
    captured = capfd.readouterr()
    assert COOKIE_VALUE not in captured.out + captured.err
    assert stored_rows(sessions) == ([], [])


def test_the_web_process_does_not_import_yt_dlp_for_a_refresh():
    """The refresh runs in a child process, so a yt-dlp update takes effect at the next refresh."""
    code = (
        "import io, pathlib, sys, tempfile\n"
        "from fastapi.testclient import TestClient\n"
        "from youtube_automanager.settings import Settings\n"
        "from youtube_automanager.web.app import create_app\n"
        "\n"
        "class Child:\n"
        "    returncode = 0\n"
        f"    stderr = io.StringIO({COUNTS_LINE!r})\n"
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
        "with TestClient(create_app(settings, start=lambda command, env, input_text: Child())) as client:\n"
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
