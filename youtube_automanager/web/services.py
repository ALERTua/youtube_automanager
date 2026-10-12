"""The parts of the running service that the routes and the pages use."""

from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.runner import SyncRunner
from youtube_automanager.scheduler import Scheduler
from youtube_automanager.settings import Settings
from youtube_automanager.ytdlp_job import YtdlpUpdates


@dataclass(frozen=True)
class Services:
    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    runner: SyncRunner
    scheduler: Scheduler
    ytdlp: YtdlpUpdates


def get_services(request: Request) -> Services:
    """A FastAPI dependency. The lifespan of the application puts the services on ``app.state``."""
    return request.app.state.services
