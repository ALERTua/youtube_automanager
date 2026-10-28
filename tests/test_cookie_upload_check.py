"""Tests of the check of a cookie upload in a child process: the body of the child and the parent that starts it.

A fake client stands in for YouTube, so no test sends a request. Each cookie value is fake.
"""

import json
import logging
import sys
import threading

import pytest
from sqlalchemy import select

from youtube_automanager.cookie_upload_check import (
    UPLOAD_CHECK_TIMEOUT_S,
    check_upload_command,
    check_upload_in_child,
    check_upload_once,
)
from youtube_automanager.db.models import Account, CookieOrigin, CookieVersion, CookieVersionState, SessionState
from youtube_automanager.engine import account_session
from youtube_automanager.engine.account_session import UPLOAD_REFUSED_TEXT
from youtube_automanager.runner import SCHEDULED_WAIT_S, SyncRunner
from youtube_automanager.settings import Settings
from youtube_automanager.short_child import (
    EXIT_LOGGED_OUT,
    EXIT_UNAVAILABLE,
    EXIT_YOUTUBE_ERROR,
    ChildBusy,
    ChildFailed,
    ChildLoggedOut,
    ChildUnavailable,
    ChildYouTubeError,
)
from youtube_automanager.youtube.account import AccountError, NotLoggedInError
from youtube_automanager.youtube.session import CookieCipher, generate_key, save_cookies

WAIT_SECONDS = 10
SECRET = "s3cr3tCookieValue"
KEY = generate_key()
STORED_LINE = json.dumps({"stored": True})


def cookie_text(sidcc="new"):
    return (
        "# Netscape HTTP Cookie File\n"
        f".youtube.com\tTRUE\t/\tTRUE\t4000000000\tSAPISID\t{SECRET}-sapisid\n"
        f".youtube.com\tTRUE\t/\tFALSE\t4000000000\tSIDCC\t{SECRET}-sidcc-{sidcc}\n"
    )


class FakeYouTube:
    """Answers the session checks: ``answer`` is "accept", "refuse" or "fail". An answer brings a new SIDCC value."""

    def __init__(self):
        self.answer = "accept"
        self.checks = []

    def client(self, text, *, budget):
        return FakeClient(self, text)


class FakeClient:
    def __init__(self, youtube, text):
        self.youtube = youtube
        self.text = text

    def check_session(self):
        self.youtube.checks.append(self.text)
        if self.youtube.answer == "refuse":
            raise NotLoggedInError("YouTube answered https://www.youtube.com as to an anonymous user")
        if self.youtube.answer == "fail":
            raise AccountError("yt-dlp failed to check the session: HTTP Error 503")

    def cookies_text(self):
        return self.text.replace("-sidcc-new", "-sidcc-renewed")


@pytest.fixture
def youtube(monkeypatch):
    fake = FakeYouTube()
    monkeypatch.setattr(account_session, "AccountClient", fake.client)
    return fake


@pytest.fixture
def settings(tmp_path, db_url):
    return Settings(home=tmp_path, database_url=db_url, tz="UTC", cookies_key=KEY)


def store_old_upload(db_sessions):
    with db_sessions.begin() as session:
        save_cookies(session, CookieCipher(KEY), cookie_text("old"))


def stored_rows(db_sessions):
    """Each column of the account and of each cookie version that the check could change."""
    with db_sessions() as session:
        accounts = [
            (row.id, row.cookies, row.session_state, row.last_checked_at, row.earliest_cookie_expiry)
            for row in session.scalars(select(Account))
        ]
        versions = [
            (row.id, row.cookies, row.origin, row.state, row.checked_at)
            for row in session.scalars(select(CookieVersion).order_by(CookieVersion.id))
        ]
        return accounts, versions


def versions(db_sessions):
    cipher = CookieCipher(KEY)
    with db_sessions() as session:
        rows = session.scalars(select(CookieVersion).order_by(CookieVersion.id))
        return [(cipher.decrypt(row.cookies), row.origin, row.state) for row in rows]


def last_line(capsys):
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    return captured.err.strip().splitlines()[-1]


# The body of the child


def test_a_text_that_youtube_accepts_is_stored_with_the_new_values_of_the_check(
    settings, db_sessions, youtube, capsys, caplog
):
    caplog.set_level(logging.DEBUG)
    store_old_upload(db_sessions)

    assert check_upload_once(settings, cookie_text()) == 0

    assert last_line(capsys) == STORED_LINE
    assert youtube.checks == [cookie_text()]
    assert versions(db_sessions) == [
        (cookie_text(), CookieOrigin.UPLOAD, CookieVersionState.GOOD),
        (cookie_text("renewed"), CookieOrigin.WRITE_BACK, CookieVersionState.UNCHECKED),
    ]
    with db_sessions() as session:
        assert session.scalars(select(Account)).one().session_state is SessionState.OK
    assert [record.getMessage() for record in caplog.records if SECRET in record.getMessage()] == []


