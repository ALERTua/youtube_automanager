"""The HTTP API of the service: the health check, the status, the run history, the session check, the refresh of
the account data, the rules, the schedule and the start of a run.

A blocking database call never runs on the event loop: a ``def`` route runs in the thread pool of FastAPI, and an
``async def`` route gives such a call to ``run_in_threadpool``. The routes of the rules and of the schedule use the
same functions and the same checks as the pages, and FastAPI shows each route on ``/docs``.
"""

import dataclasses
import functools
import logging
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, available_timezones

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager import notify
from youtube_automanager.account_cache import RefreshResult
from youtube_automanager.account_refresh import (
    RefreshAccountError,
    RefreshBusy,
    RefreshLoggedOut,
    RefreshProcessError,
    RefreshUnavailable,
    refresh_in_child,
)
from youtube_automanager.db.models import (
    Account,
    Run,
    RunAction,
    RunActionKind,
    RunStatus,
    RunTrigger,
    ScheduleKind,
    SessionState,
)
from youtube_automanager.rules_store import (
    MAX_DAYS_BACK,
    RuleDraft,
    RuleRecord,
    Stored,
    delete_rule,
    read_stored,
    save_rule,
    validate_draft,
)
from youtube_automanager.runner import BUSY_TEXT, RunAlreadyActive, SessionCheckOutcome
from youtube_automanager.scheduler import (
    RunNowOutcome,
    SchedulerState,
    ScheduleSpec,
    load_schedule,
    read_timezone,
    save_schedule,
    upcoming_run_times,
)
from youtube_automanager.web.services import Services, get_services
from youtube_automanager.youtube.ytdlp_updater import Source, active_version

LOG = logging.getLogger(__name__)

MAX_RUNS_PAGE = 500

router = APIRouter()

ServicesDep = Annotated[Services, Depends(get_services)]
# The id of a rule or of a run in a path. The database keeps a 64-bit integer, so a larger id gets 422, not a
# failure of the query.
MAX_ID = 2**63 - 1
IdPath = Annotated[int, Path(ge=1, le=MAX_ID)]


class HealthOut(BaseModel):
    status: Literal["ok", "error"]
    detail: str | None = None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    started_at: datetime
    finished_at: datetime | None
    status: RunStatus
    trigger: RunTrigger
    error_text: str | None
    added_count: int
    seen_count: int


class ErrorOut(BaseModel):
    detail: str


class StatusOut(BaseModel):
    scheduler_state: SchedulerState
    next_run_at: datetime | None
    last_run: RunOut | None
    # None when the account row does not exist yet.
    session_state: SessionState | None
    ytdlp_version: str | None
    # "data_folder" for a release that the service installed, "image" for the copy that came with the service.
    ytdlp_source: Source


class SessionCheckOut(BaseModel):
    outcome: SessionCheckOutcome
    # None when the account row does not exist yet.
    session_state: SessionState | None
    last_checked_at: datetime | None
    earliest_cookie_expiry: datetime | None
    # The cause when the check found no usable cookies, failed or did not start.
    detail: str | None


@router.get("/health", response_model=HealthOut, responses={503: {"model": HealthOut}})
def health(services: ServicesDep):
    try:
        with services.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as error:
        # The text of the error can name the database server, so only its type goes into the log.
        LOG.warning("The health check cannot reach the database: %s", type(error).__name__)
        return JSONResponse(
            status_code=503, content=HealthOut(status="error", detail="The database does not answer").model_dump()
        )
    return HealthOut(status="ok")


def _read_status_rows(sessions: sessionmaker[Session]) -> tuple[RunOut | None, SessionState | None]:
    with sessions() as session:
        last_run = session.scalars(select(Run).order_by(Run.started_at.desc(), Run.id.desc()).limit(1)).first()
        account = session.scalars(select(Account).order_by(Account.id).limit(1)).first()
        return (
            RunOut.model_validate(last_run) if last_run else None,
            account.session_state if account else None,
        )


@router.get("/api/status", response_model=StatusOut)
async def status(services: ServicesDep) -> StatusOut:
    next_run_times = await services.scheduler.next_run_times(1)
    last_run, session_state = await run_in_threadpool(_read_status_rows, services.sessions)
    ytdlp_version, ytdlp_source = await run_in_threadpool(active_version, services.settings.ytdlp_dir)
    return StatusOut(
        scheduler_state=await services.scheduler.state(),
        next_run_at=next_run_times[0] if next_run_times else None,
        last_run=last_run,
        session_state=session_state,
        ytdlp_version=ytdlp_version,
        ytdlp_source=ytdlp_source,
    )


