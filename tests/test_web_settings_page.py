"""Tests of the settings page. A fake child stands in for each session check, and Apprise plugins of this module stand in
for each notification service, so no test reads YouTube or sends a real notification. Each cookie value is fake."""

import asyncio
import datetime
import importlib.metadata
import logging
import re
import subprocess
import sys
import threading

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
from youtube_automanager.db.models import Account, NotificationTarget, Schedule, ScheduleKind, SessionState, Setting
from youtube_automanager.engine.modes import ProcessingMode
from youtube_automanager.notify import DEFAULT_EVENTS, Event, Level
from youtube_automanager.rotation import ROTATION_KEY
from youtube_automanager.runner import SOURCE_ROOT, UNAVAILABLE_EXIT_CODE, sync_lock_held
from youtube_automanager.web.ui.cookie_upload import COMPOSE_KEY_COMMAND, KEY_COMMAND, MAX_FILE_BYTES
from youtube_automanager.youtube.cookie_text import LOGIN_COOKIES
from youtube_automanager.youtube.session import CookieCipher, generate_key, load_cookies

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


def line(name: str, *, domain: str = ".youtube.com") -> str:
    flag = "TRUE" if domain.startswith(".") else "FALSE"
    return f"{domain}\t{flag}\t/\tTRUE\t{EXPIRES}\t{name}\t{SECRET}-{name}"


def cookie_text(names=LOGIN_COOKIES, *extra: str) -> str:
    return "\n".join(["# Netscape HTTP Cookie File", *(line(name) for name in sorted(names)), *extra]) + "\n"


# The number of the line of the cookie SID in the text of ``cookie_text()``. The header is line 1.
SID_LINE = sorted(LOGIN_COOKIES).index("SID") + 2


class CheckingStarter(FakeStarter):
    """A fake start of the child processes. A session check writes ``state`` into the account, as the child does."""

    def __init__(self, sessions):
        super().__init__()
        self.sessions = sessions
        self.state = SessionState.OK

    def __call__(self, command, env):
        if command[3:] == ["check-session"] and self.state is not None:
            with self.sessions.begin() as session:
                account = session.scalars(select(Account)).first()
                if account is not None:
                    account.session_state = self.state
                    account.last_checked_at = CHECKED_AT
        return super().__call__(command, env)


@pytest.fixture
def settings(settings):
    """The settings of the tests, with a cookie key."""
    return settings.model_copy(update={"cookies_key": SecretStr(KEY)})


@pytest.fixture
def fake_starter(sessions):
    return CheckingStarter(sessions)


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


async def test_an_uploaded_cookie_file_is_stored_and_the_session_is_checked_at_once(user, sessions, fake_starter):
    text = cookie_text(LOGIN_COOKIES, line("GPS"), line("NID", domain=".google.com"))
    await user.open("/settings")

    await upload(user, ("﻿" + text.replace("\n", "\r\n")).encode())

    await eventually(lambda: elements_of(user, "session-state"), "The state of the stored session")
    await result_is(user, "YouTube accepts the cookies.")
    await user.should_see("The cookies are stored.")
    assert stored_text(sessions) == text
    assert commands(fake_starter) == [["check-session"]]
    assert text_of(user, "session-state") == "OK"
    assert text_of(user, "session-checked") == "2026-10-12 18:30 EEST"
    assert text_of(user, "session-expiry") == "2030-01-01 02:00 EET"
    assert element_of(user, "check-session").enabled


async def test_a_pasted_cookie_text_is_stored_checked_and_leaves_the_field(user, sessions, fake_starter):
    await user.open("/settings")

    await paste(user, cookie_text())

    await result_is(user, "YouTube accepts the cookies.")
    assert stored_text(sessions) == cookie_text()
    assert element_of(user, "cookie-text").value == ""
    assert commands(fake_starter) == [["check-session"]]


