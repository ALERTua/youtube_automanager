import asyncio
import itertools
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from apprise.decorators import notify as apprise_notify
from nicegui import ui
from nicegui.elements.mixins.label_element import LabelElement
from nicegui.elements.mixins.text_element import TextElement
from sqlalchemy import select
from ui_support import (
    element_of,
    eventually,
    running_service,
    set_value,
    store,
    text_of,
    texts_of,
)

from youtube_automanager import i18n
from youtube_automanager.db.models import NotificationTarget, Run, RunStatus, RunTrigger, Schedule, ScheduleKind
from youtube_automanager.scheduler import MAX_INTERVAL_S, ScheduleSpec
from youtube_automanager.web.ui import scheduler_page
from youtube_automanager.web.ui.dashboard import TIME_FORMAT

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]

KYIV = ZoneInfo("Europe/Kyiv")
NEW_YORK = ZoneInfo("America/New_York")
TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+")
# How long a test waits for a click that must change nothing. A click that does change something shows it within ms.
SETTLE_SECONDS = 0.3
TEN_MINUTES = timedelta(minutes=10)


def next_evenings(count: int, zone: ZoneInfo = KYIV) -> list[str]:
    """The next ``count`` days at 18:00 in ``zone``, in the format of the page."""
    now = datetime.now(zone)
    evening = now.replace(hour=18, minute=0, second=0, microsecond=0)
    if evening <= now:
        evening += timedelta(days=1)
    return [(evening + timedelta(days=number)).strftime(TIME_FORMAT) for number in range(count)]


def times_of_day(texts: list[str]) -> set[str]:
    """The time of the day of each time text, for example ``07:30``."""
    return {text.split()[1] for text in texts}


def gaps(texts: list[str]) -> set[timedelta]:
    """The distances between the neighbor times of the time texts."""
    moments = [datetime.strptime(text.rsplit(" ", 1)[0], "%Y-%m-%d %H:%M").replace(tzinfo=UTC) for text in texts]
    return {later - earlier for earlier, later in itertools.pairwise(moments)}


def stored_schedule(sessions) -> Schedule:
    with sessions() as session:
        return session.get(Schedule, 1)


def stored_runs(sessions) -> list[Run]:
    with sessions() as session:
        return list(session.scalars(select(Run).order_by(Run.id)))


async def api_status(user) -> dict:
    return (await user.http_client.get("/api/status")).json()


async def api_next_run(user) -> datetime:
    return datetime.fromisoformat((await api_status(user))["next_run_at"])


async def preview_shows(user, expected: list[str]) -> None:
    await eventually(lambda: texts_of(user, "preview-time") == expected, "The preview of the edited schedule")


async def error_text(user) -> str:
    return (await eventually(lambda: texts_of(user, "schedule-error"), "The error text next to the form"))[0]


async def click_save(user) -> None:
    user.find(marker="schedule-save").click()
    await user.should_see("The schedule is saved.")


async def click_save_of_a_wrong_schedule(user) -> None:
    """Click the save button, and wait long enough for a save to show, if the page made one."""
    user.find(marker="schedule-save").click()
    await asyncio.sleep(SETTLE_SECONDS)
    assert not user.notify.contains("The schedule is saved.")


async def test_scheduler_page_is_in_the_drawer(user):
    await user.open("/")

    user.find(marker="nav:/scheduler").click()

    await user.should_see(marker="schedule-save")


async def test_page_shows_the_state_the_next_runs_and_the_form_of_the_default_schedule(user):
    await user.open("/scheduler")

    assert text_of(user, "scheduler-state") == "Running"
    assert text_of(user, "timezone") == "Europe/Kyiv"
    assert texts_of(user, "next-run") == next_evenings(5)
    assert element_of(user, "schedule-kind").value is ScheduleKind.CRON
    assert element_of(user, "schedule-cron").value == "0 18 * * *"
    assert element_of(user, "schedule-timezone").value == "Europe/Kyiv"
    assert element_of(user, "schedule-enabled").value is True
    assert texts_of(user, "preview-time") == next_evenings(5)
    await user.should_not_see(marker="schedule-error")
    await user.should_not_see(marker="schedule-interval")


