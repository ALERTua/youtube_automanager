"""The scheduler page: the state of the schedule, its next runs, and the form that changes the schedule.

A save writes the ``schedule`` row and then tells the running scheduler, so the new schedule acts at once. The form
shows the next run times of the edited schedule before a save, and the page saves only a schedule that
``ScheduleSpec`` accepts and that has a run time. The page reads and writes the database in a thread, never on the
event loop. Each date shows in the time zone of the schedule. The state reads again at the start and at the end of each
run, and each ``REFRESH_SECONDS``, because a change of the schedule elsewhere comes with no run.
"""

import asyncio
import dataclasses
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

from nicegui import ui

from youtube_automanager import notify
from youtube_automanager.db.models import ScheduleKind
from youtube_automanager.i18n import N_, _
from youtube_automanager.scheduler import (
    MAX_INTERVAL_S,
    MIN_INTERVAL_S,
    SchedulerState,
    ScheduleSpec,
    load_schedule,
    save_enabled,
    save_schedule,
    upcoming_run_times,
)
from youtube_automanager.web.services import Services
from youtube_automanager.web.ui.common import read_timezone, run_now_button, time_label
from youtube_automanager.web.ui.dashboard import SCHEDULER_STATES
from youtube_automanager.web.ui.layout import register_page
from youtube_automanager.web.ui.live_runs import RunFollower

# The events of the runs read the state at each start and end of a run, so this read only catches the other changes.
REFRESH_SECONDS = 60.0
# The number of run times that the page shows, for the running schedule and for the edited one.
UPCOMING_COUNT = 5

SCHEDULE_KINDS = {
    ScheduleKind.CRON: N_("Cron"),
    ScheduleKind.INTERVAL: N_("Interval"),
}


@dataclass(frozen=True)
class Status:
    """The part of the page that shows the running schedule."""

    state: SchedulerState
    next_runs: list[datetime]
    timezone: str


async def take_status(services: Services) -> Status:
    timezone = await asyncio.to_thread(read_timezone, services.sessions, services.settings.tz)
    return Status(
        state=await services.scheduler.state(),
        next_runs=await services.scheduler.next_run_times(UPCOMING_COUNT),
        timezone=timezone,
    )


def _whole_seconds(value: float | None) -> int | None:
    if value is None:
        return None
    if not float(value).is_integer():
        raise ValueError(_("The interval must be a whole number of seconds."))
    return int(value)


def _is_valid(kind: ScheduleKind, timezone: str, cron: str | None, interval: int | None) -> bool:
    try:
        ScheduleSpec(kind=kind, timezone=timezone, cron_expression=cron, interval_s=interval)
    except ValueError:
        return False
    return True


def _show_run_times(times: list[datetime], zone: ZoneInfo, marker: str) -> None:
    for time in times:
        time_label(time, zone).mark(marker)


def _status_card(status: Status) -> None:
    zone = ZoneInfo(status.timezone)
    label, color = SCHEDULER_STATES[status.state]
    with ui.card().classes("flex-1 min-w-72").mark("status-card"):
        ui.label(_("Current schedule")).classes("text-h6")
        with ui.grid(columns="auto 1fr").classes("gap-x-4 gap-y-1 items-start"):
            ui.label(_("State")).classes("text-grey-7")
            ui.badge(_(label), color=color).mark("scheduler-state")
            ui.label(_("Time zone")).classes("text-grey-7")
            ui.label(zone.key).mark("timezone")
            ui.label(_("Next runs")).classes("text-grey-7")
            with ui.column().classes("gap-0"):
                if status.next_runs:
                    _show_run_times(status.next_runs, zone, "next-run")
                else:
                    ui.label(_("No run is planned")).mark("no-next-run")


