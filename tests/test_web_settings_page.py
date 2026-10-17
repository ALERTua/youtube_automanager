"""Tests of the settings page. A fake child stands in for each session check, and Apprise plugins of this module stand in
for each notification service, so no test reads YouTube or sends a real notification. The check of a cookie upload
runs its real body in the test process, with a fake client for YouTube. Each cookie value is fake."""

import asyncio
import contextlib
import datetime
import importlib.metadata
import io
import logging
import re
import subprocess
import sys
import threading
from zoneinfo import ZoneInfo

import pytest
from apprise.decorators import notify as apprise_notify
from conftest import FakeStarter
from nicegui import ui
from nicegui.elements.mixins.label_element import LabelElement
from nicegui.elements.mixins.text_element import TextElement
from nicegui.elements.upload_files import SmallFileUpload
from pydantic import SecretStr
from sqlalchemy import select
from ui_support import element_of, elements_of, eventually, running_service, set_value, store, text_of, texts_of

from youtube_automanager import i18n
from youtube_automanager.cookie_upload_check import check_upload_once
from youtube_automanager.db.models import (
    Account,
    CookieOrigin,
    CookieVersion,
    CookieVersionState,
    NotificationTarget,
    Schedule,
    ScheduleKind,
    SessionState,
    Setting,
)
from youtube_automanager.engine import account_session
from youtube_automanager.engine.modes import ProcessingMode
from youtube_automanager.notify import DEFAULT_EVENTS, EVENT_LEVELS, Event, Level
from youtube_automanager.rotation import ROTATION_KEY
from youtube_automanager.runner import SOURCE_ROOT, UNAVAILABLE_EXIT_CODE, sync_lock_held
from youtube_automanager.short_child import (
    EXIT_YOUTUBE_ERROR,
    ChildBusy,
    ChildFailed,
    ChildLoggedOut,
    ChildUnavailable,
    ChildYouTubeError,
)
from youtube_automanager.web.ui.cookie_upload import COMPOSE_KEY_COMMAND, KEY_COMMAND, MAX_FILE_BYTES
from youtube_automanager.web.ui.notification_targets import EVENT_LABELS, LEVEL_LABELS
from youtube_automanager.web.ui.settings_page import expired_text, upload_error_text
from youtube_automanager.youtube.account import AccountError, NotLoggedInError
from youtube_automanager.youtube.cookie_text import LOGIN_COOKIES
from youtube_automanager.youtube.session import CookieCipher, generate_key, load_cookies, save_cookies

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]

KEY = generate_key()
SECRET = "s3cr3tCookieValue"
TOKEN = "s3cr3tT0ken"
# 2030-01-01 00:00 UTC, which is 02:00 in Kyiv.
EXPIRES = 1_893_456_000
TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+")
# How long a test waits for a click that must change nothing. A click that does change something shows it within ms.
SETTLE_SECONDS = 0.3
BUSY_TEXT = "Another task works with YouTube now"
SAVED_TEXT = "The settings are saved."
GONE_TEXT = "The target does not exist any more."
CHECKED_AT = datetime.datetime(2026, 10, 12, 15, 30, tzinfo=datetime.UTC)

SENT = []
# The plugin "settingsslow" waits for this event, so a test sees the page while a send runs.
SLOW_SEND = threading.Event()


@apprise_notify(on="settingsok")
def _deliver_ok(body, title, notify_type, *args, meta, **kwargs):
    SENT.append({"host": meta.get("host"), "title": title})
    return True


@apprise_notify(on="settingsfail")
def _deliver_fail(body, title, notify_type, *args, meta, **kwargs):
    SENT.append({"host": meta.get("host"), "title": title})
    return False


@apprise_notify(on="settingsslow")
def _deliver_slow(body, title, notify_type, *args, meta, **kwargs):
    SLOW_SEND.wait(10)
    SENT.append({"host": meta.get("host"), "title": title})
    return True


def line(name: str, *, domain: str = ".youtube.com", expires: int = EXPIRES) -> str:
    flag = "TRUE" if domain.startswith(".") else "FALSE"
    return f"{domain}\t{flag}\t/\tTRUE\t{expires}\t{name}\t{SECRET}-{name}"


def cookie_text(names=LOGIN_COOKIES, *extra: str) -> str:
    return "\n".join(["# Netscape HTTP Cookie File", *(line(name) for name in sorted(names)), *extra]) + "\n"


# The number of the line of the cookie SID in the text of ``cookie_text()``. The header is line 1.
SID_LINE = sorted(LOGIN_COOKIES).index("SID") + 2


class CheckingStarter(FakeStarter):
    """A fake start of the child processes.

    A session check writes ``state`` into the account, as the child does. The check of an upload runs the real body of
    its child in the test process, unless ``plan`` gives the child.
    """

    def __init__(self, sessions, settings):
        super().__init__()
        self.sessions = sessions
        self.settings = settings
        self.state = SessionState.OK

    def __call__(self, command, env, input_text=None):
        if command[3:] == ["check-session"] and self.state is not None:
            with self.sessions.begin() as session:
                account = session.scalars(select(Account)).first()
                if account is not None:
                    account.session_state = self.state
                    account.last_checked_at = CHECKED_AT
        if command[3:] == ["check-upload"] and not self.plan:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = check_upload_once(self.settings, input_text)
            self.plan.append({"exit_code": code, "stderr": stderr.getvalue()})
        return super().__call__(command, env, input_text)


class FakeYouTube:
    """Answers the session check of an upload: ``answer`` is "accept", "refuse" or "fail".

    An answer brings a new value of SIDCC, as YouTube sends one.
    """

    def __init__(self):
        self.answer = "accept"
        self.checks = 0

    def client(self, text):
        youtube = self

        class Client:
            def check_session(self):
                youtube.checks += 1
                if youtube.answer == "refuse":
                    raise NotLoggedInError("YouTube answered https://www.youtube.com as to an anonymous user")
                if youtube.answer == "fail":
                    raise AccountError("yt-dlp failed to check the session: HTTP Error 503")

            def cookies_text(self):
                return renewed(text)

        return Client()


def renewed(text: str) -> str:
    """The text with the new value of SIDCC that a check brings."""
    return text.replace(f"\tSIDCC\t{SECRET}-SIDCC\n", f"\tSIDCC\t{SECRET}-SIDCC-new\n")


@pytest.fixture
def settings(settings):
    """The settings of the tests, with a cookie key."""
    return settings.model_copy(update={"cookies_key": SecretStr(KEY)})


@pytest.fixture
def fake_starter(sessions, settings):
    return CheckingStarter(sessions, settings)


@pytest.fixture(autouse=True)
def youtube(monkeypatch):
    fake = FakeYouTube()
    monkeypatch.setattr(account_session, "AccountClient", fake.client)
    return fake


@pytest.fixture(autouse=True)
def sent():
    SENT.clear()
    SLOW_SEND.clear()
    yield SENT
    # A send that still waits ends now, so the stop of the service does not wait for it.
    SLOW_SEND.set()
    SENT.clear()


def stored_text(sessions) -> str | None:
    with sessions() as session:
        return load_cookies(session, CookieCipher(KEY))


def account(sessions) -> Account | None:
    with sessions() as session:
        return session.scalars(select(Account)).first()


def versions(sessions) -> list[tuple]:
    """The text, the origin and the state of each cookie version."""
    cipher = CookieCipher(KEY)
    with sessions() as session:
        rows = session.scalars(select(CookieVersion).order_by(CookieVersion.id))
        return [(cipher.decrypt(row.cookies), row.origin, row.state) for row in rows]