async def test_a_new_upload_replaces_the_stored_cookies_and_their_state(user, sessions, fake_starter):
    fake_starter.state = SessionState.DEAD
    await user.open("/settings")
    await paste(user, cookie_text())
    await result_is(user, "YouTube does not accept the cookies. Export new cookies and upload them.")
    assert text_of(user, "session-state") == "Dead"

    fake_starter.state = SessionState.EXPIRING
    await paste(user, cookie_text(LOGIN_COOKIES, line("GPS")))

    await result_is(user, "YouTube accepts the cookies, but they expire soon. Export new cookies and upload them.")
    assert stored_text(sessions) == cookie_text(LOGIN_COOKIES, line("GPS"))
    assert text_of(user, "session-state") == "Expiring"


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


async def test_no_page_text_and_no_log_line_holds_a_cookie_value(user, sessions, caplog):
    caplog.set_level(logging.DEBUG)
    await user.open("/settings")

    await upload(user, cookie_text().encode())
    await result_is(user, "YouTube accepts the cookies.")
    await paste(user, cookie_text(LOGIN_COOKIES - {"SID"}))
    await eventually(lambda: "SID" in text_of(user, "cookie-result"), "The text of the missing cookie")

    assert SECRET not in page_dump(user)
    assert [record.getMessage() for record in caplog.records if SECRET in record.getMessage()] == []
    assert "Stored new YouTube cookies from the settings page" in caplog.messages


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


async def test_the_rotation_is_off_by_default_and_its_switch_stores_json_booleans(user, sessions):
    await user.open("/settings")
    assert element_of(user, "rotation").value is False
    assert "about each 10 minutes" in text_of(user, "rotation-help")

    user.find(marker="rotation").click()
    await user.should_see("The cookie rotation is on.")
    assert setting(sessions, ROTATION_KEY) is True

    user.find(marker="rotation").click()
    await eventually(lambda: setting(sessions, ROTATION_KEY) is False, "The rotation off")
    await user.should_see("The cookie rotation is off.")


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
        await user.should_see("The cookie rotation is on.")

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


async def test_a_change_of_the_mode_asks_first_and_a_cancel_stores_nothing(user, sessions):
    await user.open("/settings")
    set_value(user, "mode", ProcessingMode.MARK_DATE)

    user.find(marker="mode-save").click()

    await eventually(lambda: element_of(user, "mode-confirm-dialog").value, "The open confirmation")
    assert "the videos that came since the last run are not checked" in text_of(user, "mode-confirm-text")
    user.find(marker="mode-cancel").click()
    await eventually(lambda: not element_of(user, "mode-confirm-dialog").value, "The closed confirmation")
    await asyncio.sleep(SETTLE_SECONDS)
    assert setting(sessions, "processing_mode") is None


async def test_a_confirmed_change_stores_the_mode(user, sessions):
    await user.open("/settings")
    set_value(user, "mode", ProcessingMode.NEWEST)
    set_value(user, "mode-count", 45.0)

    user.find(marker="mode-save").click()
    await eventually(lambda: element_of(user, "mode-confirm-dialog").value, "The open confirmation")
    user.find(marker="mode-confirm").click()

    await user.should_see("The processing mode is saved. The next run uses it.")
    assert setting(sessions, "processing_mode") == {"mode": "newest", "count": 45}
    assert isinstance(setting(sessions, "processing_mode")["count"], int)
    assert not element_of(user, "mode-confirm-dialog").value

    set_value(user, "mode", ProcessingMode.MARK_DATE)
    user.find(marker="mode-save").click()
    await eventually(lambda: element_of(user, "mode-confirm-dialog").value, "The second confirmation")
    user.find(marker="mode-confirm").click()
    await eventually(lambda: setting(sessions, "processing_mode") == {"mode": "mark_date"}, "The second save")


async def test_a_save_of_the_same_mode_asks_nothing_and_writes_nothing(user, sessions):
    await user.open("/settings")

    user.find(marker="mode-save").click()

    await user.should_see("The processing mode did not change.")
    assert not element_of(user, "mode-confirm-dialog").value
    assert setting(sessions, "processing_mode") is None


