"""The history page: the runs of the sync, the actions of one run, and the retention of the history.

The table of the runs reads one page of ``PAGE_SIZE`` runs from the database at a time, newest first. A click on a
run opens its actions below the table. The page reads and writes the database in a thread, never on the event loop.
Each date shows in the time zone of the schedule, and a date filter counts whole days of that time zone.

The page follows each run with no click. At the start and at the end of a run, it reads its page of runs again with its
filters, so a new run shows at the top only when the filters and the page let it through. While a run runs, its row
shows the new counts, and its open actions get the new actions.
"""

import asyncio
import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from nicegui import context, ui
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import Run, RunAction, RunActionKind, RunStatus, RunTrigger
from youtube_automanager.i18n import N_, _
from youtube_automanager.retention import (
    MIN_RETENTION_DAYS,
    get_retention_days,
    runs_lost_by_retention,
    set_retention_days,
)
from youtube_automanager.run_events import ActionItem, action_item
from youtube_automanager.scheduler import CLEANUP_HOUR, CLEANUP_MINUTE
from youtube_automanager.web.services import Services
from youtube_automanager.web.ui.common import (
    FOCUS_DELAY_MS,
    RUN_STATUSES,
    RUN_TRIGGERS,
    actions_column,
    add_row_button,
    busy,
    column,
    confirm,
    focus_when_drawn,
    format_time,
    icon_button,
    row_button_selector,
    stored_timezone,
)
from youtube_automanager.web.ui.dates import today_button
from youtube_automanager.web.ui.layout import register_page
from youtube_automanager.web.ui.live_runs import RunFollower

PAGE_SIZE = 50
# The card of the actions of a run takes the focus when a run opens. This attribute marks it for the browser.
DETAILS_ATTRIBUTE = "data-run-details"
DETAILS_SELECTOR = f"[{DETAILS_ATTRIBUTE}]"
# The select of the kind of action in that card. Quasar puts the attribute on the field, and the focus goes to its input.
ACTION_FILTER_ATTRIBUTE = "data-action-filter"
ACTION_FILTER_SELECTOR = f"[{ACTION_FILTER_ATTRIBUTE}] input"
# The value of a filter that lets each run through.
ANY = "any"
WATCH_URL = "https://www.youtube.com/watch?v="
# A typed date is moved into this range. A date outside it is no day of a run, and the time zone conversion of a date
# near the limits of ``datetime`` fails.
EARLIEST_DAY = date(1900, 1, 1)
LATEST_DAY = date(9000, 1, 1)

ACTION_KINDS = {
    RunActionKind.ADDED: N_("Added"),
    RunActionKind.SKIPPED: N_("Skipped"),
    RunActionKind.DEFERRED: N_("Deferred"),
    RunActionKind.ERROR: N_("Error"),
}
STATUS_LABELS = {status: label for status, (label, _color) in RUN_STATUSES.items()}

# The cells that need more than the text of the value. A row holds the display text of each cell, ready to show.
STATUS_CELL = """
<q-td :props="props">
  <q-badge :color="props.row.status_color">{{ props.value }}</q-badge>
</q-td>
"""
# One line with the start of the text, and the whole text as the tooltip of the cell.
ERROR_CELL = """
<q-td :props="props" class="text-negative">
  <div class="ellipsis" style="max-width: 24rem" :title="props.value">{{ props.value }}</div>
</q-td>
"""
VIDEO_CELL = """
<q-td :props="props">
  <a v-if="props.row.video_url" :href="props.row.video_url" target="_blank" rel="noopener noreferrer">{{ props.value }}</a>
</q-td>
"""
DETAIL_CELL = """
<q-td :props="props" style="white-space: normal; word-break: break-word; max-width: 30rem">{{ props.value }}</q-td>
"""


@dataclass(frozen=True)
class RunFilter:
    status: RunStatus | None = None
    trigger: RunTrigger | None = None
    # The first and the last day of the range, in the time zone of the schedule. Both days belong to the range.
    first_day: date | None = None
    last_day: date | None = None

    def conditions(self, zone: ZoneInfo) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = []
        if self.status is not None:
            conditions.append(Run.status == self.status)
        if self.trigger is not None:
            conditions.append(Run.trigger == self.trigger)
        if self.first_day is not None:
            conditions.append(Run.started_at >= datetime.combine(self.first_day, time.min, tzinfo=zone))
        if self.last_day is not None:
            end = datetime.combine(self.last_day + timedelta(days=1), time.min, tzinfo=zone)
            conditions.append(Run.started_at < end)
        return conditions


