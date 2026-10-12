"""Tests of the check and the store of a cookie upload. Each cookie value is fake."""

import datetime
import threading

import pytest

from youtube_automanager.db.models import Account, CookieOrigin, CookieVersion, SessionState
from youtube_automanager.runner import RunAlreadyActive, SyncRunner
from youtube_automanager.web.ui.cookie_upload import missing_login_cookies, normalized, store_upload
from youtube_automanager.youtube.cookie_text import LOGIN_COOKIES
from youtube_automanager.youtube.session import CookieCipher, generate_key, load_cookies

SECRET = "s3cr3tCookieValue"
# 2030-01-01 00:00 UTC.
EXPIRES = 1_893_456_000


def line(name: str, value: str | None = None, *, domain: str = ".youtube.com", expires: int = EXPIRES) -> str:
    flag = "TRUE" if domain.startswith(".") else "FALSE"
    return f"{domain}\t{flag}\t/\tTRUE\t{expires}\t{name}\t{value or SECRET + name}"


def cookie_text(names=LOGIN_COOKIES, *extra: str, domain: str = ".youtube.com") -> str:
    return (
        "\n".join(["# Netscape HTTP Cookie File", *(line(name, domain=domain) for name in sorted(names)), *extra])
        + "\n"
    )


@pytest.fixture
def runner(db_sessions, tmp_path):
    return SyncRunner(
        db_sessions, ytdlp_dir=tmp_path / "yt-dlp", database_url="sqlite://", child_timeout_s=1, lock=threading.Lock()
    )


@pytest.fixture
def cipher():
    return CookieCipher(generate_key())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ""),
        ("\n \n", ""),
        ("﻿# Netscape HTTP Cookie File\r\na\tb\r\n", "# Netscape HTTP Cookie File\na\tb\n"),
        ("\n\n  # Netscape HTTP Cookie File\nlast\t", "# Netscape HTTP Cookie File\nlast\t\n"),
    ],
)
def test_normalized_drops_the_byte_order_mark_and_the_blank_start_and_keeps_each_line(text, expected):
    assert normalized(text) == expected


def test_a_full_export_lacks_no_login_cookie():
    assert missing_login_cookies(cookie_text(LOGIN_COOKIES, line("GPS"), line("NID", domain=".google.com"))) == ()


def test_the_missing_login_cookies_come_sorted_by_name():
    text = cookie_text(LOGIN_COOKIES - {"SID", "LOGIN_INFO", "__Secure-3PSIDTS"})

    assert missing_login_cookies(text) == ("LOGIN_INFO", "SID", "__Secure-3PSIDTS")


def test_only_the_cookies_of_the_youtube_domain_count():
    google = cookie_text(LOGIN_COOKIES, domain=".google.com")
    host_only = cookie_text(LOGIN_COOKIES, domain="www.youtube.com")

    assert missing_login_cookies(google) == tuple(sorted(LOGIN_COOKIES))
    assert missing_login_cookies(host_only) == tuple(sorted(LOGIN_COOKIES))
    assert missing_login_cookies(cookie_text(LOGIN_COOKIES, domain=".YouTube.com")) == ()


@pytest.mark.parametrize(
    "text",
    [
        "SID\tvalue\n",
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\tsoon\tSID\t" + SECRET + "\n",
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID " + SECRET + "\n",
    ],
)
def test_a_text_that_is_not_a_netscape_cookie_file_gives_an_error_without_a_value(text):
    with pytest.raises(ValueError) as error:
        missing_login_cookies(text)

    assert SECRET not in str(error.value)


def test_the_store_keeps_the_text_encrypted_as_a_new_upload(db_sessions, runner, cipher):
    text = cookie_text()
    now = datetime.datetime(2026, 10, 12, 18, 0, tzinfo=datetime.UTC)
    with db_sessions.begin() as session:
        session.add(Account(session_state=SessionState.DEAD, expiry_warning_days=1))

    store_upload(db_sessions, runner, cipher, text, now=now)

    with db_sessions() as session:
        account = session.query(Account).one()
        (version,) = session.query(CookieVersion).all()
        assert load_cookies(session, cipher) == text
        assert SECRET.encode() not in account.cookies
        assert (account.session_state, account.last_checked_at, account.expiry_warning_days) == (
            SessionState.UNKNOWN,
            None,
            None,
        )
        assert account.earliest_cookie_expiry == datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)
        assert (version.origin, version.created_at) == (CookieOrigin.UPLOAD, now)


def test_the_store_waits_for_no_run_and_stores_nothing_while_the_sync_lock_is_taken(db_sessions, runner, cipher):
    with runner.lock_held(), pytest.raises(RunAlreadyActive):
        store_upload(db_sessions, runner, cipher, cookie_text())

    with db_sessions() as session:
        assert session.query(Account).count() == 0
        assert session.query(CookieVersion).count() == 0