@pytest.mark.parametrize(
    ("answer", "code", "cause"),
    [
        ("refuse", EXIT_LOGGED_OUT, UPLOAD_REFUSED_TEXT),
        (
            "fail",
            EXIT_YOUTUBE_ERROR,
            "The check of the uploaded cookies failed: yt-dlp failed to check the session: HTTP Error 503",
        ),
    ],
    ids=["refused", "no clear answer"],
)
def test_a_text_that_youtube_does_not_accept_stores_nothing_and_names_the_cause(
    settings, db_sessions, youtube, capsys, caplog, answer, code, cause
):
    store_old_upload(db_sessions)
    before = stored_rows(db_sessions)
    youtube.answer = answer

    assert check_upload_once(settings, cookie_text()) == code

    assert last_line(capsys) == cause
    assert stored_rows(db_sessions) == before
    # YouTube refused an export of the user, so the service logs no error.
    assert [record.levelno for record in caplog.records if record.levelno >= logging.ERROR] == []


def test_without_a_cookie_key_the_child_ends_with_the_unavailable_code_and_sends_no_request(
    settings, db_sessions, youtube, capsys
):
    assert check_upload_once(settings.model_copy(update={"cookies_key": None}), cookie_text()) == EXIT_UNAVAILABLE

    assert "COOKIES_KEY is not set" in last_line(capsys)
    assert youtube.checks == []
    assert stored_rows(db_sessions) == ([], [])


# The parent


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


def test_the_child_gets_the_text_on_its_standard_input_only(runner, fake_starter):
    fake_starter.plan.append({"stderr": STORED_LINE + "\n"})

    check_upload_in_child(runner, cookie_text())

    ((command, env),) = fake_starter.calls
    assert command == [sys.executable, "-m", "youtube_automanager", "check-upload"] == check_upload_command()
    assert fake_starter.inputs == [cookie_text()]
    assert SECRET not in " ".join(command) + " ".join(env.values())
    assert not runner.is_active()


def test_the_child_gets_the_timeout_of_a_session_check(db_sessions, db_url, tmp_path, fake_starter):
    waits = []

    def start(command, env, input_text):
        child = fake_starter(command, env, input_text)
        wait = child.wait

        def recording_wait(timeout=None):
            waits.append(timeout)
            return wait(timeout)

        child.wait = recording_wait
        return child

    fake_starter.plan.append({"stderr": STORED_LINE + "\n"})
    runner = SyncRunner(
        db_sessions,
        ytdlp_dir=tmp_path / "yt-dlp",
        database_url=db_url,
        child_timeout_s=WAIT_SECONDS,
        start=start,
        lock=threading.Lock(),
    )

    check_upload_in_child(runner, cookie_text())

    assert waits == [UPLOAD_CHECK_TIMEOUT_S]
    assert UPLOAD_CHECK_TIMEOUT_S < SCHEDULED_WAIT_S


@pytest.mark.parametrize(
    ("plan", "error", "text"),
    [
        ({"exit_code": EXIT_LOGGED_OUT, "stderr": UPLOAD_REFUSED_TEXT + "\n"}, ChildLoggedOut, UPLOAD_REFUSED_TEXT),
        ({"exit_code": EXIT_YOUTUBE_ERROR, "stderr": "HTTP Error 503\n"}, ChildYouTubeError, "HTTP Error 503"),
        (
            {"exit_code": EXIT_UNAVAILABLE, "stderr": "COOKIES_KEY is not set\n"},
            ChildUnavailable,
            "COOKIES_KEY is not set",
        ),
        ({"exit_code": 1, "stderr": "Traceback\n"}, ChildFailed, "The cookie check process exited with code 1."),
        (
            {"stderr": json.dumps({"stored": 1}) + "\n"},
            ChildFailed,
            "The cookie check process ended without the confirmation of the store.",
        ),
        (
            {"stderr": json.dumps({"stored": False}) + "\n"},
            ChildFailed,
            "The cookie check process ended without the confirmation of the store.",
        ),
        ({"stderr": ""}, ChildFailed, "The cookie check process ended without the confirmation of the store."),
    ],
    ids=["refused", "youtube error", "unavailable", "crash", "number instead of true", "false", "no result"],
)
def test_a_child_that_did_not_store_the_text_raises_its_error(runner, fake_starter, plan, error, text):
    fake_starter.plan.append(plan)

    with pytest.raises(error) as raised:
        check_upload_in_child(runner, cookie_text())

    assert str(raised.value) == text


def test_while_another_task_holds_the_sync_lock_no_child_starts(runner, fake_starter):
    with runner.lock_held(), pytest.raises(ChildBusy):
        check_upload_in_child(runner, cookie_text())

    assert fake_starter.calls == []


def test_the_real_child_reads_the_text_from_its_standard_input_and_names_no_value(
    db_sessions, db_url, tmp_path, monkeypatch, capfd
):
    """Starts the real `check-upload` child. A text with a broken line fails before any request."""
    monkeypatch.setenv("COOKIES_KEY", KEY)
    runner = SyncRunner(
        db_sessions, ytdlp_dir=tmp_path / "yt-dlp", database_url=db_url, child_timeout_s=90, lock=threading.Lock()
    )
    text = cookie_text() + f".youtube.com\tTRUE\t/\tTRUE\t0\tSID {SECRET}-sid\n"

    with pytest.raises(ChildUnavailable) as raised:
        check_upload_in_child(runner, text)

    # Only a child that read the text knows the number of its broken line.
    assert str(raised.value) == (
        "The uploaded cookies are not valid: line 4 of the cookie text is not a line of a Netscape cookie file"
    )
    captured = capfd.readouterr()
    assert SECRET not in captured.out + captured.err
    assert stored_rows(db_sessions) == ([], [])