@dataclass(frozen=True)
class RunRecord:
    id: int
    started_at: datetime
    finished_at: datetime | None
    status: RunStatus
    trigger: RunTrigger
    added_count: int
    seen_count: int
    error_text: str | None


@dataclass(frozen=True)
class RunList:
    """One page of the runs that match a filter."""

    runs: list[RunRecord]
    # The number of all runs that match the filter.
    total: int
    # The number of this page, counted from 1. It can differ from the requested one, when the filter has fewer pages.
    page: int
    pages: int
    timezone: str
    # True when the read had a filter. A list without runs then says that no run matches, and not that no run exists.
    filtered: bool


@dataclass(frozen=True)
class RunDetail:
    run: RunRecord
    actions: list[ActionItem]
    kind: RunActionKind | None
    timezone: str


@dataclass
class PageState:
    page: int = 1
    run_id: int | None = None
    action_kind: RunActionKind | None = None
    # The number of the newest read. A read that finishes after a newer one started drops its result.
    runs_read: int = 0
    detail_read: int = 0
    # What the page shows: the open run, the table of the runs and the table of the open actions. A progress of a run
    # changes them in place.
    detail: RunDetail | None = None
    runs_table: ui.table | None = None
    actions_table: ui.table | None = None
    # The actions that the progress events of the open run bring while a read of its actions runs, or None when no such
    # read runs. The read can end before their commit, and the poll gives each action only once.
    arrived: list[ActionItem] | None = None


def add_actions(detail: RunDetail, actions: Iterable[ActionItem]) -> tuple[RunDetail, list[ActionItem]]:
    """Return ``detail`` with the ``actions`` that it does not hold yet and that its action kind lets through, and these
    new actions."""
    known = {action.id for action in detail.actions}
    new = []
    for action in actions:
        if action.id not in known and (detail.kind is None or action.action is detail.kind):
            known.add(action.id)
            new.append(action)
    return replace(detail, actions=[*detail.actions, *new]), new


def parse_day(text: str | None) -> date | None:
    """Return the date of ``text`` in the form 2026-10-02, moved into the range of the filter.

    Return None for an empty text. Raise ValueError for a text that is not such a date.
    """
    if not text or not text.strip():
        return None
    day = date.fromisoformat(text.strip())
    return min(max(day, EARLIEST_DAY), LATEST_DAY)


def read_day(field: ui.input) -> date | None:
    """Return the date of a date field of the filter. Mark a field with a wrong date, and keep its filter off."""
    try:
        day = parse_day(field.value)
    except ValueError:
        field.error = _("This is not a date, so the filter does not use it.")
        return None
    field.error = None
    return day


def _run_record(run: Run) -> RunRecord:
    return RunRecord(
        run.id,
        run.started_at,
        run.finished_at,
        run.status,
        run.trigger,
        run.added_count,
        run.seen_count,
        run.error_text,
    )


def read_runs(sessions: sessionmaker[Session], run_filter: RunFilter, page: int, default_timezone: str) -> RunList:
    """Read one page of the runs that match ``run_filter``, newest first. This call blocks."""
    with sessions() as session:
        timezone = stored_timezone(session, default_timezone)
        conditions = run_filter.conditions(ZoneInfo(timezone))
        total = session.scalar(select(func.count()).select_from(Run).where(*conditions)) or 0
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(max(page, 1), pages)
        runs = session.scalars(
            select(Run)
            .where(*conditions)
            .order_by(Run.started_at.desc(), Run.id.desc())
            .limit(PAGE_SIZE)
            .offset((page - 1) * PAGE_SIZE)
        ).all()
        return RunList([_run_record(run) for run in runs], total, page, pages, timezone, bool(conditions))


def read_run_detail(
    sessions: sessionmaker[Session], run_id: int, kind: RunActionKind | None, default_timezone: str
) -> RunDetail | None:
    """Read a run and its actions in the order of their time, only of the action kind ``kind`` when it is given.

    Return None when the run does not exist. This call blocks.
    """
    with sessions() as session:
        run = session.get(Run, run_id)
        if run is None:
            return None
        query = select(RunAction).where(RunAction.run_id == run_id).order_by(RunAction.at, RunAction.id)
        if kind is not None:
            query = query.where(RunAction.action == kind)
        actions = [action_item(action) for action in session.scalars(query)]
        return RunDetail(_run_record(run), actions, kind, stored_timezone(session, default_timezone))