@router.get("/api/runs", response_model=list[RunOut])
def runs(
    services: ServicesDep,
    limit: Annotated[int, Query(ge=1, le=MAX_RUNS_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_ID)] = 0,
) -> list[RunOut]:
    """Return the runs, newest first."""
    with services.sessions() as session:
        rows = session.scalars(
            select(Run).order_by(Run.started_at.desc(), Run.id.desc()).limit(limit).offset(offset)
        ).all()
        return [RunOut.model_validate(row) for row in rows]


@router.post("/api/session/check", response_model=SessionCheckOut, responses={409: {"model": SessionCheckOut}})
async def check_session(services: ServicesDep):
    """Check the session of the stored cookies now. The answer is 409 while a run holds the sync lock."""
    result = await run_in_threadpool(services.runner.check_session)
    answer = SessionCheckOut(**dataclasses.asdict(result))
    if result.outcome is SessionCheckOutcome.BUSY:
        return JSONResponse(status_code=409, content=answer.model_dump(mode="json"))
    return answer


@router.post(
    "/api/account/refresh",
    response_model=RefreshResult,
    responses={409: {"model": ErrorOut}, 502: {"model": ErrorOut}},
)
def refresh_account(services: ServicesDep) -> RefreshResult:
    """Read the subscriptions and the playlists of the account again, and store them as the account data.

    The refresh runs in a child process of the runner, like a session check, so this process never imports yt-dlp. The
    route waits for the child in a thread of the pool. The child holds the sync lock, so no two requests with the
    cookies overlap, and a stop of the service stops the child.
    The answer is 409 when the sync lock is taken, when the account is not available, or when YouTube does not accept
    the stored cookies. It is 502 when YouTube does not give the data or the child fails. No answer or log line holds a
    cookie value.
    """
    try:
        return refresh_in_child(services.runner)
    except RefreshBusy as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RefreshUnavailable as error:
        LOG.warning("The account data cannot refresh, because the account is not available: %s", error)
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RefreshLoggedOut as error:
        LOG.warning("The account data cannot refresh, because YouTube answered as logged out: %s", error)
        raise HTTPException(
            status_code=409,
            detail=(
                f"YouTube does not accept the stored cookies any more ({error}). "
                "Export new YouTube cookies from your browser and upload them to the service."
            ),
        ) from error
    except RefreshAccountError as error:
        LOG.warning("The account data cannot refresh, because YouTube gave no data: %s", error)
        raise HTTPException(status_code=502, detail=f"YouTube did not give the account data: {error}") from error
    except RefreshProcessError as error:
        LOG.warning("The account data cannot refresh, because the refresh process failed: %s", error)
        raise HTTPException(status_code=502, detail=str(error)) from error


# The rules.

NO_RULE_TEXT = "No rule has this id."


class RuleIn(BaseModel):
    """A rule to create, or the new version of a rule. A PUT replaces the whole rule, so each field that the body leaves
    out gets its default."""

    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = True
    # The `UC...` ids of the channels of the rule. A rule without channels reads the subscriptions feed of the account.
    channel_ids: list[str] = Field(default_factory=list)
    # The id of the target playlist, from the stored playlists of the account. Watch Later is `WL`.
    playlist_id: str
    no_shorts: bool = False
    # The condition as a JsonLogic tree. {"==": [1, 1]} matches each video.
    condition: Any
    # The next run also adds the matching videos since this day, at most MAX_DAYS_BACK days back from today in the
    # time zone of the schedule. None means no such videos.
    catch_up_since: date | None = None


class RuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    enabled: bool
    # In the order of the channel titles.
    channel_ids: list[str]
    playlist_id: str
    no_shorts: bool
    condition: Any
    # None after the next run added the older videos, or when the rule never had such a day.
    catch_up_since: date | None
    updated_at: datetime


RULE_ERRORS: dict[int | str, dict[str, Any]] = {404: {"model": ErrorOut}}
RULE_SAVE_ERRORS: dict[int | str, dict[str, Any]] = {422: {"model": ErrorOut}}


def _read_stored(services: Services) -> Stored:
    return read_stored(services.sessions, services.settings.tz)


def _stored_rule(stored: Stored, rule_id: int) -> RuleRecord:
    rule = stored.rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=NO_RULE_TEXT)
    return rule


