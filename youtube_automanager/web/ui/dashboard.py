"""The dashboard: the state of the cookie session, of the scheduler and of the last run, and the version of yt-dlp.

The page reads the database and the yt-dlp folder in a thread, never on the event loop, and reads them again each
``REFRESH_SECONDS``. Each date shows in the time zone of the schedule.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import Account, Run, RunStatus, RunTrigger, SessionState
from youtube_automanager.i18n import N_, _
from youtube_automanager.scheduler import SchedulerState
from youtube_automanager.web.services import Services
from youtube_automanager.web.ui.common import RUN_STATUSES, RUN_TRIGGERS, SESSION_STATES, format_time, stored_timezone
from youtube_automanager.web.ui.layout import register_page
from youtube_automanager.youtube.ytdlp_updater import Source, active_version

REFRESH_SECONDS = 10.0

SCHEDULER_STATES = {
    SchedulerState.RUNNING: (N_("Running"), "positive"),
    SchedulerState.PAUSED: (N_("Paused"), "warning"),
    SchedulerState.STOPPED: (N_("Stopped"), "negative"),
}
YTDLP_SOURCES: dict[Source, str] = {
    "data_folder": N_("Data folder"),
    "image": N_("Image of the service"),
}


@dataclass(frozen=True)
class CookieSession:
    state: SessionState
    last_checked_at: datetime | None
    earliest_cookie_expiry: datetime | None


@dataclass(frozen=True)
class LastRun:
    started_at: datetime
    status: RunStatus
    trigger: RunTrigger
    added_count: int
    seen_count: int
    error_text: str | None


@dataclass(frozen=True)
class StoredState:
    """The part of the dashboard that comes from the database."""

    # None when no cookies are stored.
    session: CookieSession | None
    last_run: LastRun | None
    timezone: str


@dataclass(frozen=True)
class Snapshot:
    stored: StoredState
    scheduler_state: SchedulerState
    next_run_at: datetime | None
    # The yt-dlp that the next run imports, and where it comes from.
    ytdlp_version: str | None
    ytdlp_source: Source


def read_stored_state(sessions: sessionmaker[Session], default_timezone: str) -> StoredState:
    """Read the first account, the newest run and the time zone of the schedule. This call blocks."""
    with sessions() as session:
        account = session.execute(
            select(
                Account.session_state,
                Account.last_checked_at,
                Account.earliest_cookie_expiry,
                Account.cookies.is_not(None).label("has_cookies"),
            )
            .order_by(Account.id)
            .limit(1)
        ).first()
        run = session.scalars(select(Run).order_by(Run.started_at.desc(), Run.id.desc()).limit(1)).first()
        timezone = stored_timezone(session, default_timezone)
        return StoredState(
            session=(
                CookieSession(account.session_state, account.last_checked_at, account.earliest_cookie_expiry)
                if account is not None and account.has_cookies
                else None
            ),
            last_run=(
                LastRun(run.started_at, run.status, run.trigger, run.added_count, run.seen_count, run.error_text)
                if run is not None
                else None
            ),
            timezone=timezone,
        )


async def take_snapshot(services: Services) -> Snapshot:
    """Return the state that the dashboard shows."""
    # Not run.io_bound of NiceGUI: it returns None instead of the CancelledError of a cancelled call. A timer
    # whose callback swallows the cancel goes on, and the stop of the service waits 2 s for it and logs an error.
    stored = await asyncio.to_thread(read_stored_state, services.sessions, services.settings.tz)
    ytdlp_version, ytdlp_source = await asyncio.to_thread(active_version, services.settings.ytdlp_dir)
    next_run_times = await services.scheduler.next_run_times(1)
    return Snapshot(
        stored=stored,
        scheduler_state=await services.scheduler.state(),
        next_run_at=next_run_times[0] if next_run_times else None,
        ytdlp_version=ytdlp_version,
        ytdlp_source=ytdlp_source,
    )


def _field(label: str) -> ui.element:
    """Add one line of a card: the translated ``label`` and an empty cell for the value. Return the cell."""
    ui.label(label).classes("text-grey-7")
    return ui.row().classes("items-center gap-2")


def _value(text: str, marker: str) -> None:
    ui.label(text).mark(marker)


def _badge(label_and_color: tuple[str, str], marker: str) -> None:
    label, color = label_and_color
    ui.badge(_(label), color=color).mark(marker)


def _session_card(session: CookieSession | None, zone: ZoneInfo) -> None:
    with ui.card().classes("flex-1 min-w-72").mark("session-card"):
        ui.label(_("Cookie session")).classes("text-h6")
        if session is None:
            ui.label(_("No cookies are stored yet.")).mark("no-cookies")
            return
        with ui.grid(columns="auto 1fr").classes("gap-x-4 gap-y-1 items-center"):
            with _field(_("State")):
                _badge(SESSION_STATES[session.state], "session-state")
            with _field(_("Last check")):
                checked = session.last_checked_at
                _value(format_time(checked, zone) if checked else _("Never"), "session-checked")
            with _field(_("Earliest cookie expiry")):
                expiry = session.earliest_cookie_expiry
                _value(format_time(expiry, zone) if expiry else _("No expiry date"), "session-expiry")


def _scheduler_card(state: SchedulerState, next_run_at: datetime | None, zone: ZoneInfo) -> None:
    with ui.card().classes("flex-1 min-w-72").mark("scheduler-card"):
        ui.label(_("Scheduler")).classes("text-h6")
        with ui.grid(columns="auto 1fr").classes("gap-x-4 gap-y-1 items-center"):
            with _field(_("State")):
                _badge(SCHEDULER_STATES[state], "scheduler-state")
            with _field(_("Next run")):
                _value(format_time(next_run_at, zone) if next_run_at else _("No run is planned"), "next-run")
            with _field(_("Time zone")):
                _value(zone.key, "timezone")


def _last_run_card(last_run: LastRun | None, zone: ZoneInfo) -> None:
    with ui.card().classes("flex-1 min-w-72").mark("last-run-card"):
        ui.label(_("Last run")).classes("text-h6")
        if last_run is None:
            ui.label(_("No run yet.")).mark("no-run")
            return
        with ui.grid(columns="auto 1fr").classes("gap-x-4 gap-y-1 items-center"):
            with _field(_("Started")):
                _value(format_time(last_run.started_at, zone), "run-started")
            with _field(_("Status")):
                _badge(RUN_STATUSES[last_run.status], "run-status")
            with _field(_("Trigger")):
                _value(_(RUN_TRIGGERS[last_run.trigger]), "run-trigger")
            with _field(_("Videos added")):
                _value(str(last_run.added_count), "run-added")
            with _field(_("Videos seen")):
                _value(str(last_run.seen_count), "run-seen")
        if last_run.error_text:
            ui.label(_("Error")).classes("text-grey-7")
            ui.label(last_run.error_text).classes("whitespace-pre-wrap break-words font-mono text-negative").mark(
                "run-error"
            )


def _ytdlp_card(version: str | None, source: Source) -> None:
    with ui.card().classes("flex-1 min-w-72").mark("ytdlp-card"):
        ui.label(_("yt-dlp")).classes("text-h6")
        with ui.grid(columns="auto 1fr").classes("gap-x-4 gap-y-1 items-center"):
            with _field(_("Version")):
                _value(version or _("Unknown"), "ytdlp-version")
            with _field(_("Source")):
                _value(_(YTDLP_SOURCES[source]), "ytdlp-source")


@register_page("/", label=N_("Dashboard"), icon="dashboard")
async def dashboard_page(services: Services) -> None:
    @ui.refreshable
    def cards(snapshot: Snapshot) -> None:
        zone = ZoneInfo(snapshot.stored.timezone)
        with ui.row().classes("w-full gap-4 items-stretch"):
            _session_card(snapshot.stored.session, zone)
            _scheduler_card(snapshot.scheduler_state, snapshot.next_run_at, zone)
            _last_run_card(snapshot.stored.last_run, zone)
            _ytdlp_card(snapshot.ytdlp_version, snapshot.ytdlp_source)

    async def refresh() -> None:
        cards.refresh(await take_snapshot(services))

    ui.label(_("Dashboard")).classes("text-h5")
    cards(await take_snapshot(services))
    # The first read is above, so the timer starts with a pause.
    ui.timer(REFRESH_SECONDS, refresh, immediate=False)
