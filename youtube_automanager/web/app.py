"""The FastAPI application of the service and its lifespan: the database, the schedule, the scheduler, the cookie
rotation and the daily yt-dlp update.

``create_app`` returns a plain FastAPI application, so more routes and pages can mount on it before it starts.
"""

import asyncio
import functools
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import make_url

from youtube_automanager.db import make_engine, make_session_factory, upgrade_to_head
from youtube_automanager.db.backup import backup_before_upgrade
from youtube_automanager.log_setup import setup_logging
from youtube_automanager.retention import clean_history
from youtube_automanager.rotation import CookieRotation
from youtube_automanager.runner import ChildStarter, SyncRunner, mark_interrupted_runs, start_child
from youtube_automanager.scheduler import Scheduler, load_schedule
from youtube_automanager.settings import Settings, get_settings
from youtube_automanager.web.access import AccessGuard
from youtube_automanager.web.api import router
from youtube_automanager.web.services import Services
from youtube_automanager.youtube.ytdlp_updater import update as update_ytdlp
from youtube_automanager.ytdlp_check import check_release_in_child
from youtube_automanager.ytdlp_job import YtdlpUpdates
from youtube_automanager.ytdlp_job import read_settings as read_ytdlp_settings

LOG = logging.getLogger(__name__)

# How long a shutdown waits for the stopped child to end. The next start marks a run that is still open as failed.
STOP_TIMEOUT_SECONDS = 10


def _prepare_database(url: str) -> None:
    """Create the folder of a SQLite file, copy the file before a pending migration, and bring the schema to the newest
    version. A failed copy stops the start before the migration."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database and parsed.database != ":memory:":
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    backup_before_upgrade(url)
    upgrade_to_head(url)


def create_app(settings: Settings | None = None, *, start: ChildStarter = start_child) -> FastAPI:
    """Create the application. ``start`` starts the child process of a run, and a test gives a fake one."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        setup_logging(settings.verbose)
        LOG.info("Starting with the database %s", make_url(settings.database_url).render_as_string(hide_password=True))
        await asyncio.to_thread(_prepare_database, settings.database_url)
        engine = make_engine(settings.database_url)
        try:
            sessions = make_session_factory(engine)
            spec = await asyncio.to_thread(load_schedule, sessions, settings.tz)
            await asyncio.to_thread(mark_interrupted_runs, sessions)
            runner = SyncRunner(
                sessions,
                ytdlp_dir=settings.ytdlp_dir,
                database_url=settings.database_url,
                child_timeout_s=settings.child_timeout_s,
                start=start,
            )
            # The scheduler runs the cleanup of the old history once now, and then each day.
            scheduler = Scheduler(runner, cleanup=functools.partial(clean_history, sessions))
            await scheduler.start(spec)
            # The setting of the rotation decides at each turn whether the turn rotates.
            await scheduler.start_rotation(CookieRotation(sessions, settings))
            # A new yt-dlp release replaces the active one only after its check in a child process.
            smoke = functools.partial(check_release_in_child, runner, sessions, settings)
            ytdlp = YtdlpUpdates(sessions, runner, settings.ytdlp_dir, install=update_ytdlp, smoke=smoke)
            ytdlp_settings = await asyncio.to_thread(read_ytdlp_settings, sessions)
            await scheduler.start_ytdlp_updates(ytdlp.run_scheduled, ytdlp_settings.at, ytdlp_settings.enabled)
            app.state.services = Services(settings, engine, sessions, runner, scheduler, ytdlp)
            try:
                yield
            finally:
                # The runner stops first, so the scheduler starts no new run and has no job in a thread at its shutdown.
                await asyncio.to_thread(runner.stop, STOP_TIMEOUT_SECONDS)
                await scheduler.shutdown()
        finally:
            engine.dispose()
            LOG.info("The service stopped")

    app = FastAPI(title="YouTube AutoManager", lifespan=lifespan)
    app.include_router(router)
    # The middleware of ``app`` also covers the pages and the websocket of NiceGUI, which mount at its root.
    password = settings.ui_password.get_secret_value() if settings.ui_password else ""
    app.add_middleware(AccessGuard, allowed_hosts=settings.allowed_hosts, password=password)
    return app
