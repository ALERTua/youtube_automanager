"""The HTTP API of the service: the health check, the status and the run history.

A blocking database call never runs on the event loop: a ``def`` route runs in the thread pool of FastAPI, and an
``async def`` route gives such a call to ``run_in_threadpool``.
"""

import importlib.metadata
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import Account, Run, RunStatus, RunTrigger, SessionState
from youtube_automanager.scheduler import SchedulerState
from youtube_automanager.web.services import Services, get_services
from youtube_automanager.youtube.ytdlp_updater import DISTRIBUTION, installed_version

LOG = logging.getLogger(__name__)

MAX_RUNS_PAGE = 500

router = APIRouter()

ServicesDep = Annotated[Services, Depends(get_services)]


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


class StatusOut(BaseModel):
    scheduler_state: SchedulerState
    next_run_at: datetime | None
    last_run: RunOut | None
    # None when the account row does not exist yet.
    session_state: SessionState | None
    ytdlp_version: str | None
    # "data_folder" for a release that the service installed, "image" for the copy that came with the service.
    ytdlp_source: Literal["data_folder", "image"]


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


def _ytdlp_version(ytdlp_dir: Path) -> tuple[str | None, Literal["data_folder", "image"]]:
    version = installed_version(ytdlp_dir)
    if version:
        return version, "data_folder"
    try:
        return importlib.metadata.version(DISTRIBUTION), "image"
    except importlib.metadata.PackageNotFoundError:
        return None, "image"


@router.get("/api/status", response_model=StatusOut)
async def status(services: ServicesDep) -> StatusOut:
    next_run_times = await services.scheduler.next_run_times(1)
    last_run, session_state = await run_in_threadpool(_read_status_rows, services.sessions)
    ytdlp_version, ytdlp_source = await run_in_threadpool(_ytdlp_version, services.settings.ytdlp_dir)
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
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RunOut]:
    """Return the runs, newest first."""
    with services.sessions() as session:
        rows = session.scalars(
            select(Run).order_by(Run.started_at.desc(), Run.id.desc()).limit(limit).offset(offset)
        ).all()
        return [RunOut.model_validate(row) for row in rows]


# The pages answer each path that no route takes, with HTML. An API client gets the JSON 404 of FastAPI instead.
# This route comes last, so each API route above it matches first.
@router.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
def unknown_api_path(path: str) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Not Found"})