async def test_a_paused_schedule_shows_its_own_state_zone_and_form(nicegui_globals, settings, sessions, fake_starter):
    store(
        sessions,
        Schedule(
            enabled=False,
            kind=ScheduleKind.INTERVAL,
            interval_s=900,
            cron_expression="5 4 * * *",
            timezone="America/New_York",
        ),
    )

    async with running_service(settings, fake_starter) as user:
        await user.open("/scheduler")

        assert text_of(user, "scheduler-state") == "Paused"
        assert text_of(user, "no-next-run") == "No run is planned"
        assert text_of(user, "timezone") == "America/New_York"
        assert element_of(user, "schedule-kind").value is ScheduleKind.INTERVAL
        assert element_of(user, "schedule-interval").value == 900
        assert element_of(user, "schedule-enabled").value is False
        assert not element_of(user, "pause").enabled
        assert element_of(user, "resume").enabled
        # The form still shows when the schedule would run, and tells that it is off.
        assert len(texts_of(user, "preview-time")) == 5
        await user.should_see(marker="preview-off")


async def test_saving_a_new_cron_expression_acts_without_a_restart(user, sessions):
    await user.open("/scheduler")
    assert times_of_day(texts_of(user, "next-run")) == {"18:00"}
    before = await api_next_run(user)

    # The typing goes through the interaction of the simulated user, as a person types into the field.
    user.find(marker="schedule-cron").clear().type("30 7 * * *")
    await eventually(lambda: times_of_day(texts_of(user, "preview-time")) == {"07:30"}, "The preview of 07:30")
    # The preview changes nothing in the running service.
    assert (await api_next_run(user)) == before
    await click_save(user)

    after = (await api_next_run(user)).astimezone(KYIV)
    assert (after.hour, after.minute) == (7, 30)
    assert times_of_day(texts_of(user, "next-run")) == {"07:30"}
    row = stored_schedule(sessions)
    assert (row.kind, row.cron_expression, row.enabled, row.timezone) == (
        ScheduleKind.CRON,
        "30 7 * * *",
        True,
        "Europe/Kyiv",
    )


async def test_saving_an_interval_acts_without_a_restart(user, sessions):
    await user.open("/scheduler")

    set_value(user, "schedule-kind", ScheduleKind.INTERVAL)
    await user.should_see(marker="schedule-interval")
    await user.should_not_see(marker="schedule-cron")
    set_value(user, "schedule-interval", 600)
    soon = datetime.now(UTC) + timedelta(seconds=600)
    await eventually(
        lambda: len(texts_of(user, "preview-time")) == 5 and gaps(texts_of(user, "preview-time")) == {TEN_MINUTES},
        "The preview of the interval",
    )
    await click_save(user)

    assert abs((await api_next_run(user)) - soon) < timedelta(seconds=30)
    row = stored_schedule(sessions)
    # The cron expression stays in the row, so a change back to cron finds it again.
    assert (row.kind, row.interval_s, row.cron_expression) == (ScheduleKind.INTERVAL, 600, "0 18 * * *")


async def test_the_time_zone_of_the_schedule_changes_the_run_times(user, sessions):
    await user.open("/scheduler")

    set_value(user, "schedule-timezone", "America/New_York")
    await preview_shows(user, next_evenings(5, NEW_YORK))
    await click_save(user)

    assert texts_of(user, "next-run") == next_evenings(5, NEW_YORK)
    assert text_of(user, "timezone") == "America/New_York"
    assert stored_schedule(sessions).timezone == "America/New_York"


@pytest.mark.parametrize(
    ("typed", "message"),
    [
        ("0 18 * *", "5 fields"),
        ("61 18 * * *", "maximum"),
        ("0 18 * * 8", "day of the week"),
        ("every day", "5 fields"),
        ("", "cron expression"),
    ],
)
async def test_a_wrong_cron_expression_shows_the_error_and_saves_nothing(user, sessions, typed, message):
    await user.open("/scheduler")
    before = await api_next_run(user)
    with pytest.raises(ValueError, match=message) as refused:
        ScheduleSpec(kind=ScheduleKind.CRON, cron_expression=typed or None, timezone="Europe/Kyiv")

    set_value(user, "schedule-cron", typed)

    assert await error_text(user) == str(refused.value)
    assert texts_of(user, "preview-time") == []
    await click_save_of_a_wrong_schedule(user)
    row = stored_schedule(sessions)
    assert (row.kind, row.cron_expression, row.timezone, row.enabled) == (
        ScheduleKind.CRON,
        "0 18 * * *",
        "Europe/Kyiv",
        True,
    )
    assert (await api_next_run(user)) == before