@register_page("/scheduler", label=N_("Scheduler"), icon="schedule")
async def scheduler_page(services: Services) -> None:
    spec = await asyncio.to_thread(load_schedule, services.sessions, services.settings.tz)
    # The scan of the time zone files blocks, so it runs in a thread.
    timezones = sorted({*await asyncio.to_thread(available_timezones), spec.timezone})

    def read_form() -> ScheduleSpec:
        """Return the schedule of the form. Raise ValueError when ``ScheduleSpec`` refuses it.

        The field that the kind does not use goes into the row only when it is valid on its own. So a typo in that
        field blocks no save, and it leaves no wrong text in the row.
        """
        kind = kind_radio.value
        timezone = timezone_select.value or ""
        cron = (cron_input.value or "").strip() or None
        try:
            interval = _whole_seconds(interval_input.value)
        except ValueError:
            if kind is ScheduleKind.INTERVAL:
                raise
            interval = None
        if kind is ScheduleKind.CRON:
            interval = interval if _is_valid(ScheduleKind.INTERVAL, timezone, cron, interval) else None
        else:
            cron = cron if _is_valid(ScheduleKind.CRON, timezone, cron, interval) else None
        return ScheduleSpec(
            kind=kind,
            timezone=timezone,
            cron_expression=cron,
            interval_s=interval,
            enabled=bool(enabled_switch.value),
        )

    def check_form() -> tuple[ScheduleSpec, list[datetime]]:
        """Return the schedule of the form and its next run times. Raise ValueError when the form cannot be saved."""
        edited = read_form()
        # An off switch gives no run times, but the form shows when the schedule would run.
        times = upcoming_run_times(dataclasses.replace(edited, enabled=True), UPCOMING_COUNT)
        if not times:
            raise ValueError(_("The schedule has no run time."))
        return edited, times

    @ui.refreshable
    def status_view(status: Status) -> None:
        _status_card(status)

    @ui.refreshable
    def preview() -> None:
        try:
            edited, times = check_form()
        except ValueError as error:
            ui.label(str(error)).classes("text-negative").mark("schedule-error")
            return
        ui.label(_("Next runs of this schedule")).classes("text-grey-7")
        with ui.column().classes("gap-0"):
            _show_run_times(times, ZoneInfo(edited.timezone), "preview-time")
        if not edited.enabled:
            ui.label(_("The schedule is off. No run starts until you turn it on.")).mark("preview-off")

    # The number of the newest read of the status. A read that finishes after a newer one started drops its result.
    status_reads = 0

    async def refresh_status(*_event: object) -> None:
        nonlocal status_reads
        status_reads += 1
        read = status_reads
        status = await take_status(services)
        if read != status_reads:
            return
        status_view.refresh(status)
        pause_button.set_enabled(status.state is SchedulerState.RUNNING)
        resume_button.set_enabled(status.state is SchedulerState.PAUSED)

    async def tell_state(enabled: bool) -> None:
        # Apprise blocks, so the send runs in a thread, after the page shows the new state.
        await asyncio.to_thread(notify.send_to_stored_targets, services.sessions, notify.scheduler_state(not enabled))

    async def save() -> None:
        try:
            edited, _times = check_form()
        except ValueError:
            # The preview shows the error text next to the form.
            preview.refresh()
            return
        before = await services.scheduler.state()
        await asyncio.to_thread(save_schedule, services.sessions, edited)
        await services.scheduler.apply(edited)
        ui.notify(_("The schedule is saved."), type="positive")
        await refresh_status()
        # A save with the other position of the switch also pauses or resumes the schedule.
        if before is not SchedulerState.STOPPED and (before is SchedulerState.RUNNING) != edited.enabled:
            await tell_state(edited.enabled)

    async def set_enabled(enabled: bool) -> None:
        await asyncio.to_thread(save_enabled, services.sessions, enabled)
        if enabled:
            await services.scheduler.resume()
        else:
            await services.scheduler.pause()
        # The form keeps its other edits, so a later save does not undo the pause or the resume.
        enabled_switch.value = enabled
        await refresh_status()
        await tell_state(enabled)

    # The follower comes before the first read, so a run that starts during the read is not missed.
    follower = RunFollower(services.run_events, started=refresh_status, finished=refresh_status)
    initial = await take_status(services)
    ui.label(_("Scheduler")).classes("text-h5")
    with ui.row().classes("w-full gap-4 items-stretch"):
        status_view(initial)
        with ui.card().classes("flex-1 min-w-72"):
            ui.label(_("Actions")).classes("text-h6")
            with ui.row().classes("gap-2"):
                pause_button = ui.button(_("Pause"), icon="pause", on_click=lambda: set_enabled(False)).mark("pause")
                resume_button = ui.button(_("Resume"), icon="play_arrow", on_click=lambda: set_enabled(True)).mark(
                    "resume"
                )
                run_now_button(services, "run-now")
    pause_button.set_enabled(initial.state is SchedulerState.RUNNING)
    resume_button.set_enabled(initial.state is SchedulerState.PAUSED)

    with ui.card().classes("w-full"):
        ui.label(_("Schedule")).classes("text-h6")
        kind_radio = (
            ui.radio(
                {kind: _(label) for kind, label in SCHEDULE_KINDS.items()},
                value=spec.kind,
                on_change=lambda: preview.refresh(),
            )
            .props("inline")
            .mark("schedule-kind")
        )
        cron_input = (
            ui.input(_("Cron expression"), value=spec.cron_expression or "", on_change=lambda: preview.refresh())
            .props("outlined dense")
            .classes("w-full max-w-md font-mono")
            .mark("schedule-cron")
        )
        cron_input.bind_visibility_from(kind_radio, "value", lambda kind: kind is ScheduleKind.CRON)
        cron_hint = ui.label(
            _("Five fields: minute, hour, day, month, weekday. Example: 0 18 * * * is 18:00 each day.")
        ).classes("text-grey-7")
        cron_hint.bind_visibility_from(kind_radio, "value", lambda kind: kind is ScheduleKind.CRON)
        interval_input = (
            ui.number(
                _("Interval in seconds"),
                value=spec.interval_s,
                min=MIN_INTERVAL_S,
                max=MAX_INTERVAL_S,
                step=60,
                precision=0,
                on_change=lambda: preview.refresh(),
            )
            .props("outlined dense")
            .classes("w-full max-w-md")
            .mark("schedule-interval")
        )
        interval_input.bind_visibility_from(kind_radio, "value", lambda kind: kind is ScheduleKind.INTERVAL)
        interval_hint = ui.label(
            _("From {minimum} s ({minutes} minutes) to {maximum} s (one year). Example: 3600 is each hour.").format(
                minimum=MIN_INTERVAL_S, minutes=MIN_INTERVAL_S // 60, maximum=MAX_INTERVAL_S
            )
        ).classes("text-grey-7")
        interval_hint.bind_visibility_from(kind_radio, "value", lambda kind: kind is ScheduleKind.INTERVAL)
        timezone_select = (
            ui.select(
                timezones,
                label=_("Time zone"),
                value=spec.timezone,
                with_input=True,
                on_change=lambda: preview.refresh(),
            )
            .props("outlined dense")
            .classes("w-full max-w-md")
            .mark("schedule-timezone")
        )
        enabled_switch = ui.switch(_("Enabled"), value=spec.enabled, on_change=lambda: preview.refresh()).mark(
            "schedule-enabled"
        )
        preview()
        ui.button(_("Save"), icon="save", on_click=save).mark("schedule-save")

    if follower.start():
        await refresh_status()
    # The first read is above, so the timer starts with a pause.
    ui.timer(REFRESH_SECONDS, refresh_status, immediate=False)
