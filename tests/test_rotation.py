"""Tests of the cookie rotation on fake answers of accounts.youtube.com. They make no network request.

A fake HTTPS handler replaces the transport of urllib, so the real cookie processing of urllib sends the cookies and
takes the new cookies of each answer. The cookie values here are fake.
"""

import datetime
import email.message
import io
import json
import logging
import socket
import threading
import urllib.error
import urllib.request
import urllib.response

import pytest
from sqlalchemy import select

from youtube_automanager.db.models import Account, CookieOrigin, CookieVersion, SessionState, Setting
from youtube_automanager.rotation import (
    INTERVAL_S,
    MAX_DELAY_S,
    NEXT_ROTATION_KEY,
    PAGE_URL,
    RETRY_S,
    ROTATE_URL,
    ROTATION_KEY,
    CookieRotation,
)
from youtube_automanager.settings import Settings
from youtube_automanager.youtube.cookie_text import cookies
from youtube_automanager.youtube.session import CookieCipher, generate_key, save_cookies

FAR = 4_000_000_000
NOW = datetime.datetime(2026, 10, 12, 18, 0, tzinfo=datetime.UTC)
ROTATION_ID = "-1234567890123456789"
EXPIRES = "Tue, 12-Oct-2027 18:00:00 GMT"

STORED = (
    "\n".join(
        [
            "# Netscape HTTP Cookie File",
            f".youtube.com\tTRUE\t/\tTRUE\t{FAR}\tSAPISID\tfake-sapisid",
            f"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t{FAR}\tLOGIN_INFO\tfake-login-info",
            f".youtube.com\tTRUE\t/\tTRUE\t{FAR}\t__Secure-1PSIDTS\tfake-1psidts-old",
            f".youtube.com\tTRUE\t/\tTRUE\t{FAR}\t__Secure-3PSIDTS\tfake-3psidts-old",
            f".youtube.com\tTRUE\t/\tFALSE\t{FAR}\tSIDCC\tfake-sidcc-old",
            f".youtube.com\tTRUE\t/\tTRUE\t{FAR}\t__Secure-1PSIDCC\tfake-1psidcc-old",
            f".youtube.com\tTRUE\t/\tTRUE\t{FAR}\t__Secure-3PSIDCC\tfake-3psidcc-old",
            f".youtube.com\tTRUE\t/\tTRUE\t{FAR}\tPREF\tf6=1",
            ".youtube.com\tTRUE\t/\tFALSE\t1700000000\tST-fake\tfake-st",
            f"www.youtube.com\tFALSE\t/\tTRUE\t{FAR}\tHOST_ONLY\tfake-host-only",
        ]
    )
    + "\n"
)

ROTATED = {
    "__Secure-1PSIDTS": "fake-1psidts-new",
    "__Secure-3PSIDTS": "fake-3psidts-new",
    "SIDCC": "fake-sidcc-new",
    "__Secure-1PSIDCC": "fake-1psidcc-new",
    "__Secure-3PSIDCC": "fake-3psidcc-new",
}
SET_COOKIES = [
    *(
        f"{name}={value}; Domain=.youtube.com; Path=/; Expires={EXPIRES}; Secure; HttpOnly"
        for name, value in ROTATED.items()
    ),
    # A new cookie name, a deletion and a cookie of the host only: the guarded write-back takes none of them.
    "__Secure-NEW=fake-new; Domain=.youtube.com; Path=/; Secure",
    "SAPISID=; Domain=.youtube.com; Path=/; Max-Age=0",
    "HOST_ONLY=fake-host; Path=/; Secure",
]


def rotate_answer(interval=600):
    return ")]}'\n" + json.dumps([["identity.hfcr", interval], ["di", 30]])