def _checked_draft(body: RuleIn, stored: Stored) -> RuleDraft:
    """Return the draft of ``body``. Raise HTTPException 422 with the text of the first check that fails.

    ``validate_draft`` checks first, in the order of the rules page: the name, the target playlist, the condition. Then
    come the day of the older videos, and the playlist and the channels in the stored copy of the account.
    """
    draft = RuleDraft(
        name=body.name.strip(),
        enabled=body.enabled,
        channel_ids=tuple(body.channel_ids),
        playlist_id=body.playlist_id,
        no_shorts=body.no_shorts,
        condition=body.condition,
        catch_up_since=body.catch_up_since,
    )
    try:
        validate_draft(draft)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if draft.catch_up_since is not None:
        today = datetime.now(ZoneInfo(stored.timezone)).date()
        earliest = today - timedelta(days=MAX_DAYS_BACK)
        if not earliest <= draft.catch_up_since <= today:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"catch_up_since must be a day from {earliest.isoformat()} to {today.isoformat()}: at most "
                    f"{MAX_DAYS_BACK} days back and not in the future, in the time zone {stored.timezone}."
                ),
            )
    if draft.playlist_id not in stored.playlists:
        raise HTTPException(
            status_code=422,
            detail=(
                f"The playlist {draft.playlist_id} is not in the stored playlists of the account. "
                "Refresh the account data, and use the id of a stored playlist."
            ),
        )
    unknown = sorted(set(draft.channel_ids) - stored.channels.keys())
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"The channels {', '.join(unknown)} are not in the stored channels of the account. "
                "Refresh the account data, and use the ids of stored channels."
            ),
        )
    return draft


def _save(services: Services, rule_id: int | None, body: RuleIn) -> RuleOut:
    stored = _read_stored(services)
    if rule_id is not None:
        _stored_rule(stored, rule_id)
    saved_id = save_rule(services.sessions, rule_id, _checked_draft(body, stored))
    if saved_id is None:
        # Another request deleted the rule after the read.
        raise HTTPException(status_code=404, detail=NO_RULE_TEXT)
    return RuleOut.model_validate(_stored_rule(_read_stored(services), saved_id))


@router.get("/api/rules", response_model=list[RuleOut])
def list_rules(services: ServicesDep) -> list[RuleOut]:
    """Return the rules in the order of their evaluation."""
    return [RuleOut.model_validate(rule) for rule in _read_stored(services).rules]


@router.get("/api/rules/{rule_id}", response_model=RuleOut, responses=RULE_ERRORS)
def get_rule(rule_id: IdPath, services: ServicesDep) -> RuleOut:
    return RuleOut.model_validate(_stored_rule(_read_stored(services), rule_id))


@router.post("/api/rules", status_code=201, response_model=RuleOut, responses=RULE_SAVE_ERRORS)
def create_rule(body: RuleIn, services: ServicesDep) -> RuleOut:
    """Create a rule at the end of the evaluation order.

    The answer is 422 with the reason when the rule cannot be saved: an empty name, no target playlist, a condition that
    is no valid rule tree, a day of the older videos out of its range, or a playlist or a channel that the stored copy
    of the account does not have.
    """
    return _save(services, None, body)


@router.put("/api/rules/{rule_id}", response_model=RuleOut, responses=RULE_ERRORS | RULE_SAVE_ERRORS)
def replace_rule(rule_id: IdPath, body: RuleIn, services: ServicesDep) -> RuleOut:
    """Replace the rule with the body. The rule keeps its place in the evaluation order.

    The answer is 404 for an unknown id, and 422 with the reason when the rule cannot be saved, as for a POST.
    """
    return _save(services, rule_id, body)


@router.delete("/api/rules/{rule_id}", status_code=204, responses=RULE_ERRORS | {409: {"model": ErrorOut}})
def remove_rule(rule_id: IdPath, services: ServicesDep) -> None:
    """Delete the rule. The history keeps its actions.

    The answer is 409 while a run, a session check or another task with YouTube holds the sync lock, because a run
    writes the id of the rule with each video that it adds.
    """
    _stored_rule(_read_stored(services), rule_id)
    try:
        delete_rule(services.sessions, services.runner, rule_id)
    except RunAlreadyActive as error:
        raise HTTPException(status_code=409, detail=BUSY_TEXT) from error


# The schedule.


class ScheduleIn(BaseModel):
    """The schedule of the sync runs. A PUT replaces the whole schedule."""

    model_config = ConfigDict(extra="forbid")

    kind: ScheduleKind
    # An IANA time zone, for example Europe/Kyiv. Each run time and each day of the rules follows it.
    timezone: str
    # Five fields of standard cron, for a cron schedule. A cron schedule needs one.
    cron_expression: str | None = None
    # The seconds between two runs, for an interval schedule. An interval schedule needs one.
    interval_s: int | None = None
    # False pauses the schedule. A run can still start with POST /api/runs.
    enabled: bool = True


class ScheduleOut(ScheduleIn):
    model_config = ConfigDict(from_attributes=True)


@functools.cache
def _known_timezones() -> frozenset[str]:
    """Return the names of the time zones that the scheduler page offers. The first call scans files and blocks."""
    return frozenset(available_timezones())


def _is_valid(kind: ScheduleKind, timezone: str, cron: str | None, interval: int | None) -> bool:
    try:
        ScheduleSpec(kind=kind, timezone=timezone, cron_expression=cron, interval_s=interval)
    except ValueError:
        return False
    return True


