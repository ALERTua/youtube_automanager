"""Tests of the check of a new yt-dlp release: the body of the child and the parent that starts it.

The body runs in the test process with the locked yt-dlp, and fakes stand in for the anonymous read and for the client
of the account, so no test sends a request. Each cookie value is fake.
"""

import importlib.metadata
import io
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import ClassVar

import pytest
import yt_dlp.version
from yt_dlp import YoutubeDL
from yt_dlp.networking.common import Response
from yt_dlp.networking.exceptions import HTTPError

from youtube_automanager import ytdlp_check
from youtube_automanager.db.models import RequestBudgetState
from youtube_automanager.pacing import BUCKET_SIZE, RequestBudget
from youtube_automanager.runner import SyncRunner
from youtube_automanager.settings import Settings
from youtube_automanager.short_child import EXIT_YOUTUBE_ERROR
from youtube_automanager.youtube import account, public
from youtube_automanager.youtube.public import PublicSourceError, VideoEntry
from youtube_automanager.youtube.session import CookieCipher, generate_key, save_cookies

WAIT_SECONDS = 10
SECRET = "s3cr3tCookieValue"
KEY = generate_key()
# The folder that the locked yt-dlp comes from, and its PyPI version.
FOLDER = Path(yt_dlp.__file__).resolve().parent.parent
VERSION = importlib.metadata.version("yt-dlp")
COOKIES = f"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t4000000000\tSAPISID\t{SECRET}\n"
# The database of the request budget for the fake client, which sends no request, so the budget never opens it.
DATABASE_URL = "sqlite://"


def entry(video_id="dQw4w9WgXcQ"):
    return VideoEntry(video_id, "A video", ytdlp_check.CHECK_CHANNEL_ID, "YouTube", 60, None, None)


class FakeUploads:
    """Replaces the anonymous read of the uploads: gives ``entries``, or raises ``error``. Records each read."""

    def __init__(self):
        self.entries = [entry()]
        self.error = None
        self.reads = []

    def __call__(self, channel_id):
        self.reads.append(channel_id)
        if self.error is not None:
            raise self.error
        return (item for item in self.entries)


class FakeClient:
    """Replaces the client of the account: the session check raises ``error`` when it is set. Records each text and
    each budget."""

    error: ClassVar[BaseException | None] = None
    texts: ClassVar[list[str]] = []
    budgets: ClassVar[list[RequestBudget]] = []

    def __init__(self, text, *, budget):
        FakeClient.texts.append(text)
        FakeClient.budgets.append(budget)

    def check_session(self):
        if FakeClient.error is not None:
            raise FakeClient.error


@pytest.fixture
def uploads(monkeypatch):
    fake = FakeUploads()
    monkeypatch.setattr(public, "iter_channel_uploads", fake)
    return fake


@pytest.fixture
def client(monkeypatch):
    FakeClient.error = None
    FakeClient.texts = []
    FakeClient.budgets = []
    monkeypatch.setattr(account, "AccountClient", FakeClient)
    return FakeClient


def result(capsys):
    """Return the last stderr line of the body: its result or its cause."""
    return capsys.readouterr().err.strip().splitlines()[-1]


# The body of the child


def test_a_working_release_without_cookies_passes_after_one_anonymous_read(uploads, client, capsys):
    assert ytdlp_check.check_once(FOLDER, VERSION, "", DATABASE_URL) == 0

    assert json.loads(result(capsys)) == {"version": yt_dlp.version.__version__, "session": "not checked"}
    assert uploads.reads == [ytdlp_check.CHECK_CHANNEL_ID]
    assert client.texts == []


@pytest.mark.parametrize(
    ("error", "session"), [(None, "live"), (account.NotLoggedInError("anonymous"), "logged out")], ids=["live", "out"]
)
def test_a_live_session_and_a_logged_out_answer_both_pass(uploads, client, capsys, error, session):
    client.error = error

    assert ytdlp_check.check_once(FOLDER, VERSION, COOKIES, DATABASE_URL) == 0

    assert json.loads(result(capsys))["session"] == session
    assert client.texts == [COOKIES]
    assert [type(budget) for budget in client.budgets] == [RequestBudget]