@pytest.mark.parametrize(
    ("interval", "message"),
    [
        (None, "1 s or more"),
        (0, "1 s or more"),
        (-60, "1 s or more"),
        (0.5, "whole number"),
        (MAX_INTERVAL_S + 1, "or less"),
        (10**12, "or less"),
    ],
)
async def test_a_wrong_interval_shows_the_error_and_saves_nothing(user, sessions, interval, message):
    await user.open("/scheduler")
    before = await api_next_run(user)

    set_value(user, "schedule-kind", ScheduleKind.INTERVAL)
    set_value(user, "schedule-interval", interval)

    assert message in await error_text(user)
    await click_save_of_a_wrong_schedule(user)
    row = stored_schedule(sessions)
    assert (row.kind, row.interval_s) == (ScheduleKind.CRON, None)
    assert (await api_next_run(user)) == before


async def test_the_longest_interval_is_saved_and_the_service_keeps_running(user, sessions):
    await user.open("/scheduler")

    set_value(user, "schedule-kind", ScheduleKind.INTERVAL)
    set_value(user, "schedule-interval", MAX_INTERVAL_S)
    await eventually(lambda: len(texts_of(user, "preview-time")) == 5, "The preview of the longest interval")
    await click_save(user)

    assert stored_schedule(sessions).interval_s == MAX_INTERVAL_S
    assert text_of(user, "scheduler-state") == "Running"
    assert len(texts_of(user, "next-run")) == 5


async def test_a_schedule_without_a_run_time_is_refused(user, sessions):
    await user.open("/scheduler")

    # 31 February never comes.
    set_value(user, "schedule-cron", "0 0 31 2 *")

    assert await error_text(user) == "The schedule has no run time."
    await click_save_of_a_wrong_schedule(user)
    assert stored_schedule(sessions).cron_expression == "0 18 * * *"


@pytest.mark.parametrize("unused", [0.5, 0, -5, MAX_INTERVAL_S + 1])
async def test_a_wrong_interval_in_an_unused_field_blocks_no_save_and_is_not_stored(user, sessions, unused):
    await user.open("/scheduler")

    set_value(user, "schedule-interval", unused)
    set_value(user, "schedule-cron", "15 6 * * *")
    await eventually(lambda: times_of_day(texts_of(user, "preview-time")) == {"06:15"}, "The preview of 06:15")
    await click_save(user)

    row = stored_schedule(sessions)
    assert (row.kind, row.cron_expression, row.interval_s) == (ScheduleKind.CRON, "15 6 * * *", None)


async def test_a_wrong_cron_expression_in_an_unused_field_blocks_no_save_and_is_not_stored(user, sessions):
    await user.open("/scheduler")

    set_value(user, "schedule-cron", "every day")
    set_value(user, "schedule-kind", ScheduleKind.INTERVAL)
    set_value(user, "schedule-interval", 600)
    await eventually(lambda: gaps(texts_of(user, "preview-time")) == {TEN_MINUTES}, "The preview of the interval")
    await click_save(user)

    row = stored_schedule(sessions)
    assert (row.kind, row.interval_s, row.cron_expression) == (ScheduleKind.INTERVAL, 600, None)


async def test_the_switch_saves_a_pause_that_survives_a_restart(user, sessions):
    await user.open("/scheduler")

    user.find(marker="schedule-enabled").click()
    await user.should_see(marker="preview-off")
    await click_save(user)

    assert text_of(user, "scheduler-state") == "Paused"
    assert text_of(user, "no-next-run") == "No run is planned"
    assert (await api_status(user))["scheduler_state"] == "paused"
    # The service reads this row at its start, so the schedule stays paused after a restart.
    assert stored_schedule(sessions).enabled is False


async def test_pause_and_resume_write_the_row_and_keep_the_unsaved_edits(user, sessions):
    await user.open("/scheduler")
    assert not element_of(user, "resume").enabled
    set_value(user, "schedule-cron", "5 5 * * *")

    user.find(marker="pause").click()
    await eventually(lambda: text_of(user, "scheduler-state") == "Paused", "The pause")

    assert stored_schedule(sessions).enabled is False
    assert (await api_status(user))["scheduler_state"] == "paused"
    assert element_of(user, "schedule-enabled").value is False
    assert not element_of(user, "pause").enabled
    assert element_of(user, "resume").enabled
    # The buttons change only the enabled flag, so the form keeps its edit, and the row keeps its old expression.
    assert element_of(user, "schedule-cron").value == "5 5 * * *"
    assert stored_schedule(sessions).cron_expression == "0 18 * * *"

    user.find(marker="resume").click()
    await eventually(lambda: text_of(user, "scheduler-state") == "Running", "The resume")

    assert stored_schedule(sessions).enabled is True
    assert (await api_status(user))["scheduler_state"] == "running"
    assert element_of(user, "schedule-enabled").value is True
    assert element_of(user, "pause").enabled
    assert not element_of(user, "resume").enabled
    assert len(texts_of(user, "next-run")) == 5