def stored_rows(sessions) -> tuple[list, list]:
    """Each stored column of the account and of each cookie version."""
    with sessions() as session:
        accounts = [
            (row.id, row.cookies, row.session_state, row.last_checked_at, row.earliest_cookie_expiry)
            for row in session.scalars(select(Account))
        ]
        rows = session.scalars(select(CookieVersion).order_by(CookieVersion.id))
        return accounts, [(row.id, row.cookies, row.origin, row.state, row.created_at, row.checked_at) for row in rows]


def store_upload(sessions, text: str, state: SessionState) -> None:
    """Store ``text`` as an upload that a check gave ``state``."""
    with sessions.begin() as session:
        save_cookies(session, CookieCipher(KEY), text)
        stored = session.scalars(select(Account)).one()
        stored.session_state = state
        stored.last_checked_at = CHECKED_AT


def setting(sessions, key):
    with sessions() as session:
        row = session.get(Setting, key)
        return None if row is None else row.value


def targets(sessions) -> list[NotificationTarget]:
    with sessions() as session:
        return list(session.scalars(select(NotificationTarget).order_by(NotificationTarget.id)))


def page_dump(user) -> str:
    """Each text, value and prop of each element of the page as one text."""
    parts = []
    for element in user.client.elements.values():
        parts.append(repr(dict(element.props)))
        parts.extend(repr(getattr(element, name, None)) for name in ("text", "value", "content"))
    return "\n".join(parts)


def commands(fake_starter) -> list[list[str]]:
    return [command[3:] for command, _env in fake_starter.calls]


async def upload(user, data: bytes, name: str = "cookies.txt") -> None:
    await element_of(user, "cookie-upload").handle_uploads([SmallFileUpload(name, "text/plain", data)])


async def paste(user, text: str) -> None:
    set_value(user, "cookie-text", text)
    user.find(marker="cookie-store").click()


async def result_is(user, expected: str) -> None:
    await eventually(lambda: text_of(user, "cookie-result") == expected, f"The cookie result {expected!r}")


def draft_shown(user) -> bool:
    """True while the floating Save and Discard show."""
    return element_of(user, "settings-draft").visible


async def save(user) -> None:
    """Click the floating Save, and wait for its message and for the end of the draft."""
    count = user.notify.messages.count(SAVED_TEXT)
    user.find(marker="settings-save").click()
    await eventually(lambda: user.notify.messages.count(SAVED_TEXT) > count, "The message of the save")
    await eventually(lambda: not draft_shown(user), "No buttons of the draft")


async def confirmation_opens(user) -> None:
    await eventually(lambda: element_of(user, "mode-confirm-dialog").value, "The open confirmation of the mode")


# The page.


async def test_the_settings_page_is_the_last_page_of_the_drawer(user):
    await user.open("/")

    user.find(marker="nav:/settings").click()

    await user.should_see(marker="cookies-card")
    for marker in ("rotation-card", "mode-card", "ytdlp-card", "targets-card"):
        await user.should_see(marker=marker)


# The YouTube cookies.


async def test_a_new_service_has_no_cookies_and_no_session_check(user, fake_starter):
    await user.open("/settings")

    assert text_of(user, "no-cookies") == "No cookies are stored yet."
    assert not element_of(user, "check-session").enabled
    assert element_of(user, "cookie-upload").enabled
    await user.should_not_see(marker="cookie-key-help")

    user.find(marker="check-session").click()
    await asyncio.sleep(SETTLE_SECONDS)
    assert fake_starter.calls == []


async def test_without_a_cookie_key_the_page_tells_how_to_set_it_and_stores_nothing(
    nicegui_globals, settings, sessions, fake_starter
):
    async with running_service(settings.model_copy(update={"cookies_key": None}), fake_starter) as user:
        await user.open("/settings")

        assert (
            text_of(user, "cookie-key-missing") == "The service cannot store cookies, because COOKIES_KEY is not set."
        )
        assert text_of(user, "cookie-key-command") == KEY_COMMAND
        assert text_of(user, "cookie-key-compose") == COMPOSE_KEY_COMMAND
        await user.should_see("Then restart the service.", retries=1)
        for marker in ("cookie-upload", "cookie-text", "cookie-store"):
            assert not element_of(user, marker).enabled, marker

        # A request to the upload route still reaches the handler, which stores nothing without a key.
        await upload(user, cookie_text().encode())
        await asyncio.sleep(SETTLE_SECONDS)

    assert account(sessions) is None
    assert fake_starter.calls == []


async def test_a_cookie_key_that_is_not_valid_shows_its_cause_without_the_key(
    nicegui_globals, settings, sessions, fake_starter
):
    async with running_service(
        settings.model_copy(update={"cookies_key": SecretStr("wrong-key-text")}), fake_starter
    ) as user:
        await user.open("/settings")

        assert text_of(user, "cookie-key-invalid") == (
            "The service cannot store cookies, because COOKIES_KEY is not valid: "
            "A cookie key is not valid: it must be 32 url-safe base64-encoded bytes"
        )
        assert "wrong-key-text" not in page_dump(user)
        assert not element_of(user, "cookie-upload").enabled


async def test_the_export_steps_are_on_the_page(user):
    await user.open("/settings")

    steps = texts_of(user, "export-step")

    assert len(steps) == 7
    assert steps[0].startswith("1. Use Firefox.")
    assert "private window" in steps[1]
    assert "https://www.youtube.com/robots.txt" in steps[3]
    assert steps[5].startswith("6. Close the private window without a sign-out.")


STORED = "The cookies work and are stored."
REFUSED_KEPT = (
    "YouTube does not accept these cookies. The service kept the stored cookies. Export new cookies and upload them."
)
REFUSED_NONE = (
    "YouTube does not accept these cookies, so the service did not store them. Export new cookies and upload them."
)
NO_CLEAR_ANSWER = (
    "The check with YouTube gave no clear answer, so the service stored nothing. Upload or paste the cookies again. "
    "The cause: "
)


async def test_an_uploaded_cookie_file_is_checked_with_youtube_and_stored_because_it_works(
    user, sessions, fake_starter, youtube
):
    text = cookie_text(LOGIN_COOKIES, line("GPS"), line("NID", domain=".google.com"))
    await user.open("/settings")

    await upload(user, ("﻿" + text.replace("\n", "\r\n")).encode())

    await result_is(user, STORED)
    assert "text-negative" not in element_of(user, "cookie-result").classes
    assert user.notify.messages == [STORED]
    # One check with YouTube, with the text on the standard input of the child, and no second session check.
    assert commands(fake_starter) == [["check-upload"]]
    assert fake_starter.inputs == [text]
    assert youtube.checks == 1
    # The check brought a new value of SIDCC, which the service saved as a new version.
    assert versions(sessions) == [
        (text, CookieOrigin.UPLOAD, CookieVersionState.GOOD),
        (renewed(text), CookieOrigin.WRITE_BACK, CookieVersionState.UNCHECKED),
    ]
    assert stored_text(sessions) == renewed(text)
    assert text_of(user, "session-state") == "OK"
    assert TIME.fullmatch(text_of(user, "session-checked"))
    assert text_of(user, "session-expiry") == "2030-01-01 02:00 EET"
    assert element_of(user, "check-session").enabled


async def test_a_pasted_cookie_text_is_checked_stored_and_leaves_the_field(user, sessions, fake_starter):
    await user.open("/settings")

    await paste(user, cookie_text())

    await result_is(user, STORED)
    assert stored_text(sessions) == renewed(cookie_text())
    assert element_of(user, "cookie-text").value == ""
    assert commands(fake_starter) == [["check-upload"]]