def test_the_session_check_takes_a_token_of_the_budget_of_the_database(
    uploads, settings, sessions, monkeypatch, capsys
):
    """The real client: YouTube refuses the session with HTTP 401, and the request still took its token."""

    def refuse(ydl, request):
        raise HTTPError(Response(io.BytesIO(b""), request.url, {}, status=401))

    monkeypatch.setattr(YoutubeDL, "urlopen", refuse)

    assert ytdlp_check.check_once(FOLDER, VERSION, COOKIES, settings.database_url) == 0

    assert json.loads(result(capsys))["session"] == "logged out"
    with sessions() as session:
        row = session.get(RequestBudgetState, 1)
    assert row.tokens == BUCKET_SIZE - 1
    assert row.last_answer_at is not None


def test_a_failed_session_check_fails_the_release(uploads, client, capsys):
    client.error = account.AccountError("yt-dlp 2026.08.19 does not fit the client: YoutubeTab.initialize raised")

    assert ytdlp_check.check_once(FOLDER, VERSION, COOKIES, DATABASE_URL) == EXIT_YOUTUBE_ERROR

    assert result(capsys) == (
        "The session check failed: yt-dlp 2026.08.19 does not fit the client: YoutubeTab.initialize raised"
    )


def test_another_error_of_the_session_check_fails_without_its_text(uploads, client, capsys, caplog):
    client.error = ValueError(f"line 2 holds {SECRET}")

    assert ytdlp_check.check_once(FOLDER, VERSION, COOKIES, DATABASE_URL) == EXIT_YOUTUBE_ERROR

    output = capsys.readouterr()
    assert output.err.strip().splitlines()[-1] == "The session check raised ValueError"
    assert SECRET not in output.err + output.out + caplog.text


def test_a_release_from_another_folder_fails(uploads, client, capsys, tmp_path):
    assert ytdlp_check.check_once(tmp_path, VERSION, "", DATABASE_URL) == EXIT_YOUTUBE_ERROR

    assert result(capsys).startswith("The child imported yt-dlp from ")
    assert uploads.reads == []


def test_a_release_of_another_version_fails(uploads, client, capsys):
    assert ytdlp_check.check_once(FOLDER, "2020.1.1", "", DATABASE_URL) == EXIT_YOUTUBE_ERROR

    assert result(capsys) == f"yt-dlp says that it is {yt_dlp.version.__version__}, but the update installed 2020.1.1"


@pytest.mark.parametrize(
    ("module_version", "installed", "same"),
    [
        ("2026.08.19", "2026.8.19", True),
        ("2026.10.10.233141", "2026.10.10.233141.dev0", True),
        ("2026.08.19", "2026.8.20", False),
        ("not a version", "2026.8.19", False),
    ],
)
def test_the_version_of_the_module_names_the_pypi_version(module_version, installed, same):
    assert ytdlp_check._same_release(module_version, installed) is same


def test_a_release_without_an_internal_of_the_client_fails_before_any_request(uploads, client, capsys, monkeypatch):
    monkeypatch.setattr(account, "missing_internals", lambda: ["YoutubeTab._extract_response"])

    assert ytdlp_check.check_once(FOLDER, VERSION, COOKIES, DATABASE_URL) == EXIT_YOUTUBE_ERROR

    assert result(capsys) == f"yt-dlp {yt_dlp.version.__version__} lacks YoutubeTab._extract_response"
    assert uploads.reads == []
    assert client.texts == []


@pytest.mark.parametrize(
    ("change", "cause"),
    [
        ({"error": PublicSourceError("HTTP Error 503")}, "The anonymous read of the uploads failed: "),
        ({"error": KeyError("contents")}, "The anonymous read of the uploads failed: KeyError: 'contents'"),
        ({"entries": []}, "The anonymous read of the uploads of UCBR8-60-B28hp2BmDPdntcQ gave no video"),
    ],
    ids=["network", "changed", "empty"],
)
def test_a_failed_anonymous_read_fails_the_release(uploads, client, capsys, change, cause):
    for name, value in change.items():
        setattr(uploads, name, value)

    assert ytdlp_check.check_once(FOLDER, VERSION, COOKIES, DATABASE_URL) == EXIT_YOUTUBE_ERROR

    assert result(capsys).startswith(cause)
    assert client.texts == []


# The parent