def run_row(run: RunRecord, zone: ZoneInfo) -> dict:
    status_label, status_color = RUN_STATUSES[run.status]
    return {
        "id": run.id,
        "started": format_time(run.started_at, zone),
        "finished": format_time(run.finished_at, zone) if run.finished_at else "",
        "status": _(status_label),
        "status_color": status_color,
        "trigger": _(RUN_TRIGGERS[run.trigger]),
        "added": run.added_count,
        "seen": run.seen_count,
        "error": run.error_text or "",
    }


def action_row(action: ActionItem, zone: ZoneInfo) -> dict:
    return {
        "id": action.id,
        "at": format_time(action.at, zone),
        "action": _(ACTION_KINDS[action.action]),
        "video": action.video_id or "",
        "video_url": f"{WATCH_URL}{quote(action.video_id, safe='')}" if action.video_id else "",
        "channel": action.channel_id or "",
        "rule": "" if action.rule_id is None else action.rule_id,
        "playlist": action.playlist_id or "",
        "detail": action.detail or "",
    }


def run_columns() -> list[dict]:
    return [
        column("id", _("Run")),
        column("started", _("Started")),
        column("finished", _("Finished")),
        column("status", _("Status")),
        column("trigger", _("Trigger")),
        column("added", _("Videos added")),
        column("seen", _("Videos seen")),
        column("error", _("Error")),
        actions_column(),
    ]


def action_columns() -> list[dict]:
    return [
        column("at", _("Time")),
        column("action", _("Action")),
        column("video", _("Video")),
        column("channel", _("Channel")),
        column("rule", _("Rule")),
        column("playlist", _("Playlist")),
        column("detail", _("Detail")),
    ]


def _whole_days(value: float | None) -> int:
    """Return the number of days of the retention field. Raise ValueError for no number, a fraction or a number below 1."""
    if value is None or not float(value).is_integer() or value < MIN_RETENTION_DAYS:
        raise ValueError(_("Enter a whole number of days, 1 or more."))
    return int(value)


def _filter_options(labels: dict) -> dict:
    return {ANY: _("All"), **{member.value: _(label) for member, label in labels.items()}}


