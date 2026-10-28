"""Tests of the yt-dlp block of the settings page. The autouse fake ``ytdlp_install`` stands in for PyPI and uv."""

import asyncio
import importlib.metadata
import re
import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from ui_support import (
    element_of,
    elements_of,
    eventually,
    moment_of,
    moment_text_of,
    running_service,
    set_value,
    store,
    text_of,
)

from youtube_automanager.db.models import Schedule, ScheduleKind, Setting
from youtube_automanager.runner import sync_lock_held
from youtube_automanager.youtube import ytdlp_updater
from youtube_automanager.ytdlp_check import check_release_in_child
from youtube_automanager.ytdlp_job import LAST_UPDATE_KEY, SETTINGS_KEY, SKIPPED_KEY

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]

KYIV = ZoneInfo("Europe/Kyiv")
TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+")
IMAGE = importlib.metadata.version("yt-dlp")
OLD = "2026.7.1"
NEW = "2026.8.19"
BUSY_TEXT = "Another task works with YouTube now"
# How long a test waits for a click that must change nothing.
SETTLE_SECONDS = 0.3


def setting(sessions, key):
    with sessions() as session:
        row = session.get(Setting, key)
        return None if row is None else row.value


def next_kyiv_time(hour: int, minute: int) -> str:
    now = datetime.now(KYIV)
    planned = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if planned <= now:
        planned = (planned + timedelta(days=1)).replace(hour=hour, minute=minute)
    return planned.strftime("%Y-%m-%d %H:%M %Z")


async def test_a_new_service_uses_the_ytdlp_of_the_image_and_updates_it_each_day_at_0400(user):
    await user.open("/settings")

    await user.should_see(marker="ytdlp-card")
    assert text_of(user, "ytdlp-version") == IMAGE
    assert text_of(user, "ytdlp-source") == "Image of the service"
    assert text_of(user, "ytdlp-last-at") == "Never"
    await user.should_not_see(marker="ytdlp-last-result")
    assert text_of(user, "ytdlp-next") == next_kyiv_time(4, 0)
    assert element_of(user, "ytdlp-channel").value == "stable"
    assert element_of(user, "ytdlp-enabled").value is True
    assert element_of(user, "ytdlp-time").value == "04:00"
    assert text_of(user, "ytdlp-time-help") == "In the time zone of the schedule, Europe/Kyiv."


async def test_the_release_of_the_data_folder_and_the_last_update_show_on_the_page(
    nicegui_globals, settings, sessions, fake_starter, ytdlp_install
):
    ytdlp_install.write_release(settings.ytdlp_dir, OLD)
    record = {
        "at": "2026-10-12T01:00:00+00:00",
        "ok": False,
        "channel": "nightly",
        "old_version": OLD,
        "new_version": NEW,
        "error": "uv exited with code 2: error: No solution found",
    }
    store(sessions, Setting(key=LAST_UPDATE_KEY, value=record))

    async with running_service(settings, fake_starter) as user:
        await user.open("/settings")

        assert text_of(user, "ytdlp-version") == OLD
        assert text_of(user, "ytdlp-source") == "Data folder"
        assert text_of(user, "ytdlp-last-at") == "2026-10-12 04:00 EEST"
        assert moment_of(user, "ytdlp-last-at") == ("2026-10-12T04:00:00+03:00", "Europe/Kyiv")
        assert (moment_text_of(user, "ytdlp-next"), moment_of(user, "ytdlp-next")[1]) == (
            text_of(user, "ytdlp-next"),
            "Europe/Kyiv",
        )
        assert text_of(user, "ytdlp-last-result") == (
            "The update failed: uv exited with code 2: error: No solution found"
        )
        assert "text-negative" in element_of(user, "ytdlp-last-result").classes


def draft_shown(user) -> bool:
    return element_of(user, "settings-draft").visible


async def test_a_save_stores_the_settings_and_moves_the_update_without_a_restart(user, sessions, ytdlp_install):
    await user.open("/settings")
    set_value(user, "ytdlp-channel", "nightly")
    set_value(user, "ytdlp-time", "05:30")
    assert setting(sessions, SETTINGS_KEY) is None

    user.find(marker="settings-save").click()

    await user.should_see("The settings are saved.")
    assert setting(sessions, SETTINGS_KEY) == {"enabled": True, "channel": "nightly", "time": "05:30"}
    await eventually(lambda: text_of(user, "ytdlp-next") == next_kyiv_time(5, 30), "The new time of the update")
    assert not draft_shown(user)
    # A new channel installs at the next update, not at once.
    assert ytdlp_install.calls == []

    set_value(user, "ytdlp-enabled", False)
    user.find(marker="settings-save").click()

    await eventually(lambda: text_of(user, "ytdlp-next") == "No update is planned", "The update off")
    assert setting(sessions, SETTINGS_KEY) == {"enabled": False, "channel": "nightly", "time": "05:30"}
    assert ytdlp_install.calls == []