async def test_a_new_upload_that_works_replaces_the_dead_stored_cookies(user, sessions):
    store_upload(sessions, cookie_text(LOGIN_COOKIES, line("GPS")), SessionState.DEAD)
    await user.open("/settings")
    assert text_of(user, "session-state") == "Dead"

    await paste(user, cookie_text())

    await result_is(user, STORED)
    assert [version[:2] for version in versions(sessions)] == [
        (cookie_text(), CookieOrigin.UPLOAD),
        (renewed(cookie_text()), CookieOrigin.WRITE_BACK),
    ]
    assert text_of(user, "session-state") == "OK"


@pytest.mark.parametrize(("stored", "expected"), [(True, REFUSED_KEPT), (False, REFUSED_NONE)], ids=["stored", "none"])
async def test_an_upload_that_youtube_refuses_keeps_the_stored_cookies_and_sends_no_event(
    user, sessions, fake_starter, youtube, sent, stored, expected
):
    # A target of the event of a dead session gets each such event.
    store(sessions, target(1, "Phone", url="settingsok://phone", events=["cookie_session_dead"]))
    if stored:
        store_upload(sessions, cookie_text(LOGIN_COOKIES, line("GPS")), SessionState.OK)
    before = stored_rows(sessions)
    youtube.answer = "refuse"
    await user.open("/settings")

    await paste(user, cookie_text())

    await result_is(user, expected)
    assert "text-negative" in element_of(user, "cookie-result").classes
    assert youtube.checks == 1
    assert stored_rows(sessions) == before
    assert sent == []
    assert user.notify.messages == []
    if stored:
        assert text_of(user, "session-state") == "OK"
    else:
        assert text_of(user, "no-cookies") == "No cookies are stored yet."


@pytest.mark.parametrize(
    ("answer", "plan", "cause"),
    [
        (
            "fail",
            None,
            "The check of the uploaded cookies failed: yt-dlp failed to check the session: HTTP Error 503",
        ),
        ("accept", {"exit_code": 1, "stderr": "Traceback\n"}, "The cookie check process exited with code 1."),
    ],
    ids=["youtube error", "crash"],
)
async def test_an_upload_whose_check_gives_no_clear_answer_stores_nothing_and_asks_for_a_new_try(
    user, sessions, fake_starter, youtube, answer, plan, cause
):
    store_upload(sessions, cookie_text(LOGIN_COOKIES, line("GPS")), SessionState.OK)
    before = stored_rows(sessions)
    youtube.answer = answer
    if plan is not None:
        fake_starter.plan.append(plan)
    await user.open("/settings")

    await paste(user, cookie_text())

    await result_is(user, NO_CLEAR_ANSWER + cause)
    assert "text-negative" in element_of(user, "cookie-result").classes
    assert stored_rows(sessions) == before
    assert user.notify.messages == []


async def test_an_export_whose_login_cookies_expired_names_the_date_and_starts_no_check(user, sessions, fake_starter):
    # 2026-01-01 00:00 UTC, which is 02:00 in Kyiv.
    text = cookie_text(LOGIN_COOKIES - {"SID"}, line("SID", expires=1_767_225_600))
    await user.open("/settings")

    await paste(user, text)

    await result_is(
        user, "The login cookies of this export expired on 2026-01-01 02:00 EET. Export new cookies and upload them."
    )
    assert "text-negative" in element_of(user, "cookie-result").classes
    assert fake_starter.calls == []
    assert account(sessions) is None


async def test_a_text_without_some_login_cookies_names_them_and_stores_nothing(user, sessions, fake_starter):
    await user.open("/settings")

    await paste(user, cookie_text(LOGIN_COOKIES - {"SID", "LOGIN_INFO"}))

    await result_is(
        user,
        "The text lacks these login cookies of .youtube.com: LOGIN_INFO, SID. "
        "Export the cookies again while you are signed in to YouTube.",
    )
    assert "text-negative" in element_of(user, "cookie-result").classes
    assert element_of(user, "cookie-text").value == ""
    assert account(sessions) is None
    assert fake_starter.calls == []


async def test_a_text_without_a_login_cookie_of_youtube_says_so(user, sessions):
    await user.open("/settings")

    await upload(user, cookie_text(set(), line("SID", domain=".google.com"), line("PREF")).encode())

    await result_is(
        user,
        "The text holds no login cookie of .youtube.com. "
        "Export the cookies of youtube.com while you are signed in to YouTube.",
    )
    assert account(sessions) is None


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"", "The text is empty. Upload the cookie file, or paste its text."),
        (
            b"SID=" + SECRET.encode(),
            (
                "The text is not a Netscape cookie file: the cookie text does not start with the header line of a "
                "Netscape cookie file."
            ),
        ),
        (
            cookie_text().replace(f"\tSID\t{SECRET}-SID", f"\tSID {SECRET}-SID").encode(),
            (
                f"The text is not a Netscape cookie file: line {SID_LINE} of the cookie text is not a line of a "
                "Netscape cookie file."
            ),
        ),
        (b"\xff\xfe" + SECRET.encode("utf-16-le"), "The file is no text in UTF-8, so it is no cookie file."),
        (b"#" * (MAX_FILE_BYTES + 1), "The file is larger than 1 MB, so it is no cookie file of YouTube."),
    ],
    ids=["empty", "no header", "broken line", "not utf-8", "too large"],
)
async def test_a_file_that_is_no_cookie_file_is_refused_without_a_value(user, sessions, fake_starter, data, expected):
    await user.open("/settings")

    await upload(user, data)

    await result_is(user, expected)
    assert SECRET not in page_dump(user)
    assert account(sessions) is None
    assert fake_starter.calls == []


async def test_a_file_that_the_browser_refuses_gets_the_size_text(user):
    await user.open("/settings")

    user.find(marker="cookie-upload").trigger("rejected")

    await result_is(user, "The file is larger than 1 MB, so it is no cookie file of YouTube.")


async def test_an_upload_while_the_sync_lock_is_taken_stores_nothing(user, sessions, fake_starter):
    await user.open("/settings")

    with sync_lock_held():
        await paste(user, cookie_text())
        await eventually(lambda: text_of(user, "cookie-result").startswith(BUSY_TEXT), "The busy text")

    assert account(sessions) is None
    assert fake_starter.calls == []


async def test_no_page_text_no_log_line_and_no_stderr_line_holds_a_cookie_value(
    user, sessions, youtube, caplog, capsys
):
    caplog.set_level(logging.DEBUG)
    await user.open("/settings")

    await upload(user, cookie_text().encode())
    await result_is(user, STORED)
    youtube.answer = "refuse"
    await paste(user, cookie_text(LOGIN_COOKIES, line("GPS")))
    await result_is(user, REFUSED_KEPT)
    youtube.answer = "fail"
    await paste(user, cookie_text(LOGIN_COOKIES, line("GPS")))
    await eventually(lambda: text_of(user, "cookie-result").startswith(NO_CLEAR_ANSWER), "The text of no clear answer")
    await paste(user, cookie_text(LOGIN_COOKIES - {"SID"}))
    await eventually(lambda: "SID" in text_of(user, "cookie-result"), "The text of the missing cookie")

    assert SECRET not in page_dump(user)
    assert [record.getMessage() for record in caplog.records if SECRET in record.getMessage()] == []
    assert SECRET not in capsys.readouterr().err
    assert "The settings page stored new YouTube cookies, because YouTube accepted them" in caplog.messages


async def test_the_controls_are_off_while_the_check_of_an_upload_runs(user, sessions, fake_starter):
    store(sessions, Account(cookies=b"encrypted"))
    fake_starter.plan.append({"blocking": True, "stderr": "HTTP Error 503\n"})
    await user.open("/settings")

    await paste(user, cookie_text())
    await eventually(lambda: fake_starter.children, "The start of the check")

    assert text_of(user, "cookie-result") == "The service checks the cookies with YouTube. This takes up to a minute."
    for marker in ("check-session", "cookie-upload", "cookie-text", "cookie-store"):
        assert not element_of(user, marker).enabled, marker
    user.find(marker="cookie-store").click()
    await asyncio.sleep(SETTLE_SECONDS)
    assert len(fake_starter.calls) == 1

    fake_starter.children[0].finish(EXIT_YOUTUBE_ERROR)

    await result_is(user, NO_CLEAR_ANSWER + "HTTP Error 503")
    for marker in ("check-session", "cookie-upload", "cookie-text", "cookie-store"):
        assert element_of(user, marker).enabled, marker