class FakeAccounts(urllib.request.HTTPSHandler):
    """Answers the rotation page and the rotation, and records each request with its headers."""

    def __init__(self):
        super().__init__()
        self.requests = []
        self.page = (200, f"<script>init('{ROTATION_ID}', 'x', 1);</script>", [])
        self.rotate = (200, rotate_answer(), SET_COOKIES)
        self.error = None

    def https_open(self, request):
        self.requests.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "headers": dict(request.header_items()),
                "body": request.data,
            }
        )
        if self.error is not None:
            raise self.error
        status, body, set_cookies = self.page if request.full_url == PAGE_URL else self.rotate
        headers = email.message.Message()
        headers["Content-Type"] = "text/html; charset=utf-8"
        for header in set_cookies:
            headers["Set-Cookie"] = header
        response = urllib.response.addinfourl(io.BytesIO(body.encode("utf-8")), headers, request.full_url, status)
        response.msg = "OK" if status == 200 else "Error"
        return response

    def cookies_sent(self, number):
        header = self.requests[number]["headers"].get("Cookie", "")
        return dict(part.split("=", 1) for part in header.split("; ") if part)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test of the rotation tried to open a network connection")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


class Harness:
    def __init__(self, sessions, tmp_path):
        self.sessions = sessions
        self.key = generate_key()
        self.cipher = CookieCipher(self.key)
        self.settings = Settings(home=tmp_path, database_url="sqlite://", tz="UTC", cookies_key=self.key)
        self.accounts = FakeAccounts()
        self.lock = threading.Lock()
        self.sleeps = []
        self.now = NOW

    def rotation(self, settings=None):
        return CookieRotation(
            self.sessions,
            settings or self.settings,
            lock=self.lock,
            handlers=[self.accounts],
            clock=lambda: self.now,
            sleep=self.sleeps.append,
        )

    def upload(self, text=STORED):
        with self.sessions.begin() as session:
            save_cookies(session, self.cipher, text, now=self.now)

    def setting(self, key, value):
        # A new row, because the change tracking of SQLAlchemy sees no change from true to 1.
        with self.sessions.begin() as session:
            row = session.get(Setting, key)
            if row is not None:
                session.delete(row)
                session.flush()
            session.add(Setting(key=key, value=value))

    def versions(self):
        with self.sessions() as session:
            rows = session.scalars(select(CookieVersion).order_by(CookieVersion.id))
            return [(row.origin, self.cipher.decrypt(row.cookies)) for row in rows]

    def next_at(self):
        with self.sessions() as session:
            row = session.get(Setting, NEXT_ROTATION_KEY)
            return None if row is None else datetime.datetime.fromisoformat(row.value)


@pytest.fixture
def harness(db_sessions, tmp_path):
    harness = Harness(db_sessions, tmp_path)
    harness.upload()
    harness.setting(ROTATION_KEY, True)
    return harness


def values(text):
    return {cookie.name: cookie.value for cookie in cookies(text)}


# A rotation


def test_a_rotation_sends_the_two_requests_of_a_browser_tab(harness):
    assert harness.rotation().turn() == INTERVAL_S

    page, rotate = harness.accounts.requests
    assert (page["method"], page["url"], rotate["method"], rotate["url"]) == ("GET", PAGE_URL, "POST", ROTATE_URL)
    assert page["headers"]["Referer"] == "https://www.youtube.com/"
    assert json.loads(rotate["body"]) == [None, ROTATION_ID, 1]
    assert rotate["headers"]["Content-type"] == "application/json"
    assert rotate["headers"]["Origin"] == "https://accounts.youtube.com"
    assert rotate["headers"]["Referer"] == PAGE_URL
    assert page["headers"]["User-agent"] == rotate["headers"]["User-agent"]
    # The two requests are a pause apart.
    assert harness.sleeps == [5.0]


def test_a_rotation_sends_only_the_cookies_of_youtube_com_that_did_not_expire(harness):
    harness.rotation().turn()

    sent = harness.accounts.cookies_sent(0)
    assert set(sent) == set(values(STORED)) - {"ST-fake", "HOST_ONLY"}
    assert sent["SAPISID"] == "fake-sapisid"


