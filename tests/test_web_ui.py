import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from nicegui import ui
from nicegui.elements.mixins.text_element import TextElement
from nicegui.testing import User

from youtube_automanager import i18n
from youtube_automanager.db import make_engine, make_session_factory, upgrade_to_head
from youtube_automanager.db.models import Account, Run, RunStatus, RunTrigger, Schedule, ScheduleKind, SessionState
from youtube_automanager.i18n import N_
from youtube_automanager.settings import Settings
from youtube_automanager.web.app import create_app
from youtube_automanager.web.ui import dashboard, layout, mount_ui

pytestmark = pytest.mark.anyio

KYIV = ZoneInfo("Europe/Kyiv")
TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def logging_setups(monkeypatch):
    """Records the logging setup of the lifespan instead of a change of the handlers that pytest captures."""
    calls = []
    monkeypatch.setattr("youtube_automanager.web.app.setup_logging", calls.append)
    return calls


@pytest.fixture(autouse=True)
def no_logged_errors(caplog):
    """Fails a test when the service logged an error. NiceGUI logs an error of a page or a timer instead of a raise."""
    yield
    errors = [record.getMessage() for record in caplog.get_records("call") if record.levelno >= logging.ERROR]
    assert not errors, f"The service logged errors: {errors}"


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
def sessions(settings):
    """Sessions of the database of ``settings``, with the newest schema."""
    settings.home.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(settings.database_url)
    engine = make_engine(settings.database_url)
    yield make_session_factory(engine)
    engine.dispose()


def store(sessions, *rows):
    with sessions.begin() as session:
        session.add_all(rows)


@asynccontextmanager
async def running_service(settings, start) -> AsyncIterator[User]:
    """Run the whole service, its lifespan, its routes and its pages, and give a simulated browser on it.

    The ``user`` fixture of NiceGUI runs only the lifespan of NiceGUI and sends each request to NiceGUI directly, so
    its pages would get no services.
    """
    app = create_app(settings, start=start)
    mount_ui(app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        yield User(client)


@pytest.fixture
async def user(nicegui_globals, settings, fake_starter):
    async with running_service(settings, fake_starter) as user:
        yield user


def text_of(user: User, marker: str) -> str:
    (element,) = user.find(marker=marker).elements
    return element.text


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
        Schedule(enabled=False, kind=ScheduleKind.INTERVAL, interval_s=600, timezone="America/New_York"),
        Run(status=RunStatus.SUCCEEDED, trigger=RunTrigger.SCHEDULE, started_at=datetime(2026, 1, 5, tzinfo=UTC)),
    )

    async with running_service(settings, fake_starter) as user:
        await user.open("/")

        assert text_of(user, "scheduler-state") == "Paused"
        assert text_of(user, "next-run") == "No run is planned"
        assert text_of(user, "timezone") == "America/New_York"
        assert text_of(user, "run-started") == "2026-01-04 19:00 EST"


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
        assert [item.text for item in items] == ["Dashboard", "Later page"]

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
    data = {"Europe/Kyiv", "2", "7", "boom"}

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