async def test_the_session_check_button_checks_the_stored_cookies(user, sessions, fake_starter):
    store(
        sessions,
        Account(
            cookies=b"encrypted", earliest_cookie_expiry=datetime.datetime(2026, 12, 31, 22, 0, tzinfo=datetime.UTC)
        ),
    )
    fake_starter.state = SessionState.DEAD
    await user.open("/settings")
    assert (text_of(user, "session-state"), text_of(user, "session-checked")) == ("Unknown", "Never")
    assert text_of(user, "session-expiry") == "2027-01-01 00:00 EET"

    user.find(marker="check-session").click()

    await result_is(user, "YouTube does not accept the cookies. Export new cookies and upload them.")
    await eventually(lambda: text_of(user, "session-state") == "Dead", "The new state")
    assert text_of(user, "session-checked") == "2026-10-12 18:30 EEST"
    assert commands(fake_starter) == [["check-session"]]


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        (
            {"exit_code": UNAVAILABLE_EXIT_CODE, "stderr": "COOKIES_KEY is not set\n"},
            "The session check cannot use the stored cookies: COOKIES_KEY is not set",
        ),
        ({"exit_code": 1, "stderr": "boom\n"}, "The session check failed: boom"),
    ],
    ids=["unavailable", "failed"],
)
async def test_a_session_check_that_gives_no_answer_tells_why(user, sessions, fake_starter, plan, expected):
    store(sessions, Account(cookies=b"encrypted"))
    fake_starter.state = None
    fake_starter.plan.append(plan)
    await user.open("/settings")

    user.find(marker="check-session").click()

    await result_is(user, expected)
    assert "text-negative" in element_of(user, "cookie-result").classes
    assert text_of(user, "session-state") == "Unknown"


async def test_a_session_check_while_the_sync_lock_is_taken_starts_nothing(user, sessions, fake_starter):
    store(sessions, Account(cookies=b"encrypted"))
    await user.open("/settings")

    with sync_lock_held():
        user.find(marker="check-session").click()
        await eventually(lambda: text_of(user, "cookie-result").startswith(BUSY_TEXT), "The busy text")

    assert fake_starter.calls == []


async def test_the_controls_are_off_while_the_session_check_runs(user, sessions, fake_starter):
    store(sessions, Account(cookies=b"encrypted"))
    fake_starter.plan.append({"blocking": True})
    await user.open("/settings")

    user.find(marker="check-session").click()
    await eventually(lambda: fake_starter.children, "The start of the check")

    button = element_of(user, "check-session")
    assert "loading" in button.props
    assert text_of(user, "cookie-result") == "The service checks the session with YouTube. This takes up to a minute."
    for marker in ("check-session", "cookie-upload", "cookie-text", "cookie-store"):
        assert not element_of(user, marker).enabled, marker
    user.find(marker="check-session").click()
    await asyncio.sleep(SETTLE_SECONDS)
    assert len(fake_starter.calls) == 1

    fake_starter.children[0].finish(0)

    await result_is(user, "YouTube accepts the cookies.")
    assert "loading" not in element_of(user, "check-session").props
    for marker in ("check-session", "cookie-upload", "cookie-text", "cookie-store"):
        assert element_of(user, marker).enabled, marker


# The cookie rotation.


async def test_the_rotation_is_on_by_default_and_a_save_stores_json_booleans(user, sessions, caplog):
    caplog.set_level(logging.INFO)
    await user.open("/settings")
    assert element_of(user, "rotation").value is True
    assert "about each 10 minutes" in text_of(user, "rotation-help")
    assert "It is on by default." in text_of(user, "rotation-help")

    user.find(marker="rotation").click()
    await save(user)
    assert setting(sessions, ROTATION_KEY) is False
    assert "The cookie rotation is now off" in caplog.messages

    user.find(marker="rotation").click()
    await save(user)
    assert setting(sessions, ROTATION_KEY) is True
    assert "The cookie rotation is now on" in caplog.messages


@pytest.mark.parametrize(("value", "shown"), [(True, True), (False, False), (1, False)])
async def test_the_rotation_switch_shows_the_stored_setting(
    nicegui_globals, settings, sessions, fake_starter, value, shown
):
    store(sessions, Setting(key=ROTATION_KEY, value=value))

    async with running_service(settings, fake_starter) as user:
        await user.open("/settings")

        assert element_of(user, "rotation").value is shown


async def test_the_rotation_switch_replaces_a_stored_value_of_another_type(
    nicegui_globals, settings, sessions, fake_starter
):
    store(sessions, Setting(key=ROTATION_KEY, value=1))

    async with running_service(settings, fake_starter) as user:
        await user.open("/settings")
        user.find(marker="rotation").click()
        await save(user)

    assert setting(sessions, ROTATION_KEY) is True


# The processing mode.


async def test_the_mode_of_a_new_service_is_stop_at_seen(user):
    await user.open("/settings")

    assert element_of(user, "mode").value is ProcessingMode.STOP_AT_SEEN
    assert "at least the 20 newest videos" in text_of(user, "mode-help")
    await user.should_not_see(marker="mode-count")
    await user.should_not_see(marker="mode-invalid")


async def test_the_help_text_and_the_count_follow_the_chosen_mode(user):
    await user.open("/settings")

    set_value(user, "mode", ProcessingMode.NEWEST)
    await user.should_see(marker="mode-count")
    assert element_of(user, "mode-count").value == 30
    assert text_of(user, "mode-help").startswith("Each run reads the given number of the newest videos")
    assert text_of(user, "mode-count-hint") == "Each 100 videos cost one more request to YouTube for each source."

    set_value(user, "mode", ProcessingMode.MARK_DATE)
    await user.should_not_see(marker="mode-count")
    assert "upload day" in text_of(user, "mode-help")


async def test_a_change_of_the_mode_asks_first_and_a_cancel_writes_nothing_and_keeps_the_draft(user, sessions):
    await user.open("/settings")
    set_value(user, "mode", ProcessingMode.MARK_DATE)
    user.find(marker="rotation").click()

    user.find(marker="settings-save").click()

    await confirmation_opens(user)
    assert "the videos that came since the last run are not checked" in text_of(user, "mode-confirm-text")
    user.find(marker="mode-cancel").click()
    await eventually(lambda: not element_of(user, "mode-confirm-dialog").value, "The closed confirmation")
    await asyncio.sleep(SETTLE_SECONDS)
    # The cancel stops the whole save, also of the other changed settings.
    assert (setting(sessions, "processing_mode"), setting(sessions, ROTATION_KEY)) == (None, None)
    assert draft_shown(user)
    assert element_of(user, "mode").value is ProcessingMode.MARK_DATE
    assert element_of(user, "rotation").value is False

    user.find(marker="settings-save").click()
    await confirmation_opens(user)
    user.find(marker="mode-confirm").click()

    await eventually(lambda: setting(sessions, "processing_mode") == {"mode": "mark_date"}, "The save of the mode")
    assert setting(sessions, ROTATION_KEY) is False
    await eventually(lambda: not draft_shown(user), "No buttons of the draft")