DELIVERED: list[str] = []
PAUSED_TITLE = "YouTube AutoManager: the schedule is paused"
RESUMED_TITLE = "YouTube AutoManager: the schedule is resumed"


@apprise_notify(on="testschedule")
def _capture(body, title, notify_type, *args, meta, **kwargs):
    DELIVERED.append(title)
    return True


@pytest.fixture
def delivered(sessions):
    """A target that takes the event of a pause or a resume, and the titles that it got."""
    DELIVERED.clear()
    store(
        sessions,
        NotificationTarget(name="t", url="testschedule://t", min_level=0, events=["scheduler_state"], enabled=True),
    )
    yield DELIVERED
    DELIVERED.clear()


async def test_pause_and_resume_send_the_state_of_the_schedule(user, delivered):
    await user.open("/scheduler")

    user.find(marker="pause").click()
    await eventually(lambda: delivered == [PAUSED_TITLE], "The notification of the pause")
    user.find(marker="resume").click()
    await eventually(lambda: delivered == [PAUSED_TITLE, RESUMED_TITLE], "The notification of the resume")


async def test_a_save_that_turns_the_switch_sends_the_state_and_a_save_that_keeps_it_sends_nothing(user, delivered):
    await user.open("/scheduler")

    set_value(user, "schedule-cron", "5 5 * * *")
    await click_save(user)
    await asyncio.sleep(SETTLE_SECONDS)
    assert delivered == []

    user.find(marker="schedule-enabled").click()
    await user.should_see(marker="preview-off")
    await click_save(user)
    await eventually(lambda: delivered == [PAUSED_TITLE], "The notification of the pause")

    user.find(marker="schedule-enabled").click()
    await user.should_see(marker="preview-time")
    await click_save(user)
    await eventually(lambda: delivered == [PAUSED_TITLE, RESUMED_TITLE], "The notification of the resume")


async def test_run_now_starts_a_manual_run(user, sessions, fake_starter):
    await user.open("/scheduler")

    user.find(marker="run-now").click()

    await user.should_see("Run 1 started.")
    await eventually(
        lambda: [run.status for run in stored_runs(sessions)] == [RunStatus.SUCCEEDED], "The end of the run"
    )
    assert [run.trigger for run in stored_runs(sessions)] == [RunTrigger.MANUAL]
    assert len(fake_starter.calls) == 1


async def test_run_now_while_a_run_is_active_starts_nothing(user, sessions, fake_starter):
    fake_starter.plan.append({"blocking": True})
    await user.open("/scheduler")

    user.find(marker="run-now").click()
    await user.should_see("Run 1 started.")
    await eventually(fake_starter.child_started.is_set, "The start of the child")
    user.find(marker="run-now").click()

    await user.should_see("A run is already active, so no new run started.")
    fake_starter.children[0].finish(0)
    await eventually(
        lambda: [run.status for run in stored_runs(sessions)] == [RunStatus.SUCCEEDED], "The end of the run"
    )
    assert len(fake_starter.calls) == 1


async def test_the_state_refreshes_itself(user, sessions, monkeypatch):
    monkeypatch.setattr(scheduler_page, "REFRESH_SECONDS", 0.1)
    await user.open("/scheduler")
    assert text_of(user, "timezone") == "Europe/Kyiv"

    with sessions.begin() as session:
        session.get(Schedule, 1).timezone = "America/New_York"

    await eventually(lambda: text_of(user, "timezone") == "America/New_York", "The refresh of the time zone")


async def test_each_text_of_the_scheduler_page_goes_through_the_translation(
    nicegui_globals, settings, sessions, fake_starter, monkeypatch
):
    class Marking:
        def gettext(self, message):
            return f"<<{message}>>"

    monkeypatch.setattr(i18n, "_translations", Marking())
    data = {"Europe/Kyiv"}
    texts = []

    async with running_service(settings, fake_starter) as user:
        await user.open("/scheduler")
        for element in user.client.elements.values():
            if isinstance(element, TextElement) and element.text:
                texts.append(element.text)
            if isinstance(element, LabelElement) and element.props.get("label"):
                texts.append(element.props["label"])
            if isinstance(element, ui.radio):
                texts.extend(element.options.values())

    assert "<<Cron>>" in texts
    assert "<<Interval in seconds>>" in texts
    untranslated = [
        text
        for text in texts
        if not (text.startswith("<<") and text.endswith(">>")) and text not in data and not TIME.fullmatch(text)
    ]
    assert untranslated == []