async def test_the_stored_mode_shows_on_the_page(nicegui_globals, settings, sessions, fake_starter):
    store(sessions, Setting(key="processing_mode", value={"mode": "newest", "count": 7}))

    async with running_service(settings, fake_starter) as user:
        await user.open("/settings")

        assert element_of(user, "mode").value is ProcessingMode.NEWEST
        assert element_of(user, "mode-count").value == 7
        set_value(user, "mode-count", 7.0)
        user.find(marker="mode-save").click()
        await user.should_see("The processing mode did not change.")


@pytest.mark.parametrize("count", [None, 0, -3, 2.5])
async def test_a_wrong_count_shows_the_error_and_stores_nothing(user, sessions, count):
    await user.open("/settings")
    set_value(user, "mode", ProcessingMode.NEWEST)
    set_value(user, "mode-count", count)

    user.find(marker="mode-save").click()

    await eventually(lambda: text_of(user, "mode-error") == "Enter a whole number of videos, 1 or more.", "The error")
    assert not element_of(user, "mode-confirm-dialog").value
    assert setting(sessions, "processing_mode") is None

    set_value(user, "mode-count", 1)
    user.find(marker="mode-save").click()
    await eventually(lambda: element_of(user, "mode-confirm-dialog").value, "The confirmation")
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
        user.find(marker="mode-save").click()
        await eventually(lambda: element_of(user, "mode-confirm-dialog").value, "The confirmation")
        user.find(marker="mode-confirm").click()
        await user.should_see("The processing mode is saved. The next run uses it.")
        await user.should_not_see(marker="mode-invalid")

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
    await user.should_not_see(marker="targets-table")


async def test_a_new_target_gets_the_default_events_and_the_table_shows_only_its_scheme(user, sessions):
    await user.open("/settings")
    await open_editor(user, "target-add")
    assert text_of(user, "target-editor-title") == "New target"
    assert element_of(user, "target-url").props["type"] == "password"

    user.find(marker="target-name").type("Phone")
    user.find(marker="target-url").type(f"settingsok://{TOKEN}/chat")
    user.find(marker="target-save").click()

    await user.should_see("The target Phone is saved.")
    (row,) = targets(sessions)
    assert (row.name, row.url, row.min_level, row.enabled) == ("Phone", f"settingsok://{TOKEN}/chat", 0, True)
    assert set(row.events) == {event.value for event in DEFAULT_EVENTS}
    assert not element_of(user, "target-dialog").value
    await eventually(lambda: elements_of(user, f"target-{row.id}"), "The row of the new target")
    assert text_of(user, f"target-{row.id}-name") == "Phone"
    assert text_of(user, f"target-{row.id}-url") == "settingsok://…"
    for event in Event:
        assert element_of(user, f"target-{row.id}-event-{event.value}").value is (event in DEFAULT_EVENTS), event
    assert element_of(user, f"target-{row.id}-level").value is Level.INFO
    assert element_of(user, f"target-{row.id}-enabled").value is True
    assert TOKEN not in page_dump(user)


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


async def test_a_change_keeps_the_stored_url_while_the_field_stays_empty(user, sessions):
    store(sessions, target(1, "Phone"))
    await user.open("/settings")
    await open_editor(user, "target-1-edit")
    assert text_of(user, "target-editor-title") == "Change the target"
    assert element_of(user, "target-name").value == "Phone"
    assert element_of(user, "target-url").value == ""
    assert text_of(user, "target-url-hint") == (
        "The stored URL stays when this field is empty. It starts with settingsok://…. A URL in this field replaces it."
    )

    user.find(marker="target-name").clear()
    user.find(marker="target-name").type("Desk")
    user.find(marker="target-save").click()

    await user.should_see("The target Desk is saved.")
    assert [(row.name, row.url) for row in targets(sessions)] == [("Desk", f"settingsok://{TOKEN}")]
    await eventually(lambda: text_of(user, "target-1-name") == "Desk", "The new name in the table")

    await open_editor(user, "target-1-edit")
    user.find(marker="target-url").type("settingsfail://other")
    user.find(marker="target-save").click()
    await eventually(lambda: targets(sessions)[0].url == "settingsfail://other", "The new URL")
    await eventually(lambda: text_of(user, "target-1-url") == "settingsfail://…", "The new scheme in the table")


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
    await eventually(lambda: not elements_of(user, "target-1"), "The row of the deleted target goes")
    assert elements_of(user, "target-2")