async def test_a_confirmed_change_stores_the_mode(user, sessions, caplog):
    caplog.set_level(logging.INFO)
    await user.open("/settings")
    set_value(user, "mode", ProcessingMode.NEWEST)
    set_value(user, "mode-count", 45.0)

    user.find(marker="settings-save").click()
    await confirmation_opens(user)
    user.find(marker="mode-confirm").click()

    await user.should_see("The settings are saved.")
    assert setting(sessions, "processing_mode") == {"mode": "newest", "count": 45}
    assert isinstance(setting(sessions, "processing_mode")["count"], int)
    assert not element_of(user, "mode-confirm-dialog").value
    assert "The processing mode is now newest 45" in caplog.messages

    # A change of the count is a change of the mode too.
    set_value(user, "mode-count", 46.0)
    user.find(marker="settings-save").click()
    await confirmation_opens(user)
    user.find(marker="mode-confirm").click()
    await eventually(lambda: setting(sessions, "processing_mode") == {"mode": "newest", "count": 46}, "The new count")


async def test_a_form_that_equals_the_stored_mode_has_nothing_to_save(user, sessions):
    await user.open("/settings")
    set_value(user, "mode", ProcessingMode.NEWEST)
    set_value(user, "mode-count", 45.0)
    await eventually(lambda: draft_shown(user), "The buttons of the draft")

    # The count of mode NEWEST does not count for another mode.
    set_value(user, "mode", ProcessingMode.STOP_AT_SEEN)

    await eventually(lambda: not draft_shown(user), "No buttons of the draft")
    assert setting(sessions, "processing_mode") is None


async def test_the_stored_mode_shows_on_the_page(nicegui_globals, settings, sessions, fake_starter):
    store(sessions, Setting(key="processing_mode", value={"mode": "newest", "count": 7}))

    async with running_service(settings, fake_starter) as user:
        await user.open("/settings")

        assert element_of(user, "mode").value is ProcessingMode.NEWEST
        assert element_of(user, "mode-count").value == 7
        set_value(user, "mode-count", 8.0)
        await eventually(lambda: draft_shown(user), "The buttons of the draft")
        set_value(user, "mode-count", 7.0)
        await eventually(lambda: not draft_shown(user), "No buttons of the draft")


@pytest.mark.parametrize("count", [None, 0, -3, 2.5])
async def test_a_wrong_count_shows_the_error_and_stores_nothing(user, sessions, count):
    await user.open("/settings")
    set_value(user, "mode", ProcessingMode.NEWEST)
    set_value(user, "mode-count", count)
    user.find(marker="rotation").click()

    user.find(marker="settings-save").click()

    await eventually(lambda: text_of(user, "mode-error") == "Enter a whole number of videos, 1 or more.", "The error")
    await asyncio.sleep(SETTLE_SECONDS)
    assert not element_of(user, "mode-confirm-dialog").value
    # The error stops the whole save, also of the other changed settings.
    assert (setting(sessions, "processing_mode"), setting(sessions, ROTATION_KEY)) == (None, None)

    set_value(user, "mode-count", 1)
    user.find(marker="settings-save").click()
    await confirmation_opens(user)
    await user.should_not_see(marker="mode-error")


async def test_a_stored_mode_that_is_not_valid_shows_its_cause_until_a_save(
    nicegui_globals, settings, sessions, fake_starter
):
    store(sessions, Setting(key="processing_mode", value={"mode": "fastest"}))

    async with running_service(settings, fake_starter) as user:
        await user.open("/settings")

        assert text_of(user, "mode-invalid").startswith(
            "The stored mode is not valid, so each run stops with an error: unknown mode 'fastest'"
        )
        assert element_of(user, "mode").value is ProcessingMode.STOP_AT_SEEN
        # The default mode of the form differs from the stored value, so the page offers to save it at once.
        assert draft_shown(user)
        user.find(marker="settings-save").click()
        await confirmation_opens(user)
        user.find(marker="mode-confirm").click()
        await user.should_see("The settings are saved.")
        await user.should_not_see(marker="mode-invalid")
        assert not draft_shown(user)

    assert setting(sessions, "processing_mode") == {"mode": "stop_at_seen"}


# The notification targets.


def target(target_id: int, name: str, url: str = f"settingsok://{TOKEN}", **values) -> NotificationTarget:
    values = {"min_level": 0, "events": ["run_failed"], "enabled": True} | values
    return NotificationTarget(id=target_id, name=name, url=url, **values)


async def open_editor(user, marker: str) -> None:
    user.find(marker=marker).click()
    await eventually(lambda: element_of(user, "target-dialog").value, "The open editor")
    assert elements_of(user, "target-name")


async def editor_error_is(user, expected: str) -> None:
    await eventually(lambda: text_of(user, "target-error") == expected, f"The editor error {expected!r}")


async def test_a_new_service_has_no_target(user):
    await user.open("/settings")

    assert text_of(user, "no-targets") == "No notification target yet. The button Add a target creates one."
    await user.should_not_see(marker="targets-list")


async def test_a_new_target_gets_the_default_events_and_its_card_shows_only_its_scheme(user, sessions):
    await user.open("/settings")
    await open_editor(user, "target-add")
    assert text_of(user, "target-editor-title") == "New target"
    assert element_of(user, "target-url").props["type"] == "password"
    assert element_of(user, "target-url-link").props["href"] == "https://appriseit.com/services/"

    user.find(marker="target-name").type("Phone")
    user.find(marker="target-url").type(f"settingsok://{TOKEN}/chat")
    user.find(marker="target-save").click()

    await user.should_see("The target Phone is saved.")
    (row,) = targets(sessions)
    assert (row.name, row.url, row.min_level, row.enabled) == ("Phone", f"settingsok://{TOKEN}/chat", 0, True)
    assert set(row.events) == {event.value for event in DEFAULT_EVENTS}
    assert not element_of(user, "target-dialog").value
    await eventually(lambda: elements_of(user, f"target-{row.id}"), "The card of the new target")
    assert text_of(user, f"target-{row.id}-name") == "Phone"
    assert text_of(user, f"target-{row.id}-url") == "settingsok://…"
    for event in Event:
        assert element_of(user, f"target-{row.id}-event-{event.value}").value is (event in DEFAULT_EVENTS), event
    assert element_of(user, f"target-{row.id}-level").value is Level.INFO
    assert element_of(user, f"target-{row.id}-enabled").value is True
    assert TOKEN not in page_dump(user)


def child_of(card: ui.element, element: ui.element) -> ui.element | None:
    """Return the direct child of ``card`` that holds ``element``, or None when ``card`` does not hold it."""
    while element.parent_slot is not None:
        parent = element.parent_slot.parent
        if parent is card:
            return element
        element = parent
    return None


async def test_each_checkbox_of_a_target_names_its_event_and_the_level_of_the_event(user, sessions):
    store(sessions, target(1, "Phone"))
    await user.open("/settings")

    assert element_of(user, "target-1-event-run_summary").text == "Run summary · Success"
    assert element_of(user, "target-1-event-run_failed").text == "Run failed · Failure"
    for event in Event:
        expected = f"{EVENT_LABELS[event]} · {LEVEL_LABELS[EVENT_LEVELS[event]]}"
        assert element_of(user, f"target-1-event-{event.value}").text == expected, event
    assert element_of(user, "target-1-level").props["label"] == "Lowest level"
    assert element_of(user, "target-1-enabled").text == "Enabled"


async def test_a_card_holds_the_controls_of_its_target_in_two_rows(user, sessions):
    store(sessions, target(1, "Phone"), target(2, "Desk"))
    await user.open("/settings")
    first, second = element_of(user, "target-1"), element_of(user, "target-2")

    controls = [f"target-1-{name}" for name in ("name", "url", "level", "enabled", "test", "edit", "delete")]
    top = {child_of(first, element_of(user, marker)) for marker in controls}
    events = {child_of(first, element_of(user, f"target-1-event-{event.value}")) for event in Event}
    assert first.default_slot.children == [*top, *events]
    assert events == {element_of(user, "target-1-events")}
    # The class "row" of Quasar wraps the children of a row, and the style "flex-wrap: nowrap" stops it.
    assert [row._style.get("flex-wrap") for row in first.default_slot.children] == [None, None]
    assert child_of(second, element_of(user, "target-1-name")) is None
    assert child_of(first, element_of(user, "target-2-event-run_failed")) is None