async def test_a_form_that_equals_the_stored_settings_has_nothing_to_save(user, sessions):
    await user.open("/settings")
    assert not draft_shown(user)

    set_value(user, "ytdlp-channel", "nightly")
    await eventually(lambda: draft_shown(user), "The buttons of the draft")
    set_value(user, "ytdlp-channel", "stable")
    await eventually(lambda: not draft_shown(user), "No buttons of the draft")
    # Another text of the same time is the same setting.
    set_value(user, "ytdlp-time", "4:00")

    await asyncio.sleep(SETTLE_SECONDS)
    assert not draft_shown(user)
    assert setting(sessions, SETTINGS_KEY) is None


@pytest.mark.parametrize("value", ["25:00", "", None, "noon"])
async def test_a_wrong_time_shows_the_error_and_stores_nothing(user, sessions, value):
    await user.open("/settings")
    set_value(user, "ytdlp-channel", "nightly")
    set_value(user, "ytdlp-time", value)

    user.find(marker="settings-save").click()

    await user.should_see(marker="ytdlp-error")
    assert text_of(user, "ytdlp-error") == "Enter the time as HH:MM, from 00:00 to 23:59."
    await asyncio.sleep(SETTLE_SECONDS)
    assert setting(sessions, SETTINGS_KEY) is None
    assert text_of(user, "ytdlp-next") == next_kyiv_time(4, 0)

    # The error goes when the form equals the stored settings again.
    set_value(user, "ytdlp-channel", "stable")
    set_value(user, "ytdlp-time", "04:00")
    await eventually(lambda: not draft_shown(user), "No buttons of the draft")
    await user.should_not_see(marker="ytdlp-error")


async def test_a_save_writes_only_the_fields_that_the_page_changed(user, sessions):
    await user.open("/settings")
    set_value(user, "ytdlp-channel", "nightly")
    # Another page moves the update to 06:15 after this page read the settings.
    store(sessions, Setting(key=SETTINGS_KEY, value={"enabled": True, "channel": "stable", "time": "06:15"}))

    user.find(marker="settings-save").click()

    await user.should_see("The settings are saved.")
    assert setting(sessions, SETTINGS_KEY) == {"enabled": True, "channel": "nightly", "time": "06:15"}
    # The form and the scheduler follow the database.
    assert element_of(user, "ytdlp-time").value == "06:15"
    assert not draft_shown(user)
    await eventually(lambda: text_of(user, "ytdlp-next") == next_kyiv_time(6, 15), "The time of the other page")


async def test_update_now_shows_a_spinner_and_then_the_result_and_the_new_version(
    nicegui_globals, settings, sessions, fake_starter, ytdlp_install
):
    ytdlp_install.write_release(settings.ytdlp_dir, OLD)
    ytdlp_install.plan.append({"version": NEW})
    ytdlp_install.release = threading.Event()
    store(sessions, Setting(key=SETTINGS_KEY, value={"enabled": True, "channel": "nightly", "time": "04:00"}))

    async with running_service(settings, fake_starter) as user:
        await user.open("/settings")
        assert text_of(user, "ytdlp-version") == OLD

        user.find(marker="ytdlp-update-now").click()
        await eventually(lambda: ytdlp_install.calls, "The start of the update")

        button = element_of(user, "ytdlp-update-now")
        assert "loading" in button.props
        assert button.props["aria-label"] == "Update now"
        assert not button.enabled
        assert text_of(user, "ytdlp-result") == (
            "The service installs the newest yt-dlp of the saved channel. This can take a few minutes."
        )
        user.find(marker="ytdlp-update-now").click()
        await asyncio.sleep(SETTLE_SECONDS)
        assert len(ytdlp_install.calls) == 1

        ytdlp_install.release.set()

        await eventually(lambda: text_of(user, "ytdlp-result") == f"Updated from {OLD} to {NEW}.", "The result")
        assert "loading" not in element_of(user, "ytdlp-update-now").props
        assert "aria-label" not in element_of(user, "ytdlp-update-now").props
        assert element_of(user, "ytdlp-update-now").enabled
        assert text_of(user, "ytdlp-version") == NEW
        assert text_of(user, "ytdlp-source") == "Data folder"
        assert TIME.fullmatch(text_of(user, "ytdlp-last-at"))
        assert text_of(user, "ytdlp-last-result") == f"Updated from {OLD} to {NEW}."

    assert ytdlp_install.calls == [(settings.ytdlp_dir, "nightly")]
    assert ytdlp_install.lock_taken == [True]
    assert setting(sessions, LAST_UPDATE_KEY)["new_version"] == NEW
    # The new release replaces the active one only after its check in a child process.
    [options] = ytdlp_install.options
    assert options["smoke"].func is check_release_in_child
    assert options["skipped"] is None


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        ([], f"{IMAGE} is already the newest version."),
        ([{"version": NEW}], f"Installed {NEW} into the data folder."),
    ],
    ids=["current", "first-install"],
)
async def test_update_now_tells_the_kind_of_result(user, ytdlp_install, settings, plan, expected):
    if not plan:
        ytdlp_install.write_release(settings.ytdlp_dir, IMAGE)
    ytdlp_install.plan.extend(plan)
    await user.open("/settings")

    user.find(marker="ytdlp-update-now").click()

    await eventually(lambda: text_of(user, "ytdlp-result") == expected, "The result")
    assert "text-negative" not in element_of(user, "ytdlp-result").classes


