"""The yt-dlp block of the settings page: the version that the next run uses, the daily update, "Update now", and the
actions that go back to the previous release or to the yt-dlp of the image.

An update installs a release from PyPI and can take a few minutes, so the block waits for it in a thread. The block
reads and writes the database in a thread, never on the event loop. The switch, the channel and the time of the daily
update are part of the draft of the page. The floating Save of the page writes only the fields that changed, while it
holds the lock of the page. A save of the settings tells the scheduler at once, and a new channel installs at the next
update. "Update now" acts at once with the saved channel. The two other actions act at once after a confirmation, and
the daily update then skips the version that the user left. Each time shows in the time zone of the schedule.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from nicegui import ui
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.i18n import N_, _
from youtube_automanager.short_child import ChildBusy
from youtube_automanager.web.services import Services
from youtube_automanager.web.ui.account_lists import child_error_text
from youtube_automanager.web.ui.common import ResultLabel, confirm, format_time, read_timezone
from youtube_automanager.web.ui.dashboard import YTDLP_SOURCES
from youtube_automanager.web.ui.settings_draft import DraftPart, FormError, SettingsDraft, Write
from youtube_automanager.youtube.ytdlp_updater import (
    Source,
    active_version,
    image_version,
    installed_version,
    previous_dir,
)
from youtube_automanager.ytdlp_job import (
    CHANNELS,
    LastUpdate,
    SwitchResult,
    UpdateSettings,
    change_settings,
    parse_time,
    read_last_update,
    read_settings,
    read_skipped,
    time_text,
)

LOG = logging.getLogger(__name__)

CHANNEL_LABELS = {
    "stable": N_("Stable"),
    "nightly": N_("Nightly"),
}


@dataclass(frozen=True)
class YtdlpStatus:
    """The part of the block that shows the yt-dlp of the next run, the kept releases and the updates.

    ``previous`` is the version of the release that the data folder keeps to go back to, ``image`` the version of
    the image, and ``skipped`` the version that the daily update skips.
    """

    version: str | None
    source: Source
    previous: str | None
    image: str | None
    skipped: str | None
    last_update: LastUpdate | None
    next_update: datetime | None
    timezone: str


def _read_status(
    sessions: sessionmaker[Session], ytdlp_dir: Path, default_timezone: str, next_update: datetime | None
) -> YtdlpStatus:
    version, source = active_version(ytdlp_dir)
    return YtdlpStatus(
        version,
        source,
        installed_version(previous_dir(ytdlp_dir)),
        image_version(),
        read_skipped(sessions),
        read_last_update(sessions),
        next_update,
        read_timezone(sessions, default_timezone),
    )


async def take_status(services: Services) -> YtdlpStatus:
    next_updates = await services.scheduler.next_ytdlp_update_times(1)
    return await asyncio.to_thread(
        _read_status,
        services.sessions,
        services.settings.ytdlp_dir,
        services.settings.tz,
        next_updates[0] if next_updates else None,
    )


def result_text(record: LastUpdate) -> str:
    """Return the text of the result of an update."""
    if not record.ok:
        # Translators: {cause} is the English cause from the service, for example the error of uv.
        return _("The update failed: {cause}").format(cause=record.error or "")
    if record.skipped:
        # Translators: {version} is a version of yt-dlp, for example 2026.8.19.
        return _("{version} is the version that you left, so the daily update does not install it.").format(
            version=record.new_version
        )
    if not record.changed:
        return _("{version} is already the newest version.").format(version=record.new_version)
    if record.old_version is None:
        return _("Installed {new} into the data folder.").format(new=record.new_version)
    return _("Updated from {old} to {new}.").format(old=record.old_version, new=record.new_version)


def switch_text(result: SwitchResult) -> str:
    """Return the text of the result of a change of the active yt-dlp."""
    if result.error is not None:
        # Translators: {cause} is the English cause from the service.
        return _("The change failed: {cause}").format(cause=result.error)
    active = result.active or _("Unknown")
    if result.left is None:
        return _("yt-dlp {active} is active now.").format(active=active)
    # Translators: {active} and {left} are versions of yt-dlp, for example 2026.8.19.
    return _("yt-dlp {active} is active now. The daily update skips {left}.").format(active=active, left=result.left)


@dataclass
class UpdateState:
    status: YtdlpStatus
    stored: UpdateSettings
    # True while an update or a change of the active yt-dlp works.
    busy: bool = False


async def ytdlp_section(services: Services, draft: SettingsDraft) -> None:
    state = UpdateState(await take_status(services), await asyncio.to_thread(read_settings, services.sessions))

    def show_result(text: str, *, problem: bool = False) -> None:
        if problem:
            result_label.problem(text)
        else:
            result_label.ok(text)

    def show_error(text: str | None) -> None:
        error_label.set_text(text or "")
        error_label.set_visibility(bool(text))

    def show_work() -> None:
        status = state.status
        update_button.set_enabled(not state.busy)
        back_button.set_enabled(not state.busy and status.previous is not None)
        image_button.set_enabled(not state.busy and status.source == "data_folder" and status.image is not None)

    async def reload() -> None:
        state.status = await take_status(services)
        # The await rebuilds the view now, and not at the next turn of the event loop.
        await status_view.refresh()
        show_work()

    def read_form() -> UpdateSettings:
        """Return the settings of the form. Raise FormError when the form cannot be saved."""
        try:
            at = parse_time(time_input.value or "")
        except ValueError:
            raise FormError(_("Enter the time as HH:MM, from 00:00 to 23:59.")) from None
        return UpdateSettings(bool(enabled_switch.value), channel_radio.value, at)

    def changed() -> bool:
        try:
            return read_form() != state.stored
        except FormError:
            return True

    def show_stored() -> None:
        enabled_switch.value = state.stored.enabled
        channel_radio.value = state.stored.channel
        time_input.value = time_text(state.stored.at)

    def prepare() -> Write:
        stored, edited = state.stored, read_form()

        async def run() -> None:
            saved = await asyncio.to_thread(change_settings, services.sessions, stored, edited)
            await services.scheduler.plan_ytdlp_updates(saved.at, saved.enabled)
            # A field that this page did not change can hold the value of another page, and the form shows it.
            state.stored = saved
            show_stored()
            LOG.info("The yt-dlp update settings are now %s", saved.to_value())

        return Write(run, after=reload)

    async def update_now() -> None:
        if state.busy:
            return
        state.busy = True
        update_button.props("loading")
        show_work()
        show_result(_("The service installs the newest yt-dlp of the saved channel. This can take a few minutes."))
        try:
            record = await asyncio.to_thread(services.ytdlp.update_now)
        finally:
            state.busy = False
            update_button.props(remove="loading")
            show_work()
        if record is None:
            show_result(child_error_text(ChildBusy("")), problem=True)
            return
        # The text and the new state show together.
        await reload()
        show_result(result_text(record), problem=not record.ok)

    async def change_active(switch: Callable[[], SwitchResult | None]) -> None:
        # The question waits for the user, and an update can start meanwhile.
        if state.busy:
            return
        state.busy = True
        show_work()
        try:
            result = await asyncio.to_thread(switch)
        finally:
            state.busy = False
            show_work()
        if result is None:
            show_result(child_error_text(ChildBusy("")), problem=True)
            return
        await reload()
        show_result(switch_text(result), problem=result.error is not None)

    async def go_back() -> None:
        if state.busy:
            return
        status = state.status
        question = _(
            "Go back from yt-dlp {active} to {previous}? The daily update then skips {active}, and it installs only a "
            "newer version."
        ).format(active=status.version or _("Unknown"), previous=status.previous)
        if await confirm(question, _("Go back"), marker="ytdlp-go-back", classes="max-w-lg"):
            await change_active(services.ytdlp.go_back)

    async def use_image() -> None:
        if state.busy:
            return
        status = state.status
        question = _(
            "Use yt-dlp {image} of the image instead of {active} of the data folder? The service keeps {active} as "
            "the previous version. The daily update then skips {active}, and it installs only a newer version."
        ).format(image=status.image, active=status.version)
        if await confirm(question, _("Use the image version"), marker="ytdlp-use-image", classes="max-w-lg"):
            await change_active(services.ytdlp.use_image)

    @ui.refreshable
    def status_view() -> None:
        status = state.status
        zone = ZoneInfo(status.timezone)
        last = status.last_update
        with ui.grid(columns="auto 1fr").classes("gap-x-4 gap-y-1 items-center"):
            ui.label(_("Version")).classes("text-grey-7")
            ui.label(status.version or _("Unknown")).mark("ytdlp-version")
            ui.label(_("Source")).classes("text-grey-7")
            ui.label(_(YTDLP_SOURCES[status.source])).mark("ytdlp-source")
            ui.label(_("Previous version")).classes("text-grey-7")
            ui.label(status.previous or _("None")).mark("ytdlp-previous")
            ui.label(_("Skipped version")).classes("text-grey-7")
            ui.label(status.skipped or _("None")).mark("ytdlp-skipped")
            ui.label(_("Last update")).classes("text-grey-7")
            ui.label(format_time(last.at, zone) if last else _("Never")).mark("ytdlp-last-at")
            if last is not None:
                ui.label(_("Result")).classes("text-grey-7")
                result = ui.label(result_text(last)).classes("whitespace-pre-wrap break-words")
                result.mark("ytdlp-last-result")
                if not last.ok:
                    result.classes("text-negative")
            ui.label(_("Next update")).classes("text-grey-7")
            next_update = status.next_update
            ui.label(format_time(next_update, zone) if next_update else _("No update is planned")).mark("ytdlp-next")

    with ui.card().classes("w-full").mark("ytdlp-card"):
        ui.label(_("yt-dlp")).classes("text-h6")
        ui.label(
            _(
                "The service reads YouTube with yt-dlp. It installs the new releases from PyPI into its data folder, "
                "and the next run uses the new version without a new image of the service."
            )
        ).classes("text-grey-7")
        status_view()
        channel_radio = (
            ui.radio(
                {channel: _(CHANNEL_LABELS[channel]) for channel in CHANNELS},
                value=state.stored.channel,
                on_change=lambda: draft.update(),
            )
            .props("inline")
            .mark("ytdlp-channel")
        )
        ui.label(
            _(
                "The nightly builds get the fixes for the changes of YouTube first, and they change more often. A new "
                "channel installs at the next update."
            )
        ).classes("text-grey-7").mark("ytdlp-channel-help")
        enabled_switch = ui.switch(
            _("Update each day"), value=state.stored.enabled, on_change=lambda: draft.update()
        ).mark("ytdlp-enabled")
        time_input = (
            ui.input(_("Time of the update"), value=time_text(state.stored.at), on_change=lambda: draft.update())
            .props("type=time outlined dense")
            .classes("w-48")
            .mark("ytdlp-time")
        )
        # Translators: {zone} is the name of a time zone, for example Europe/Kyiv.
        ui.label(_("In the time zone of the schedule, {zone}.").format(zone=state.status.timezone)).classes(
            "text-grey-7"
        ).mark("ytdlp-time-help")
        error_label = ui.label().classes("text-negative").mark("ytdlp-error")
        error_label.set_visibility(False)
        with ui.row().classes("gap-2"):
            update_button = ui.button(_("Update now"), icon="system_update_alt", on_click=update_now).mark(
                "ytdlp-update-now"
            )
            back_button = (
                ui.button(_("Go back to the previous version"), icon="undo", on_click=go_back)
                .props("outline")
                .mark("ytdlp-go-back")
            )
            image_button = (
                ui.button(_("Use the image version"), icon="inventory_2", on_click=use_image)
                .props("outline")
                .mark("ytdlp-use-image")
            )
        result_label = ResultLabel().classes("whitespace-pre-wrap break-words").mark("ytdlp-result")
    show_work()
    draft.add(DraftPart(changed=changed, prepare=prepare, discard=show_stored, show_error=show_error))
