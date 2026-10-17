import importlib.metadata
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from nicegui import ui
from nicegui.elements.mixins.text_element import TextElement
from ui_support import element_of, eventually, running_service, store, text_of

from youtube_automanager import i18n
from youtube_automanager.db.models import (
    Account,
    Run,
    RunStatus,
    RunTrigger,
    Schedule,
    ScheduleKind,
    SessionState,
    Setting,
)
from youtube_automanager.i18n import N_
from youtube_automanager.web.ui import dashboard, layout
from youtube_automanager.web.ui.theme import THEME_KEY, Theme, first_paint_style, read_theme

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]

KYIV = ZoneInfo("Europe/Kyiv")
TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+")


def next_kyiv_evening() -> str:
    now = datetime.now(KYIV)
    evening = now.replace(hour=18, minute=0, second=0, microsecond=0)
    if evening <= now:
        evening = (evening + timedelta(days=1)).replace(hour=18)
    return evening.strftime("%Y-%m-%d %H:%M %Z")


async def test_existing_routes_still_answer_next_to_the_pages(user):
    health = await user.http_client.get("/health")
    status = await user.http_client.get("/api/status")
    runs = await user.http_client.get("/api/runs")
    page = await user.http_client.get("/")

    assert (health.status_code, health.json()) == (200, {"status": "ok", "detail": None})
    assert (status.status_code, status.json()["scheduler_state"]) == (200, "running")
    assert (runs.status_code, runs.json()) == (200, [])
    assert (page.status_code, page.headers["X-Nicegui-Content"]) == (200, "page")


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_an_unknown_api_path_gets_a_json_404_next_to_the_pages(user, method):
    answer = await user.http_client.request(method, "/api/no-such-path")

    assert (answer.status_code, answer.json()) == (404, {"detail": "Not Found"})


async def test_dashboard_of_a_new_service(user):
    await user.open("/")

    await user.should_see("No cookies are stored yet.")
    assert text_of(user, "scheduler-state") == "Running"
    assert text_of(user, "next-run") == next_kyiv_evening()
    assert text_of(user, "timezone") == "Europe/Kyiv"
    await user.should_see("No run yet.")


async def test_dashboard_shows_the_session_and_the_failed_last_run(user, sessions):
    store(
        sessions,
        Account(
            cookies=b"encrypted",
            session_state=SessionState.EXPIRING,
            last_checked_at=datetime(2026, 10, 1, 9, 30, tzinfo=UTC),
            earliest_cookie_expiry=datetime(2026, 12, 31, 22, 0, tzinfo=UTC),
        ),
        Run(status=RunStatus.SUCCEEDED, trigger=RunTrigger.SCHEDULE, started_at=datetime(2026, 10, 1, tzinfo=UTC)),
        Run(
            status=RunStatus.FAILED,
            trigger=RunTrigger.MANUAL,
            started_at=datetime(2026, 10, 2, 15, 5, tzinfo=UTC),
            added_count=3,
            seen_count=40,
            error_text="The sync process exited with code 1.",
        ),
    )

    await user.open("/")

    assert text_of(user, "session-state") == "Expiring"
    # Kyiv is at UTC+3 in summer and at UTC+2 in winter.
    assert text_of(user, "session-checked") == "2026-10-01 12:30 EEST"
    assert text_of(user, "session-expiry") == "2027-01-01 00:00 EET"
    assert text_of(user, "run-started") == "2026-10-02 18:05 EEST"
    assert text_of(user, "run-status") == "Failed"
    assert text_of(user, "run-trigger") == "Manual"
    assert (text_of(user, "run-added"), text_of(user, "run-seen")) == ("3", "40")
    assert text_of(user, "run-error") == "The sync process exited with code 1."


async def test_dashboard_shows_an_unchecked_session_and_an_active_run(user, sessions):
    store(
        sessions,
        Account(cookies=b"encrypted"),
        Run(status=RunStatus.RUNNING, trigger=RunTrigger.SCHEDULE, started_at=datetime(2026, 10, 2, tzinfo=UTC)),
    )

    await user.open("/")

    assert text_of(user, "session-state") == "Unknown"
    assert text_of(user, "session-checked") == "Never"
    assert text_of(user, "session-expiry") == "No expiry date"
    assert (text_of(user, "run-status"), text_of(user, "run-trigger")) == ("In progress", "Schedule")
    await user.should_not_see(marker="run-error")


async def test_account_without_cookies_counts_as_no_stored_cookies(user, sessions):
    store(sessions, Account(cookies=None, session_state=SessionState.DEAD))

    await user.open("/")

    await user.should_see("No cookies are stored yet.")
    await user.should_not_see(marker="session-state")


async def test_paused_schedule_has_no_next_run_and_shows_its_own_time_zone(
    nicegui_globals, settings, sessions, fake_starter
):
    store(
        sessions,
        Schedule(enabled=False, kind=ScheduleKind.INTERVAL, interval_s=1800, timezone="America/New_York"),
        Run(status=RunStatus.SUCCEEDED, trigger=RunTrigger.SCHEDULE, started_at=datetime(2026, 1, 5, tzinfo=UTC)),
    )

    async with running_service(settings, fake_starter) as user:
        await user.open("/")

        assert text_of(user, "scheduler-state") == "Paused"
        assert text_of(user, "next-run") == "No run is planned"
        assert text_of(user, "timezone") == "America/New_York"
        assert text_of(user, "run-started") == "2026-01-04 19:00 EST"