@pytest.mark.parametrize(
    ("name", "url", "expected"),
    [
        ("", "settingsok://x", "Enter a name."),
        ("Phone", "", "Enter the Apprise URL of the target."),
        (
            "Phone",
            f"https://example.com/{TOKEN}",
            "Apprise does not accept this URL. The link below lists the URL of each service.",
        ),
        ("desk", f"settingsok://{TOKEN}", "Another target has this name."),
    ],
    ids=["no name", "no url", "url that apprise refuses", "name taken"],
)
async def test_the_editor_refuses_a_target_that_cannot_work(user, sessions, name, url, expected):
    store(sessions, target(1, "Desk"))
    await user.open("/settings")
    await open_editor(user, "target-add")

    user.find(marker="target-name").type(name)
    user.find(marker="target-url").type(url)
    user.find(marker="target-save").click()

    await editor_error_is(user, expected)
    assert TOKEN not in text_of(user, "target-error")
    assert [row.name for row in targets(sessions)] == ["Desk"]
    assert element_of(user, "target-dialog").value


async def test_the_edit_form_shows_the_stored_url_and_a_new_url_replaces_it(user, sessions):
    store(sessions, target(1, "Phone"))
    await user.open("/settings")
    await open_editor(user, "target-1-edit")
    assert text_of(user, "target-editor-title") == "Change the target"
    assert element_of(user, "target-name").value == "Phone"
    url_field = element_of(user, "target-url")
    assert url_field.value == f"settingsok://{TOKEN}"
    assert url_field.props.get("type") != "password"
    assert text_of(user, "target-url-hint") == (
        "The URL names the service and its token, for example tgram://bottoken/ChatID for Telegram."
    )
    assert element_of(user, "target-url-link").props["href"] == "https://appriseit.com/services/"

    user.find(marker="target-name").clear()
    user.find(marker="target-name").type("Desk")
    user.find(marker="target-save").click()

    await user.should_see("The target Desk is saved.")
    assert [(row.name, row.url) for row in targets(sessions)] == [("Desk", f"settingsok://{TOKEN}")]
    await eventually(lambda: text_of(user, "target-1-name") == "Desk", "The new name in the card")

    await open_editor(user, "target-1-edit")
    assert element_of(user, "target-url").value == f"settingsok://{TOKEN}"
    user.find(marker="target-url").clear()
    user.find(marker="target-url").type("settingsfail://other")
    user.find(marker="target-save").click()
    await eventually(lambda: targets(sessions)[0].url == "settingsfail://other", "The new URL")
    await eventually(lambda: text_of(user, "target-1-url") == "settingsfail://…", "The new scheme in the card")


async def test_the_edit_form_refuses_an_empty_url(user, sessions):
    store(sessions, target(1, "Phone"))
    await user.open("/settings")
    await open_editor(user, "target-1-edit")

    user.find(marker="target-url").clear()
    user.find(marker="target-url").type("  ")
    user.find(marker="target-save").click()

    await editor_error_is(user, "Enter the Apprise URL of the target.")
    assert [(row.name, row.url) for row in targets(sessions)] == [("Phone", f"settingsok://{TOKEN}")]
    assert element_of(user, "target-dialog").value


async def test_a_name_change_keeps_the_stored_url_without_a_new_check(user, sessions):
    # Apprise does not accept this URL, but only a new URL gets the check.
    store(sessions, target(1, "Phone", url=f"https://example.com/{TOKEN}"), target(2, "Desk"))
    await user.open("/settings")
    await open_editor(user, "target-1-edit")
    user.find(marker="target-name").type(" 2")
    user.find(marker="target-save").click()
    await user.should_see("The target Phone 2 is saved.")
    assert targets(sessions)[0].url == f"https://example.com/{TOKEN}"

    # Another page renames the target before the form opens, and the form shows the new name.
    with sessions.begin() as session:
        session.get(NotificationTarget, 2).name = "Office"
    await open_editor(user, "target-2-edit")
    assert element_of(user, "target-name").value == "Office"
    # Another page changes the URL while the form is open. A save of the name keeps that URL.
    with sessions.begin() as session:
        session.get(NotificationTarget, 2).url = "settingsfail://other"
    user.find(marker="target-name").type(" 2")
    user.find(marker="target-save").click()

    await user.should_see("The target Office 2 is saved.")
    assert [(row.name, row.url) for row in targets(sessions)][1] == ("Office 2", "settingsfail://other")


async def test_the_edit_of_a_target_that_went_tells_so(user, sessions):
    store(sessions, target(1, "Phone"), target(2, "Desk"))
    await user.open("/settings")
    with sessions.begin() as session:
        session.delete(session.get(NotificationTarget, 1))

    user.find(marker="target-1-edit").click()

    await user.should_see(GONE_TEXT)
    await eventually(lambda: not elements_of(user, "target-1"), "The card of the gone target goes")
    assert not element_of(user, "target-dialog").value
    assert not elements_of(user, "target-name")


async def test_the_cancel_of_the_editor_saves_nothing(user, sessions):
    store(sessions, target(1, "Phone"))
    await user.open("/settings")
    await open_editor(user, "target-1-edit")

    user.find(marker="target-name").type(" and more")
    user.find(marker="target-cancel").click()

    await eventually(lambda: not element_of(user, "target-dialog").value, "The closed editor")
    assert [row.name for row in targets(sessions)] == ["Phone"]


async def test_a_delete_asks_first(user, sessions):
    store(sessions, target(1, "Phone"), target(2, "Desk"))
    await user.open("/settings")

    user.find(marker="target-1-delete").click()
    await eventually(lambda: elements_of(user, "target-delete-question"), "The question")
    assert text_of(user, "target-delete-question") == 'Delete the target "Phone"?'
    user.find(marker="target-delete-cancel").click()
    await asyncio.sleep(SETTLE_SECONDS)
    assert [row.name for row in targets(sessions)] == ["Phone", "Desk"]

    user.find(marker="target-1-delete").click()
    await eventually(lambda: elements_of(user, "target-delete-confirm"), "The second question")
    user.find(marker="target-delete-confirm").click()

    await user.should_see("The target Phone is deleted.")
    assert [row.name for row in targets(sessions)] == ["Desk"]
    await eventually(lambda: not elements_of(user, "target-1"), "The card of the deleted target goes")
    assert elements_of(user, "target-2")


def choices_of(row: NotificationTarget) -> tuple:
    return row.events, row.min_level, row.enabled


async def test_the_cards_change_only_the_draft_until_a_save(user, sessions, caplog):
    caplog.set_level(logging.INFO)
    store(sessions, target(1, "Phone"), target(2, "Desk", events=["video_added", "no_such_event"]))
    await user.open("/settings")
    assert element_of(user, "target-2-event-video_added").value is True
    assert element_of(user, "target-2-event-run_failed").value is False

    user.find(marker="target-2-event-run_failed").click()
    user.find(marker="target-2-event-video_added").click()
    user.find(marker="target-2-event-scheduler_state").click()
    set_value(user, "target-2-level", Level.WARNING)
    user.find(marker="target-2-enabled").click()

    await eventually(lambda: draft_shown(user), "The buttons of the draft")
    await asyncio.sleep(SETTLE_SECONDS)
    assert choices_of(targets(sessions)[1]) == (["video_added", "no_such_event"], 0, True)

    await save(user)

    assert choices_of(targets(sessions)[1]) == (
        ["run_failed", "scheduler_state", "no_such_event"],
        Level.WARNING,
        False,
    )
    assert choices_of(targets(sessions)[0]) == (["run_failed"], 0, True)
    assert "Saved the events, the lowest level and the switch of the notification target 'Desk'" in caplog.messages
    assert not [message for message in caplog.messages if "'Phone'" in message]