async def test_a_failed_update_shows_its_cause(user, ytdlp_install):
    ytdlp_install.plan.append({"version": NEW, "error": "uv exited with code 2: error: No solution found"})
    await user.open("/settings")

    user.find(marker="ytdlp-update-now").click()

    expected = "The update failed: uv exited with code 2: error: No solution found"
    await eventually(lambda: text_of(user, "ytdlp-result") == expected, "The failure")
    assert "text-negative" in element_of(user, "ytdlp-result").classes
    assert text_of(user, "ytdlp-last-result") == expected
    assert text_of(user, "ytdlp-source") == "Image of the service"


async def test_update_now_while_the_sync_lock_is_taken_installs_nothing(user, sessions, ytdlp_install):
    await user.open("/settings")

    with sync_lock_held():
        user.find(marker="ytdlp-update-now").click()
        await eventually(lambda: text_of(user, "ytdlp-result").startswith(BUSY_TEXT), "The busy text")

    assert "text-negative" in element_of(user, "ytdlp-result").classes
    assert ytdlp_install.calls == []
    assert setting(sessions, LAST_UPDATE_KEY) is None
    assert element_of(user, "ytdlp-update-now").enabled


async def test_the_next_update_follows_the_time_zone_of_the_schedule(nicegui_globals, settings, sessions, fake_starter):
    store(sessions, Schedule(enabled=True, kind=ScheduleKind.CRON, cron_expression="0 18 * * *", timezone="UTC"))

    async with running_service(settings, fake_starter) as user:
        await user.open("/settings")

        assert text_of(user, "ytdlp-time-help") == "In the time zone of the schedule, UTC."
        next_update = datetime.strptime(text_of(user, "ytdlp-next"), "%Y-%m-%d %H:%M UTC").replace(tzinfo=UTC)
        assert (next_update.hour, next_update.minute) == (4, 0)


# The previous version and the yt-dlp of the image


def previous(settings):
    return ytdlp_updater.previous_dir(settings.ytdlp_dir)


async def answer(user, marker, confirm):
    """Answer the open question of ``marker``: confirm it or cancel it."""
    await eventually(lambda: elements_of(user, f"{marker}-question"), "The question")
    user.find(marker=f"{marker}-confirm" if confirm else f"{marker}-cancel").click()


async def test_the_card_shows_the_previous_and_the_skipped_version(
    nicegui_globals, settings, sessions, fake_starter, ytdlp_install
):
    ytdlp_install.write_release(previous(settings), OLD)
    ytdlp_install.write_release(settings.ytdlp_dir, NEW)
    store(sessions, Setting(key=SKIPPED_KEY, value="2026.9.1"))

    async with running_service(settings, fake_starter) as user:
        await user.open("/settings")

        assert text_of(user, "ytdlp-version") == NEW
        assert text_of(user, "ytdlp-previous") == OLD
        assert text_of(user, "ytdlp-skipped") == "2026.9.1"
        assert element_of(user, "ytdlp-go-back").enabled
        assert element_of(user, "ytdlp-use-image").enabled