@register_page("/history", label=N_("History"), icon="history")
async def history_page(services: Services) -> None:
    state = PageState()
    # The client of the page. A handler can run after the element that started it is gone, so it needs the client itself.
    client = context.client

    def read_filter() -> RunFilter:
        return RunFilter(
            status=None if status_select.value == ANY else RunStatus(status_select.value),
            trigger=None if trigger_select.value == ANY else RunTrigger(trigger_select.value),
            first_day=read_day(first_input),
            last_day=read_day(last_input),
        )

    async def load_runs(page: int | None = None) -> None:
        if page is not None:
            state.page = page
        state.runs_read += 1
        read = state.runs_read
        result = await asyncio.to_thread(read_runs, services.sessions, read_filter(), state.page, services.settings.tz)
        if read != state.runs_read:
            return
        state.page = result.page
        await runs_view.refresh(result)

    async def show_run(run_id: int | None, kind: RunActionKind | None = None) -> None:
        state.run_id = run_id
        state.action_kind = kind
        state.detail_read += 1
        read = state.detail_read
        detail = None
        state.arrived = None if run_id is None else []
        if run_id is not None:
            detail = await asyncio.to_thread(read_run_detail, services.sessions, run_id, kind, services.settings.tz)
        if read != state.detail_read:
            return
        arrived, state.arrived = state.arrived or [], None
        if detail is None:
            # The run is gone, for example after a cleanup of the history.
            state.run_id = None
        else:
            detail, _new = add_actions(detail, arrived)
        state.detail = detail
        # Awaited, so the panel shows this read before the handler of the next progress runs.
        await details.refresh()

    async def refresh() -> None:
        await load_runs()
        if state.run_id is not None:
            await show_run(state.run_id, state.action_kind)

    async def reload_for_run(run_id: int, *_trigger_or_status: object) -> None:
        # The read again with the filters and the page decides whether the run shows, and the row of the database holds
        # the status that the run really has.
        await load_runs()
        if state.run_id == run_id:
            await show_run(run_id, state.action_kind)

    async def show_progress(run_id: int, added: int, seen: int, actions: tuple[ActionItem, ...]) -> None:
        # Each change comes before the first await, so no read of the end of the run can come between. See RunFollower.
        table = state.runs_table
        if table is not None and not table.is_deleted:
            for index, row in enumerate(table.rows):
                if row["id"] == run_id:
                    # The rows are observed props of NiceGUI, so the new row goes to the browser. An event that is older
                    # than the read of the row does not lower its counts.
                    table.rows[index] = {**row, "added": max(row["added"], added), "seen": max(row["seen"], seen)}
                    break
        if state.arrived is not None and state.run_id == run_id:
            state.arrived.extend(actions)
        detail = state.detail
        if detail is None or detail.run.id != run_id:
            return
        # A read of the actions that ended after the poll can hold some of these actions already.
        state.detail, new = add_actions(detail, actions)
        if not new:
            return
        actions_table = state.actions_table
        if actions_table is not None and not actions_table.is_deleted:
            actions_table.add_rows([action_row(action, ZoneInfo(detail.timezone)) for action in new])
        else:
            # The panel said that the run has no actions, so it needs its table now.
            await details.refresh()

    def live(run: RunRecord) -> RunRecord:
        """Return ``run`` with the counts of its last progress, when it runs and its read is older."""
        if run.status is not RunStatus.RUNNING:
            return run
        added, seen = follower.counts(run.id, run.added_count, run.seen_count)
        return replace(run, added_count=added, seen_count=seen)

    async def filters_changed() -> None:
        await load_runs(page=1)

    async def open_run(event) -> None:
        await show_run(int(event.args))
        # The table of the runs was drawn again, so the button that had the focus is gone. The focus goes to the card.
        focus_when_drawn(client, DETAILS_SELECTOR)

    async def close_run() -> None:
        run_id = state.run_id
        await show_run(None)
        # The close button is gone with the card, so the focus goes back to the button of the same run.
        # The run can be on another page of the table, or a cleanup can have deleted it. Then Refresh gets the focus.
        if run_id is not None:
            focus_when_drawn(
                client, row_button_selector(run_id), fallback=f"#{refresh_button.html_id}", delay_ms=FOCUS_DELAY_MS
            )

    async def change_action_kind(run_id: int, value: str) -> None:
        await show_run(run_id, None if value == ANY else RunActionKind(value))
        # The card was built again, so the select that had the focus is gone. The card is the second choice.
        focus_when_drawn(client, ACTION_FILTER_SELECTOR, fallback=DETAILS_SELECTOR)

    async def save_retention() -> None:
        try:
            days = _whole_days(days_input.value)
        except ValueError as error:
            retention_error.set_text(str(error))
            retention_error.set_visibility(True)
            return
        retention_error.set_visibility(False)
        # The button stays off while the question is open, so a second click opens no second question.
        with busy(save_button):
            lost = await asyncio.to_thread(runs_lost_by_retention, services.sessions, days)
            # A smaller value makes the next cleanup delete runs, so the person sees how many before the save.
            if lost:
                confirmed = await confirm(
                    _("Keep the history for {days} days?").format(days=days),
                    _("Save"),
                    marker="retention",
                    detail=_(
                        "Finished runs that started more than {days} days ago: {count}. The next cleanup deletes them. "
                        "It runs at the start of the service and each day at {time} ({zone})."
                    ).format(
                        days=days,
                        count=lost,
                        time=f"{CLEANUP_HOUR:02d}:{CLEANUP_MINUTE:02d}",
                        zone=first_page.timezone,
                    ),
                    color="negative",
                    classes="max-w-lg",
                )
                if not confirmed:
                    return
            await asyncio.to_thread(set_retention_days, services.sessions, days)
        ui.notify(_("The retention is saved."), type="positive")

    @ui.refreshable
    def runs_view(result: RunList) -> None:
        zone = ZoneInfo(result.timezone)
        state.runs_table = None
        if not result.runs:
            ui.label(_("No run matches the filters.") if result.filtered else _("No run yet.")).mark("no-runs")
            return
        first = (result.page - 1) * PAGE_SIZE + 1
        last = first + len(result.runs) - 1
        ui.label(_("Runs {first}-{last} of {total}").format(first=first, last=last, total=result.total)).mark(
            "runs-summary"
        )
        table = (
            ui.table(columns=run_columns(), rows=[run_row(live(run), zone) for run in result.runs], row_key="id")
            .props("flat bordered dense")
            .classes("w-full cursor-pointer")
            .mark("runs-table")
        )
        state.runs_table = table
        table.add_slot("body-cell-status", STATUS_CELL)
        table.add_slot("body-cell-error", ERROR_CELL)
        # The run id is the only part of the click that the page needs.
        table.on("rowClick", open_run, js_handler="(event, row) => emit(row.id)")
        add_row_button(table, open_run, icon="open_in_new", label=_("Open"))
        if result.pages > 1:
            ui.pagination(
                1,
                result.pages,
                direction_links=True,
                value=result.page,
                on_change=lambda event: load_runs(page=event.value),
            ).props("max-pages=7 boundary-numbers").mark("runs-pagination")

    @ui.refreshable
    def details() -> None:
        state.actions_table = None
        detail = state.detail
        if detail is None:
            return
        zone = ZoneInfo(detail.timezone)
        run = detail.run
        status_label, status_color = RUN_STATUSES[run.status]
        with ui.card().props(f"tabindex=-1 {DETAILS_ATTRIBUTE}").classes("w-full outline-none").mark("details"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label(_("Actions of run {id}").format(id=run.id)).classes("text-h6").mark("details-title")
                icon_button("close", _("Close"), close_run).mark("details-close")
            with ui.row().classes("items-center gap-2"):
                ui.label(format_time(run.started_at, zone)).mark("details-started")
                ui.badge(_(status_label), color=status_color).mark("details-status")
            if run.error_text:
                ui.label(_("Error")).classes("text-grey-7")
                ui.label(run.error_text).classes("whitespace-pre-wrap break-words font-mono text-negative").mark(
                    "run-error"
                )
            ui.select(
                _filter_options(ACTION_KINDS),
                label=_("Action"),
                value=ANY if detail.kind is None else detail.kind.value,
                on_change=lambda event: change_action_kind(run.id, event.value),
            ).props(f"outlined dense {ACTION_FILTER_ATTRIBUTE}").classes("w-48").mark("filter-action")
            if not detail.actions:
                ui.label(_("This run has no actions.")).mark("no-actions")
                return
            table = (
                ui.table(
                    columns=action_columns(),
                    rows=[action_row(action, zone) for action in detail.actions],
                    row_key="id",
                    pagination=PAGE_SIZE if len(detail.actions) > PAGE_SIZE else None,
                )
                .props("flat bordered dense")
                .classes("w-full")
                .mark("actions-table")
            )
            table.add_slot("body-cell-video", VIDEO_CELL)
            table.add_slot("body-cell-detail", DETAIL_CELL)
            state.actions_table = table

    # The follower comes before the first read, so a run that starts during the read is not missed.
    follower = RunFollower(services.run_events, started=reload_for_run, progress=show_progress, finished=reload_for_run)
    retention_days = await asyncio.to_thread(get_retention_days, services.sessions)
    first_page = await asyncio.to_thread(read_runs, services.sessions, RunFilter(), 1, services.settings.tz)

    ui.label(_("History")).classes("text-h5")
    with ui.row().classes("w-full items-center gap-4"):
        status_select = (
            ui.select(_filter_options(STATUS_LABELS), label=_("Status"), value=ANY, on_change=filters_changed)
            .props("outlined dense")
            .classes("w-40")
            .mark("filter-status")
        )
        trigger_select = (
            ui.select(_filter_options(RUN_TRIGGERS), label=_("Trigger"), value=ANY, on_change=filters_changed)
            .props("outlined dense")
            .classes("w-40")
            .mark("filter-trigger")
        )
        # The page marks a wrong date itself, in ``read_day``. The automatic validation would clear that mark at each
        # change of the value, after the change handlers.
        first_input = (
            ui.input(_("From"), on_change=filters_changed)
            .without_auto_validation()
            .props("type=date outlined dense clearable")
            .mark("filter-from")
        )
        today_button(first_input, first_page.timezone).mark("filter-from-today")
        last_input = (
            ui.input(_("To"), on_change=filters_changed)
            .without_auto_validation()
            .props("type=date outlined dense clearable")
            .mark("filter-to")
        )
        today_button(last_input, first_page.timezone).mark("filter-to-today")
        refresh_button = ui.button(_("Refresh"), icon="refresh", on_click=refresh).mark("refresh")
    runs_view(first_page)
    details()

    with ui.card().classes("w-full"):
        ui.label(_("Retention")).classes("text-h6")
        with ui.row().classes("items-center gap-4"):
            days_input = (
                ui.number(
                    _("Days of history to keep"),
                    value=retention_days,
                    min=MIN_RETENTION_DAYS,
                    step=1,
                    precision=0,
                )
                .props("outlined dense")
                .classes("w-56")
                .mark("retention-days")
            )
            save_button = ui.button(_("Save"), icon="save", on_click=save_retention).mark("retention-save")
        retention_error = ui.label().classes("text-negative").mark("retention-error")
        retention_error.set_visibility(False)
        ui.label(
            _("The service deletes older runs with their actions at its start and each day at {time} ({zone}).").format(
                time=f"{CLEANUP_HOUR:02d}:{CLEANUP_MINUTE:02d}", zone=first_page.timezone
            )
        ).classes("text-grey-7").mark("retention-hint")
    if follower.start():
        await refresh()