async def test_a_save_of_a_target_that_went_tells_so_and_saves_the_other_changes(user, sessions):
    store(sessions, target(1, "Phone"), target(2, "Desk"))
    await user.open("/settings")
    user.find(marker="target-1-enabled").click()
    user.find(marker="rotation").click()
    with sessions.begin() as session:
        session.delete(session.get(NotificationTarget, 1))

    await save(user)

    await user.should_see(GONE_TEXT)
    await eventually(lambda: not elements_of(user, "target-1"), "The card of the gone target goes")
    assert [row.name for row in targets(sessions)] == ["Desk"]
    assert setting(sessions, ROTATION_KEY) is False


async def test_a_delete_keeps_the_draft_of_the_other_targets_and_blocks(user, sessions):
    store(sessions, target(1, "Phone"), target(2, "Desk"), target(3, "Lamp"))
    await user.open("/settings")
    user.find(marker="target-1-event-video_added").click()
    set_value(user, "target-2-level", Level.WARNING)
    user.find(marker="rotation").click()
    # Another page changes the choices that this page did not change.
    with sessions.begin() as session:
        session.get(NotificationTarget, 1).min_level = Level.FAILURE.value
        session.get(NotificationTarget, 3).enabled = False

    user.find(marker="target-2-delete").click()
    await eventually(lambda: elements_of(user, "target-delete-confirm"), "The question")
    user.find(marker="target-delete-confirm").click()

    await eventually(lambda: not elements_of(user, "target-2"), "The card of the deleted target goes")
    assert element_of(user, "target-1-event-video_added").value is True
    assert element_of(user, "target-1-level").value is Level.FAILURE
    assert element_of(user, "target-3-enabled").value is False
    assert element_of(user, "rotation").value is False
    assert draft_shown(user)

    await save(user)

    assert [choices_of(row) for row in targets(sessions)] == [
        (["run_failed", "video_added"], Level.FAILURE, True),
        (["run_failed"], 0, False),
    ]
    assert setting(sessions, ROTATION_KEY) is False
    # The draft of the deleted target went with it, so no warning came.
    assert GONE_TEXT not in user.notify.messages


async def test_the_delete_of_the_only_changed_target_ends_the_draft(user, sessions):
    store(sessions, target(1, "Phone"), target(2, "Desk"))
    await user.open("/settings")
    user.find(marker="target-1-enabled").click()
    await eventually(lambda: draft_shown(user), "The buttons of the draft")

    user.find(marker="target-1-delete").click()
    await eventually(lambda: elements_of(user, "target-delete-confirm"), "The question")
    user.find(marker="target-delete-confirm").click()

    await eventually(lambda: not draft_shown(user), "No buttons of the draft")
    assert [row.name for row in targets(sessions)] == ["Desk"]


async def test_an_added_or_changed_target_keeps_the_draft_of_the_cards(user, sessions):
    store(sessions, target(1, "Phone"))
    await user.open("/settings")
    user.find(marker="target-1-enabled").click()

    await open_editor(user, "target-1-edit")
    user.find(marker="target-name").type(" 2")
    user.find(marker="target-save").click()
    await eventually(lambda: text_of(user, "target-1-name") == "Phone 2", "The new name in the card")
    await open_editor(user, "target-add")
    user.find(marker="target-name").type("Desk")
    user.find(marker="target-url").type("settingsok://desk")
    user.find(marker="target-save").click()
    await eventually(lambda: elements_of(user, "target-2"), "The card of the new target")

    assert element_of(user, "target-1-enabled").value is False
    assert element_of(user, "target-2-enabled").value is True
    assert draft_shown(user)
    await save(user)
    assert [(row.name, row.enabled) for row in targets(sessions)] == [("Phone 2", False), ("Desk", True)]


async def test_a_test_notification_goes_to_the_target(user, sessions, sent):
    store(sessions, target(1, "Phone", url="settingsok://phonehost", enabled=False, events=[]))
    await user.open("/settings")

    user.find(marker="target-1-test").click()

    await user.should_see("The test notification went to Phone.")
    assert sent == [{"host": "phonehost", "title": "YouTube AutoManager: test notification"}]


async def test_a_failed_test_notification_says_so(user, sessions, sent):
    store(sessions, target(1, "Phone", url=f"settingsfail://{TOKEN}"))
    await user.open("/settings")

    user.find(marker="target-1-test").click()

    await user.should_see("The test notification to Phone failed. The log of the service names the cause.")
    assert len(sent) == 1


async def test_the_test_button_shows_a_spinner_while_the_send_runs(user, sessions, sent):
    store(sessions, target(1, "Slow", url="settingsslow://host"), target(2, "Fast", url="settingsok://fast"))
    await user.open("/settings")

    user.find(marker="target-1-test").click()
    await eventually(lambda: "loading" in element_of(user, "target-1-test").props, "The spinner")
    assert not element_of(user, "target-1-test").enabled
    # The other target can get its test at the same time.
    user.find(marker="target-2-test").click()
    await user.should_see("The test notification went to Fast.")
    assert "loading" in element_of(user, "target-1-test").props

    SLOW_SEND.set()

    await user.should_see("The test notification went to Slow.")
    assert "loading" not in element_of(user, "target-1-test").props
    assert element_of(user, "target-1-test").enabled


async def test_the_test_of_a_target_that_went_tells_so(user, sessions, sent):
    store(sessions, target(1, "Phone"))
    await user.open("/settings")
    with sessions.begin() as session:
        session.delete(session.get(NotificationTarget, 1))

    user.find(marker="target-1-test").click()

    await user.should_see("The target does not exist any more.")
    assert sent == []


async def test_only_the_edit_form_shows_a_target_url_and_no_log_line_holds_one(user, sessions, sent, caplog):
    caplog.set_level(logging.DEBUG)
    store(sessions, target(1, "Fails", url=f"settingsfail://{TOKEN}"), target(2, "Bad", url=f"https://{TOKEN}.example"))
    await user.open("/settings")
    assert TOKEN not in page_dump(user)

    user.find(marker="target-1-test").click()
    await user.should_see("The test notification to Fails failed. The log of the service names the cause.")
    user.find(marker="target-2-test").click()
    await user.should_see("The test notification to Bad failed. The log of the service names the cause.")
    await open_editor(user, "target-1-edit")
    assert TOKEN in page_dump(user)
    user.find(marker="target-url").clear()
    user.find(marker="target-url").type(f"settingsok://{TOKEN}/new")
    user.find(marker="target-save").click()
    await user.should_see("The target Fails is saved.")

    # The form goes after the save, and the page holds no URL any more.
    await eventually(lambda: not elements_of(user, "target-url"), "The end of the form")
    assert TOKEN not in page_dump(user)
    assert [record.getMessage() for record in caplog.records if TOKEN in record.getMessage()] == []
    assert text_of(user, "target-2-url") == "https://…"


# The draft of the page.