@pytest.fixture
def runner(db_sessions, db_url, tmp_path, fake_starter):
    return SyncRunner(
        db_sessions,
        ytdlp_dir=tmp_path / "data" / "yt-dlp",
        database_url=db_url,
        child_timeout_s=WAIT_SECONDS,
        start=fake_starter,
        lock=threading.Lock(),
    )


def make_settings(tmp_path, key=KEY):
    return Settings(home=tmp_path, database_url="sqlite://", tz="UTC", cookies_key=key)


def test_the_parent_checks_the_new_folder_with_the_stored_cookies_on_the_standard_input(
    db_sessions, runner, fake_starter, tmp_path
):
    with db_sessions.begin() as session:
        save_cookies(session, CookieCipher(KEY), COOKIES)
    fake_starter.plan.append({"stderr": json.dumps({"version": "2026.08.19", "session": "live"}) + "\n"})
    new_dir = tmp_path / "data" / "yt-dlp.new"

    with runner.lock_held(timeout=0):
        cause = ytdlp_check.check_release_in_child(runner, db_sessions, make_settings(tmp_path), new_dir, "2026.8.19")

    assert cause is None
    [(command, env)] = fake_starter.calls
    assert command == [
        sys.executable,
        "-m",
        "youtube_automanager",
        "check-ytdlp",
        "--folder",
        str(new_dir),
        "--version",
        "2026.8.19",
    ]
    assert env["PYTHONPATH"].split(";" if sys.platform == "win32" else ":")[0] == str(new_dir)
    assert fake_starter.inputs == [COOKIES]
    assert SECRET not in " ".join(command) + json.dumps(env)


@pytest.mark.parametrize("key", [KEY, None], ids=["no-cookies", "no-key"])
def test_the_parent_without_stored_cookies_gives_no_input(db_sessions, runner, fake_starter, tmp_path, key):
    fake_starter.plan.append({"stderr": json.dumps({"version": "2026.08.19", "session": "not checked"}) + "\n"})

    with runner.lock_held(timeout=0):
        cause = ytdlp_check.check_release_in_child(
            runner, db_sessions, make_settings(tmp_path, key), tmp_path, "2026.8.19"
        )

    assert cause is None
    assert fake_starter.inputs == [None]


@pytest.mark.parametrize(
    ("child", "expected"),
    [
        ({"exit_code": EXIT_YOUTUBE_ERROR, "stderr": "yt-dlp 2026.08.19 lacks YoutubeTab.initialize\n"}, "lacks"),
        ({"exit_code": 1, "stderr": "Traceback ...\n"}, "The check of the new yt-dlp process exited with code 1."),
        ({"exit_code": 0, "stderr": "not a result\n"}, "The check process ended without its result."),
    ],
    ids=["failed", "crashed", "no-result"],
)
def test_the_parent_gives_the_cause_of_a_failed_check(db_sessions, runner, fake_starter, tmp_path, child, expected):
    fake_starter.plan.append(child)

    with runner.lock_held(timeout=0):
        cause = ytdlp_check.check_release_in_child(runner, db_sessions, make_settings(tmp_path), tmp_path, "2026.8.19")

    assert cause is not None
    assert expected in cause


def test_the_parent_stops_a_check_that_does_not_end_in_time(db_sessions, runner, fake_starter, tmp_path, monkeypatch):
    monkeypatch.setattr(ytdlp_check, "CHECK_TIMEOUT_S", 0.05)
    fake_starter.plan.append({"blocking": True})

    with runner.lock_held(timeout=0):
        cause = ytdlp_check.check_release_in_child(runner, db_sessions, make_settings(tmp_path), tmp_path, "2026.8.19")

    assert cause == "The check of the new yt-dlp did not end within 0.05 s, so the service stopped it."
    assert fake_starter.children[0].killed


def test_the_parent_needs_the_sync_lock(db_sessions, runner, tmp_path):
    with pytest.raises(RuntimeError, match="needs the sync lock"):
        ytdlp_check.check_release_in_child(runner, db_sessions, make_settings(tmp_path), tmp_path, "2026.8.19")


def test_the_check_module_imports_no_yt_dlp():
    code = "import sys, youtube_automanager.ytdlp_check; print('yt_dlp' in sys.modules)"
    output = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout

    assert output.strip() == "False"