def _checked_schedule(body: ScheduleIn, services: Services) -> ScheduleSpec:
    """Return the schedule of ``body``. Raise ValueError with the reason when it cannot be saved. This call blocks.

    The time zone is a known one, or the one that the schedule has now, as on the scheduler page. The field that the
    kind does not use goes into the row only when it is valid on its own, as on the scheduler page too. So an old row,
    for example a cron schedule with an interval below MIN_INTERVAL_S, comes back from a GET and a PUT without an
    error, and the row never holds a wrong text.
    """
    if body.timezone not in _known_timezones() and body.timezone != read_timezone(
        services.sessions, services.settings.tz
    ):
        raise ValueError(f"Unknown time zone {body.timezone!r}. Use an IANA name, for example Europe/Kyiv.")
    cron = (body.cron_expression or "").strip() or None
    interval = body.interval_s
    if body.kind is ScheduleKind.CRON:
        interval = interval if _is_valid(ScheduleKind.INTERVAL, body.timezone, cron, interval) else None
    else:
        cron = cron if _is_valid(ScheduleKind.CRON, body.timezone, cron, interval) else None
    spec = ScheduleSpec(
        kind=body.kind, timezone=body.timezone, cron_expression=cron, interval_s=interval, enabled=body.enabled
    )
    # A paused schedule has no run times, but it must have them after a resume.
    if not upcoming_run_times(dataclasses.replace(spec, enabled=True), 1):
        raise ValueError("The schedule has no run time.")
    return spec


@router.get("/api/schedule", response_model=ScheduleOut)
def get_schedule(services: ServicesDep) -> ScheduleOut:
    return ScheduleOut.model_validate(load_schedule(services.sessions, services.settings.tz))


@router.put("/api/schedule", response_model=ScheduleOut, responses={422: {"model": ErrorOut}})
async def put_schedule(body: ScheduleIn, services: ServicesDep, background: BackgroundTasks) -> ScheduleOut:
    """Save the schedule, and let the running scheduler follow it at once.

    The answer is 422 with the reason for a wrong schedule, and for a schedule that never runs, for example the cron
    expression ``0 0 31 2 *``. A save that pauses or resumes the schedule tells the notification targets, as the
    scheduler page does.
    """
    try:
        # The search for a run time of a rare cron expression takes up to a few hundred milliseconds.
        spec = await run_in_threadpool(_checked_schedule, body, services)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    before = await services.scheduler.state()
    await run_in_threadpool(save_schedule, services.sessions, spec)
    await services.scheduler.apply(spec)
    if before is not SchedulerState.STOPPED and (before is SchedulerState.RUNNING) != spec.enabled:
        # Apprise blocks, so the send runs in the thread pool after the answer.
        background.add_task(notify.send_to_stored_targets, services.sessions, notify.scheduler_state(not spec.enabled))
    return ScheduleOut.model_validate(spec)


# The start of a run and the actions of a run.


class RunStartOut(BaseModel):
    run_id: int


class ActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    at: datetime
    action: RunActionKind
    video_id: str | None
    channel_id: str | None
    rule_id: int | None
    playlist_id: str | None
    detail: str | None


class RunDetailOut(RunOut):
    # In the order of their time.
    actions: list[ActionOut]


@router.post("/api/runs", status_code=202, response_model=RunStartOut, responses={409: {"model": ErrorOut}})
async def start_run(services: ServicesDep) -> RunStartOut:
    """Start a sync run now, also while the schedule is paused, as the Run now button of the scheduler page does.

    The answer comes at the start of the run and holds its id: GET /api/runs/{run_id} shows its state. The answer is
    409 while a run, a session check or another task with YouTube holds the sync lock.
    """
    result = await services.scheduler.run_now()
    if result.outcome is RunNowOutcome.ALREADY_ACTIVE or result.run_id is None:
        raise HTTPException(status_code=409, detail=BUSY_TEXT)
    return RunStartOut(run_id=result.run_id)


@router.get("/api/runs/{run_id}", response_model=RunDetailOut, responses={404: {"model": ErrorOut}})
def get_run(
    run_id: IdPath,
    services: ServicesDep,
    action: Annotated[RunActionKind | None, Query(description="Only the actions of this kind.")] = None,
) -> RunDetailOut:
    """Return the run with its actions."""
    with services.sessions() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="No run has this id.")
        query = select(RunAction).where(RunAction.run_id == run_id).order_by(RunAction.at, RunAction.id)
        if action is not None:
            query = query.where(RunAction.action == action)
        actions = [ActionOut.model_validate(row) for row in session.scalars(query)]
        return RunDetailOut(**RunOut.model_validate(run).model_dump(), actions=actions)


# The pages answer each path that no route takes, with HTML. An API client gets the JSON 404 of FastAPI instead.
# This route comes last, so each API route above it matches first.
@router.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
def unknown_api_path(path: str) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Not Found"})