async def test_a_change_of_each_setting_shows_save_and_discard_until_the_user_puts_it_back(user, sessions):
    store(sessions, target(1, "Phone"))
    await user.open("/settings")
    assert not draft_shown(user)
    assert texts_of(user, "settings-save") == ["Save"]
    assert texts_of(user, "settings-discard") == ["Discard"]
    changes = [
        ("rotation", False, True),
        ("mode", ProcessingMode.MARK_DATE, ProcessingMode.STOP_AT_SEEN),
        ("ytdlp-enabled", False, True),
        ("ytdlp-channel", "nightly", "stable"),
        ("ytdlp-time", "05:30", "04:00"),
        ("target-1-event-video_added", True, False),
        ("target-1-level", Level.WARNING, Level.INFO),
        ("target-1-enabled", False, True),
    ]

    for marker, changed, stored in changes:
        set_value(user, marker, changed)
        await eventually(lambda: draft_shown(user), f"The buttons of the draft after a change of {marker}")
        await user.should_see(marker="settings-save")
        set_value(user, marker, stored)
        await eventually(lambda: not draft_shown(user), f"No buttons of the draft after {marker} went back")

    assert [setting(sessions, key) for key in (ROTATION_KEY, "processing_mode", "ytdlp_update")] == [None] * 3
    assert choices_of(targets(sessions)[0]) == (["run_failed"], 0, True)


async def test_discard_puts_each_control_back_and_writes_nothing(user, sessions):
    store(sessions, target(1, "Phone"))
    await user.open("/settings")
    user.find(marker="rotation").click()
    set_value(user, "mode", ProcessingMode.NEWEST)
    set_value(user, "mode-count", 45.0)
    set_value(user, "ytdlp-enabled", False)
    set_value(user, "ytdlp-channel", "nightly")
    set_value(user, "ytdlp-time", "noon")
    user.find(marker="target-1-event-video_added").click()
    set_value(user, "target-1-level", Level.FAILURE)
    user.find(marker="target-1-enabled").click()
    user.find(marker="settings-save").click()
    await user.should_see(marker="ytdlp-error")

    user.find(marker="settings-discard").click()

    await eventually(lambda: not draft_shown(user), "No buttons of the draft")
    assert element_of(user, "rotation").value is True
    assert element_of(user, "mode").value is ProcessingMode.STOP_AT_SEEN
    assert element_of(user, "mode-count").value == 30
    assert (element_of(user, "ytdlp-enabled").value, element_of(user, "ytdlp-channel").value) == (True, "stable")
    assert element_of(user, "ytdlp-time").value == "04:00"
    await user.should_not_see(marker="ytdlp-error")
    # The cards show the stored choices after their rebuild.
    await eventually(lambda: element_of(user, "target-1-event-video_added").value is False, "The stored event")
    assert element_of(user, "target-1-level").value is Level.INFO
    assert element_of(user, "target-1-enabled").value is True
    assert [setting(sessions, key) for key in (ROTATION_KEY, "processing_mode", "ytdlp_update")] == [None] * 3
    assert choices_of(targets(sessions)[0]) == (["run_failed"], 0, True)


async def test_a_save_writes_only_the_changed_settings(user, sessions):
    store(sessions, target(1, "Phone"), target(2, "Desk"))
    await user.open("/settings")
    user.find(marker="rotation").click()
    set_value(user, "target-1-level", Level.WARNING)
    # Another page changes the mode, the yt-dlp settings and the other target after this page read them.
    store(
        sessions,
        Setting(key="processing_mode", value={"mode": "mark_date"}),
        Setting(key="ytdlp_update", value={"enabled": False, "channel": "nightly", "time": "06:15"}),
    )
    with sessions.begin() as session:
        session.get(NotificationTarget, 2).events = ["video_added"]
        session.get(NotificationTarget, 1).enabled = False

    await save(user)

    assert setting(sessions, ROTATION_KEY) is False
    assert setting(sessions, "processing_mode") == {"mode": "mark_date"}
    assert setting(sessions, "ytdlp_update") == {"enabled": False, "channel": "nightly", "time": "06:15"}
    assert [choices_of(row) for row in targets(sessions)] == [
        (["run_failed"], Level.WARNING, False),
        (["video_added"], 0, True),
    ]
    assert not element_of(user, "mode-confirm-dialog").value
    # The block reads the targets again, so it shows the choices of the other page too.
    await eventually(lambda: element_of(user, "target-1-enabled").value is False, "The switch of the other page")
    assert element_of(user, "target-2-event-video_added").value is True
    assert element_of(user, "target-1-level").value is Level.WARNING
    assert not draft_shown(user)


# The translation.


async def test_each_text_of_the_settings_page_goes_through_the_translation(
    nicegui_globals, settings, sessions, fake_starter, monkeypatch
):
    class Marking:
        def gettext(self, message):
            return f"<<{message}>>"

    monkeypatch.setattr(i18n, "_translations", Marking())
    store(
        sessions,
        Account(cookies=b"encrypted", last_checked_at=CHECKED_AT),
        Schedule(enabled=True, kind=ScheduleKind.CRON, timezone="Europe/Kyiv"),
        target(1, "Phone"),
    )
    data = {"Phone", "settingsok://…", KEY_COMMAND, COMPOSE_KEY_COMMAND, importlib.metadata.version("yt-dlp")}
    texts = []

    async with running_service(settings.model_copy(update={"cookies_key": None}), fake_starter) as user:
        await user.open("/settings")
        await open_editor(user, "target-1-edit")
        for element in user.client.elements.values():
            if isinstance(element, TextElement) and element.text:
                texts.append(element.text)
            if isinstance(element, LabelElement) and element.props.get("label"):
                texts.append(element.props["label"])
            if isinstance(element, (ui.select, ui.radio)):
                texts.extend(element.options.values())

    assert "<<Settings>>" in texts
    assert "<<Info>>" in texts
    assert "<<Stop at a seen video (default)>>" in texts
    assert "<<Nightly>>" in texts
    assert "<<Time of the update>>" in texts
    untranslated = [
        text
        for text in texts
        if not (text.startswith("<<") and text.endswith(">>")) and text not in data and not TIME.fullmatch(text)
    ]
    assert untranslated == []


def test_each_text_of_an_upload_that_is_not_stored_goes_through_the_translation(monkeypatch):
    class Marking:
        def gettext(self, message):
            return f"<<{message}>>"

    monkeypatch.setattr(i18n, "_translations", Marking())
    errors = [ChildBusy("x"), ChildLoggedOut("x"), ChildUnavailable("x"), ChildYouTubeError("x"), ChildFailed("x")]

    texts = [upload_error_text(error, had_cookies=had_cookies) for error in errors for had_cookies in (True, False)]
    texts.append(expired_text(CHECKED_AT, ZoneInfo("UTC")))

    assert [text for text in texts if not (text.startswith("<<") and text.endswith(">>"))] == []


# The process of the service.


def test_the_settings_page_and_an_upload_leave_yt_dlp_out_of_the_process_of_the_service():
    """The check of an upload runs in a child process, so a yt-dlp update reaches the next upload."""
    code = (
        "import io, pathlib, sys, tempfile\n"
        "from fastapi.testclient import TestClient\n"
        "from youtube_automanager.cookie_upload_check import check_upload_in_child\n"
        "from youtube_automanager.settings import Settings\n"
        "from youtube_automanager.web.app import create_app\n"
        "from youtube_automanager.web.ui import mount_ui\n"
        "\n"
        "inputs = []\n"
        "\n"
        "class Child:\n"
        "    returncode = 0\n"
        "    def __init__(self, input_text):\n"
        "        inputs.append(input_text)\n"
        "        self.stderr = io.StringIO('{\"stored\": true}\\n')\n"
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
        "app = create_app(settings, start=lambda command, env, input_text: Child(input_text))\n"
        "mount_ui(app)\n"
        "with TestClient(app) as client:\n"
        "    page = client.get('/settings')\n"
        "    check_upload_in_child(client.app.state.services.runner, '# Netscape HTTP Cookie File\\n')\n"
        "assert page.status_code == 200, page.text\n"
        "assert inputs == ['# Netscape HTTP Cookie File\\n'], inputs\n"
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