async def test_dashboard_shows_the_ytdlp_of_the_image_without_a_release_in_the_data_folder(user):
    await user.open("/")

    assert text_of(user, "ytdlp-version") == importlib.metadata.version("yt-dlp")
    assert text_of(user, "ytdlp-source") == "Image of the service"


async def test_dashboard_shows_the_ytdlp_of_the_data_folder_after_an_update(user, settings, ytdlp_install, monkeypatch):
    monkeypatch.setattr(dashboard, "REFRESH_SECONDS", 0.1)
    await user.open("/")

    ytdlp_install.write_release(settings.ytdlp_dir, "2099.1.2")

    await user.should_see("Data folder", retries=50)
    assert text_of(user, "ytdlp-version") == "2099.1.2"


async def test_dashboard_refreshes_itself(user, sessions, monkeypatch):
    monkeypatch.setattr(dashboard, "REFRESH_SECONDS", 0.1)
    await user.open("/")
    await user.should_see("No run yet.")

    store(sessions, Run(status=RunStatus.SUCCEEDED, trigger=RunTrigger.SCHEDULE, added_count=5))

    await user.should_see(marker="run-status", retries=50)
    assert (text_of(user, "run-status"), text_of(user, "run-added")) == ("Succeeded", "5")


async def test_shell_shows_the_service_name_and_each_registered_page(
    nicegui_globals, settings, fake_starter, monkeypatch
):
    monkeypatch.setattr(layout, "_PAGES", dict(layout._PAGES))

    @layout.register_page("/later", label=N_("Later page"), icon="list")
    async def later_page(services):
        ui.label(f"time zone {services.settings.tz}").mark("later-content")

    async with running_service(settings, fake_starter) as user:
        await user.open("/")
        await user.should_see("YouTube AutoManager")
        with user.scope(marker="navigation"):
            items = sorted(user.find(kind=ui.item_label).elements, key=lambda element: element.id)
        assert [item.text for item in items] == [
            "Dashboard",
            "Rules",
            "Subscriptions",
            "Scheduler",
            "History",
            "Settings",
            "Later page",
        ]

        user.find(marker="nav:/later").click()

        await user.should_see(marker="later-content")
        assert text_of(user, "later-content") == "time zone Europe/Kyiv"


async def test_each_text_of_the_dashboard_goes_through_the_translation(
    nicegui_globals, settings, sessions, fake_starter, monkeypatch
):
    class Marking:
        def gettext(self, message):
            return f"<<{message}>>"

    monkeypatch.setattr(i18n, "_translations", Marking())
    store(
        sessions,
        Account(cookies=b"encrypted", last_checked_at=datetime(2026, 10, 1, tzinfo=UTC)),
        Run(status=RunStatus.FAILED, trigger=RunTrigger.MANUAL, added_count=2, seen_count=7, error_text="boom"),
    )
    data = {"Europe/Kyiv", "2", "7", "boom", importlib.metadata.version("yt-dlp")}

    async with running_service(settings, fake_starter) as user:
        await user.open("/")
        texts = [element.text for element in user.client.elements.values() if isinstance(element, TextElement)]

    texts = [text for text in texts if text]
    assert "<<Cookie session>>" in texts
    untranslated = [
        text
        for text in texts
        if not (text.startswith("<<") and text.endswith(">>")) and text not in data and not TIME.fullmatch(text)
    ]
    assert untranslated == []


# The color theme.


def theme_of(user):
    return element_of(user, "dark-mode").value, element_of(user, "theme").props["icon"]


async def test_the_theme_follows_the_system_of_the_device_by_default(user):
    await user.open("/")

    assert theme_of(user) == (None, "brightness_auto")


async def test_the_theme_button_goes_through_dark_light_and_auto_and_stores_each_choice(user, sessions):
    await user.open("/")

    for value, icon, stored in [
        (True, "dark_mode", "dark"),
        (False, "light_mode", "light"),
        (None, "brightness_auto", "auto"),
    ]:
        user.find(marker="theme").click()
        await eventually(lambda stored=stored: read_theme(sessions) == Theme(stored), f"The stored theme {stored}")
        assert theme_of(user) == (value, icon)


def test_the_dark_and_the_system_theme_paint_the_page_dark_before_the_mount():
    assert first_paint_style(Theme.DARK) == "<style>body:not([class]) { background-color: #121212; }</style>"
    assert first_paint_style(Theme.AUTO) == (
        "<style>@media (prefers-color-scheme: dark) { body:not([class]) { background-color: #121212; } }</style>"
    )
    assert first_paint_style(Theme.LIGHT) == ""


@pytest.mark.parametrize("stored", ["dark", "auto", "light"])
@pytest.mark.parametrize("path", ["/", "/rules", "/settings"])
async def test_the_first_html_of_each_page_holds_the_background_of_the_stored_theme(user, sessions, stored, path):
    store(sessions, Setting(key=THEME_KEY, value=stored))

    response = await user.http_client.get(path)

    html = response.text
    style = first_paint_style(Theme(stored))
    head = html[: html.index("<body")]
    if style:
        assert style in head
    else:
        assert "body:not([class])" not in html


@pytest.mark.parametrize(("stored", "value"), [("dark", True), ("light", False), ("auto", None), ("purple", None)])
async def test_each_page_shows_the_stored_theme(user, sessions, stored, value):
    store(sessions, Setting(key=THEME_KEY, value=stored))

    await user.open("/settings")

    assert element_of(user, "dark-mode").value is value