async def test_a_new_service_has_nothing_to_go_back_to(user):
    await user.open("/settings")

    assert text_of(user, "ytdlp-previous") == "None"
    assert text_of(user, "ytdlp-skipped") == "None"
    assert not element_of(user, "ytdlp-go-back").enabled
    assert not element_of(user, "ytdlp-use-image").enabled


async def test_go_back_after_a_confirmation_puts_the_previous_version_back_and_skips_the_new_one(
    nicegui_globals, settings, sessions, fake_starter, ytdlp_install
):
    ytdlp_install.write_release(settings.ytdlp_dir, OLD)
    ytdlp_install.plan.append({"version": NEW})

    async with running_service(settings, fake_starter) as user:
        await user.open("/settings")
        user.find(marker="ytdlp-update-now").click()
        await eventually(lambda: text_of(user, "ytdlp-previous") == OLD, "The kept previous version")

        user.find(marker="ytdlp-go-back").click()
        await answer(user, "ytdlp-go-back", confirm=False)
        await asyncio.sleep(SETTLE_SECONDS)
        assert ytdlp_updater.installed_version(settings.ytdlp_dir) == NEW

        user.find(marker="ytdlp-go-back").click()
        await eventually(lambda: elements_of(user, "ytdlp-go-back-question"), "The second question")
        assert text_of(user, "ytdlp-go-back-question") == (
            f"Go back from yt-dlp {NEW} to {OLD}? The daily update then skips {NEW}, and it installs only a newer "
            "version."
        )
        await answer(user, "ytdlp-go-back", confirm=True)

        expected = f"yt-dlp {OLD} is active now. The daily update skips {NEW}."
        await eventually(lambda: text_of(user, "ytdlp-result") == expected, "The result")
        assert text_of(user, "ytdlp-version") == OLD
        assert text_of(user, "ytdlp-previous") == NEW
        assert text_of(user, "ytdlp-skipped") == NEW

    assert ytdlp_updater.installed_version(settings.ytdlp_dir) == OLD
    assert setting(sessions, SKIPPED_KEY) == NEW


async def test_use_image_after_a_confirmation_moves_the_release_aside_and_skips_its_version(
    nicegui_globals, settings, sessions, fake_starter, ytdlp_install
):
    ytdlp_install.write_release(settings.ytdlp_dir, NEW)

    async with running_service(settings, fake_starter) as user:
        await user.open("/settings")
        user.find(marker="ytdlp-use-image").click()
        await answer(user, "ytdlp-use-image", confirm=True)

        expected = f"yt-dlp {IMAGE} is active now. The daily update skips {NEW}."
        await eventually(lambda: text_of(user, "ytdlp-result") == expected, "The result")
        assert text_of(user, "ytdlp-version") == IMAGE
        assert text_of(user, "ytdlp-source") == "Image of the service"
        assert text_of(user, "ytdlp-previous") == NEW
        assert not element_of(user, "ytdlp-use-image").enabled
        assert element_of(user, "ytdlp-go-back").enabled

    assert ytdlp_updater.active_version(settings.ytdlp_dir) == (IMAGE, "image")
    assert setting(sessions, SKIPPED_KEY) == NEW


async def test_a_change_while_the_sync_lock_is_taken_changes_nothing(
    nicegui_globals, settings, sessions, fake_starter, ytdlp_install
):
    ytdlp_install.write_release(previous(settings), OLD)
    ytdlp_install.write_release(settings.ytdlp_dir, NEW)

    async with running_service(settings, fake_starter) as user:
        await user.open("/settings")
        user.find(marker="ytdlp-go-back").click()
        await eventually(lambda: elements_of(user, "ytdlp-go-back-question"), "The question")
        with sync_lock_held():
            user.find(marker="ytdlp-go-back-confirm").click()
            await eventually(lambda: text_of(user, "ytdlp-result").startswith(BUSY_TEXT), "The busy text")

    assert ytdlp_updater.installed_version(settings.ytdlp_dir) == NEW
    assert setting(sessions, SKIPPED_KEY) is None


async def test_a_skipped_daily_update_tells_why_it_installed_nothing(nicegui_globals, settings, sessions, fake_starter):
    record = {
        "at": "2026-10-12T01:00:00+00:00",
        "ok": True,
        "channel": "stable",
        "old_version": OLD,
        "new_version": NEW,
        "error": None,
        "skipped": True,
    }
    store(sessions, Setting(key=LAST_UPDATE_KEY, value=record))

    async with running_service(settings, fake_starter) as user:
        await user.open("/settings")

        assert text_of(user, "ytdlp-last-result") == (
            f"{NEW} is the version that you left, so the daily update does not install it."
        )