def test_a_rotation_saves_only_the_new_values_of_known_cookies_as_a_new_version(harness):
    harness.rotation().turn()

    (_, (origin, text)) = harness.versions()
    assert origin is CookieOrigin.ROTATION
    assert values(text) == values(STORED) | ROTATED
    with harness.sessions() as session:
        assert session.scalars(select(Account)).one().cookies is not None
    expires = {cookie.name: cookie.expires for cookie in cookies(text)}
    assert expires["SIDCC"] == FAR


def test_the_next_rotation_waits_for_the_interval_of_the_answer(harness):
    harness.accounts.rotate = (200, rotate_answer(1200), SET_COOKIES)
    rotation = harness.rotation()

    assert rotation.turn() == 1200
    assert harness.next_at() == NOW + datetime.timedelta(seconds=1200)

    harness.now = NOW + datetime.timedelta(seconds=1000)
    assert rotation.turn() == 200
    # A new process reads the time of the next rotation from the database.
    assert harness.rotation().turn() == 200
    assert len(harness.accounts.requests) == 2


@pytest.mark.parametrize("answer", [rotate_answer(60), rotate_answer(0), ")]}'\nnot json", ""], ids=str)
def test_the_rotation_never_comes_more_often_than_the_measured_interval(harness, answer):
    harness.accounts.rotate = (200, answer, SET_COOKIES)

    assert harness.rotation().turn() == INTERVAL_S


# No rotation


def test_the_rotation_is_off_by_default(harness):
    with harness.sessions.begin() as session:
        session.delete(session.get(Setting, ROTATION_KEY))

    assert harness.rotation().turn() == INTERVAL_S
    assert harness.accounts.requests == []


@pytest.mark.parametrize("value", [False, "yes", 1, None])
def test_a_setting_that_is_not_true_keeps_the_rotation_off(harness, value):
    harness.setting(ROTATION_KEY, value)

    harness.rotation().turn()

    assert harness.accounts.requests == []


def test_a_run_that_holds_the_sync_lock_skips_the_turn_without_a_wait(harness):
    with harness.lock:
        assert harness.rotation().turn() == RETRY_S

    assert harness.accounts.requests == []
    assert harness.next_at() is None


def test_a_dead_session_gets_no_rotation(harness):
    with harness.sessions.begin() as session:
        session.scalars(select(Account)).one().session_state = SessionState.DEAD

    assert harness.rotation().turn() == INTERVAL_S
    assert harness.accounts.requests == []


def test_without_a_cookie_key_or_cookies_there_is_no_rotation(harness, db_sessions, tmp_path):
    no_key = Settings(home=tmp_path, database_url="sqlite://", tz="UTC", cookies_key=None)
    assert harness.rotation(no_key).turn() == INTERVAL_S
    with harness.sessions.begin() as session:
        session.execute(CookieVersion.__table__.delete())

    assert harness.rotation().turn() == INTERVAL_S
    assert harness.accounts.requests == []


# Failed rotations


def test_http_429_saves_nothing_and_each_failure_doubles_the_wait(harness):
    harness.accounts.rotate = (429, "Too Many Requests", [])
    rotation = harness.rotation()
    delays = []
    for _ in range(4):
        delays.append(rotation.turn())
        harness.now += datetime.timedelta(seconds=delays[-1])

    assert delays == [2 * INTERVAL_S, 4 * INTERVAL_S, MAX_DELAY_S, MAX_DELAY_S]
    assert len(harness.versions()) == 1

    harness.accounts.rotate = (200, rotate_answer(), SET_COOKIES)
    assert rotation.turn() == INTERVAL_S


def test_a_page_without_a_rotation_id_sends_no_rotation(harness):
    harness.accounts.page = (200, "<html>no id</html>", [])

    assert harness.rotation().turn() == 2 * INTERVAL_S
    assert [request["url"] for request in harness.accounts.requests] == [PAGE_URL]


def test_a_network_error_logs_only_its_type(harness, caplog):
    harness.accounts.error = urllib.error.URLError("fake-sapisid in a strange error")
    caplog.set_level(logging.WARNING)

    assert harness.rotation().turn() == 2 * INTERVAL_S

    assert "URLError" in caplog.text
    assert "fake-sapisid" not in caplog.text
    assert len(harness.versions()) == 1