async def test_the_table_stores_each_change_at_once(user, sessions):
    store(sessions, target(1, "Phone"), target(2, "Desk", events=["video_added", "no_such_event"]))
    await user.open("/settings")
    assert element_of(user, "target-2-event-video_added").value is True
    assert element_of(user, "target-2-event-run_failed").value is False

    user.find(marker="target-2-event-run_failed").click()
    user.find(marker="target-2-event-video_added").click()
    user.find(marker="target-2-event-scheduler_state").click()
    set_value(user, "target-2-level", Level.WARNING)
    user.find(marker="target-2-enabled").click()

    def desk():
        return targets(sessions)[1]

    await eventually(lambda: not desk().enabled, "The switch off")
    assert desk().events == ["run_failed", "scheduler_state", "no_such_event"]
    assert desk().min_level == Level.WARNING
    assert (targets(sessions)[0].events, targets(sessions)[0].min_level) == (["run_failed"], 0)


async def test_a_change_of_a_target_that_went_tells_so_and_reads_the_table_again(user, sessions):
    store(sessions, target(1, "Phone"), target(2, "Desk"))
    await user.open("/settings")
    with sessions.begin() as session:
        session.delete(session.get(NotificationTarget, 1))

    user.find(marker="target-1-enabled").click()

    await user.should_see("The target does not exist any more.")
    await eventually(lambda: not elements_of(user, "target-1"), "The row of the gone target goes")
    assert [row.name for row in targets(sessions)] == ["Desk"]


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


async def test_no_page_text_and_no_log_line_holds_a_target_url(user, sessions, sent, caplog):
    caplog.set_level(logging.DEBUG)
    store(sessions, target(1, "Fails", url=f"settingsfail://{TOKEN}"), target(2, "Bad", url=f"https://{TOKEN}.example"))
    await user.open("/settings")

    user.find(marker="target-1-test").click()
    await user.should_see("The test notification to Fails failed. The log of the service names the cause.")
    user.find(marker="target-2-test").click()
    await user.should_see("The test notification to Bad failed. The log of the service names the cause.")
    await open_editor(user, "target-1-edit")
    user.find(marker="target-url").type(f"settingsok://{TOKEN}/new")
    user.find(marker="target-save").click()
    await user.should_see("The target Fails is saved.")

    assert TOKEN not in page_dump(user)
    assert [record.getMessage() for record in caplog.records if TOKEN in record.getMessage()] == []
    assert text_of(user, "target-2-url") == "https://…"


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


# The process of the service.


def test_the_settings_page_leaves_yt_dlp_out_of_the_process_of_the_service():
    code = (
        "import pathlib, sys, tempfile\n"
        "from fastapi.testclient import TestClient\n"
        "from youtube_automanager.settings import Settings\n"
        "from youtube_automanager.web.app import create_app\n"
        "from youtube_automanager.web.ui import mount_ui\n"
        "home = pathlib.Path(tempfile.mkdtemp())\n"
        "settings = Settings(home=home, database_url=f'sqlite:///{home / \"service.sqlite\"}', tz='UTC')\n"
        "app = create_app(settings)\n"
        "mount_ui(app)\n"
        "with TestClient(app) as client:\n"
        "    page = client.get('/settings')\n"
        "assert page.status_code == 200, page.text\n"
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
