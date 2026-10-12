"""Runs each sync in a child process and keeps the history of the runs in the ``run`` table.

The parent writes the ``run`` row: ``running`` at the start, and the end time with ``succeeded`` or ``failed`` at the
end. The child does the sync itself, so a yt-dlp update in the data folder takes effect at the next run.
"""

import collections
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

import youtube_automanager
from youtube_automanager.db import make_engine, make_session_factory
from youtube_automanager.db.models import Run, RunStatus, RunTrigger, utc_now
from youtube_automanager.settings import Settings
from youtube_automanager.youtube.ytdlp_updater import child_env

LOG = logging.getLogger(__name__)

# One sync at a time in this process. A yt-dlp update takes the same lock through `sync_lock_held`, because it must
# not replace the yt-dlp folder while a child imports from it.
SYNC_LOCK = threading.Lock()

INTERRUPTED_ERROR = "The service stopped during the run."
ERROR_TAIL_LINES = 10
ERROR_TEXT_MAX_CHARS = 2000
# How long a killed child and the reader of its stderr get to end.
KILL_WAIT_SECONDS = 10

SOURCE_ROOT = Path(youtube_automanager.__file__).resolve().parent.parent


class RunAlreadyActive(Exception):
    """A sync run or a yt-dlp update holds the sync lock."""


@contextmanager
def sync_lock_held(timeout: float | None = None) -> Iterator[None]:
    """Hold the process-wide sync lock for the body of the ``with`` block, so no sync run starts in that time.

    ``timeout`` None waits until the active run ends, and 0 does not wait.
    Raise ``RunAlreadyActive`` when the lock stays taken for ``timeout`` seconds.
    """
    acquired = SYNC_LOCK.acquire() if timeout is None else SYNC_LOCK.acquire(timeout=timeout)
    if not acquired:
        raise RunAlreadyActive("A sync run or a yt-dlp update holds the sync lock")
    try:
        yield
    finally:
        SYNC_LOCK.release()


class ChildProcess(Protocol):
    """The part of ``subprocess.Popen`` that the runner uses."""

    @property
    def stderr(self) -> Iterable[str] | None: ...

    @property
    def returncode(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


type ChildStarter = Callable[[Sequence[str], Mapping[str, str]], ChildProcess]


def start_child(command: Sequence[str], env: Mapping[str, str]) -> subprocess.Popen[str]:
    """Start the child with its stdout on the stdout of the service and its stderr in a pipe."""
    return subprocess.Popen(
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def child_command(run_id: int) -> list[str]:
    return [sys.executable, "-m", "youtube_automanager", "run-once", "--run-id", str(run_id)]


def child_environment(ytdlp_dir: Path, database_url: str, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the environment of the child.

    The yt-dlp folder comes first on the import path, and the source root of the service follows it, so the child
    imports the service also when the service started in another folder. The child gets the database of the parent
    and writes its stderr in UTF-8, which the parent reads.
    """
    env = dict(os.environ if base is None else base)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([str(SOURCE_ROOT), existing]) if existing else str(SOURCE_ROOT)
    env = child_env(ytdlp_dir, env)
    env["DATABASE_URL"] = database_url
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def error_text_from_tail(lines: Iterable[str]) -> str:
    """Join the last stderr lines into the error text of a run, and keep only its end when it is too long."""
    text = "\n".join(line.rstrip() for line in lines).strip()
    if len(text) > ERROR_TEXT_MAX_CHARS:
        text = "..." + text[-(ERROR_TEXT_MAX_CHARS - 3) :]
    return text


def _forward_stderr(stream: Iterable[str], tail: collections.deque[str]) -> None:
    """Copy each stderr line of the child to the stderr of the service, and keep the last lines for the error text."""
    try:
        for line in stream:
            sys.stderr.write(line)
            tail.append(line)
    except (OSError, ValueError) as error:
        LOG.warning("Cannot read the stderr of the sync process: %s", error)


class SyncRunner:
    """Starts the sync runs, one at a time, and writes their ``run`` rows."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        ytdlp_dir: Path,
        database_url: str,
        child_timeout_s: float,
        start: ChildStarter = start_child,
        lock: threading.Lock = SYNC_LOCK,
    ):
        self._sessions = sessions
        self._ytdlp_dir = ytdlp_dir
        self._database_url = database_url
        self._child_timeout_s = child_timeout_s
        self._start = start
        self._lock = lock
        # Guards `_child` and `_stopping` between the job thread and `stop`.
        self._state_lock = threading.Lock()
        self._child: ChildProcess | None = None
        self._stopping = False

    def claim(self, trigger: RunTrigger) -> int | None:
        """Take the sync lock and write a ``running`` row. Return the id of the run.

        Return None and write nothing when a run or a yt-dlp update is active, or when the service stops.
        The caller must give the id to ``execute`` or ``abandon``, which release the lock.
        """
        if not self._lock.acquire(blocking=False):
            return None
        try:
            with self._state_lock:
                stopping = self._stopping
            if stopping:
                self._lock.release()
                return None
            with self._sessions.begin() as session:
                run = Run(status=RunStatus.RUNNING, trigger=trigger)
                session.add(run)
                session.flush()
                run_id = run.id
        except BaseException:
            self._lock.release()
            raise
        LOG.info("Run %s started by %s", run_id, trigger.value)
        return run_id

    def execute(self, run_id: int) -> RunStatus:
        """Run the child for the claimed run ``run_id``, write the result, and release the sync lock."""
        try:
            try:
                status, error_text = self._run_child(run_id)
            except Exception as error:
                LOG.exception("Run %s: the service cannot run the sync process", run_id)
                status, error_text = RunStatus.FAILED, f"The service cannot run the sync process: {error}"
            self._finish(run_id, status, error_text)
            return status
        finally:
            self._lock.release()

    def abandon(self, run_id: int, error_text: str) -> None:
        """Write the claimed run ``run_id`` as failed without a start of the child, and release the sync lock."""
        try:
            self._finish(run_id, RunStatus.FAILED, error_text)
        finally:
            self._lock.release()

    def run_scheduled(self) -> None:
        """The job of the schedule: one run, or nothing when a run is already active."""
        run_id = self.claim(RunTrigger.SCHEDULE)
        if run_id is None:
            LOG.warning("The scheduled run did not start, because a run is already active or the service stops")
            return
        self.execute(run_id)

    def is_active(self) -> bool:
        return self._lock.locked()

    def stop(self, timeout: float) -> bool:
        """Start no more runs, stop the active child, and wait up to ``timeout`` s until the active run ends.

        The stopped run ends as ``failed`` with the text that the service stopped during the run.
        Return True when no run is active at the end.
        """
        with self._state_lock:
            self._stopping = True
            child = self._child
        if child is not None and child.poll() is None:
            LOG.info("Stopping the active sync process")
            child.terminate()
        if self._lock.acquire(timeout=timeout):
            self._lock.release()
            return True
        if child is not None and child.poll() is None:
            child.kill()
        LOG.warning("The active run did not end within %s s", timeout)
        return False

    def _run_child(self, run_id: int) -> tuple[RunStatus, str | None]:
        command = child_command(run_id)
        env = child_environment(self._ytdlp_dir, self._database_url)
        with self._state_lock:
            if self._stopping:
                return RunStatus.FAILED, INTERRUPTED_ERROR
            # The start happens under the lock, so `stop` either sees this child or stops the run before it starts.
            try:
                child = self._start(command, env)
            except (OSError, subprocess.SubprocessError) as error:
                LOG.error("Run %s: cannot start the sync process: %s", run_id, error)
                return RunStatus.FAILED, f"The service cannot start the sync process: {error}"
            self._child = child

        tail: collections.deque[str] = collections.deque(maxlen=ERROR_TAIL_LINES)
        reader = None
        if child.stderr is not None:
            reader = threading.Thread(
                target=_forward_stderr, args=(child.stderr, tail), name=f"run-{run_id}-stderr", daemon=True
            )
            reader.start()

        timed_out = False
        try:
            returncode = child.wait(timeout=self._child_timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            LOG.error("Run %s did not end within %s s, so the service stops it", run_id, self._child_timeout_s)
            child.kill()
            try:
                returncode = child.wait(timeout=KILL_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                returncode = None
        finally:
            with self._state_lock:
                self._child = None
                stopped = self._stopping
        if reader is not None:
            # A grandchild can keep the pipe open after the child ends, so the wait has a limit.
            reader.join(timeout=KILL_WAIT_SECONDS)

        if timed_out:
            return RunStatus.FAILED, f"The run did not end within {self._child_timeout_s} s, so the service stopped it."
        if returncode == 0:
            return RunStatus.SUCCEEDED, None
        if stopped:
            return RunStatus.FAILED, INTERRUPTED_ERROR
        return RunStatus.FAILED, error_text_from_tail(tail) or f"The sync process exited with code {returncode}."

    def _finish(self, run_id: int, status: RunStatus, error_text: str | None) -> None:
        with self._sessions.begin() as session:
            run = session.get(Run, run_id)
            if run is None:
                LOG.error("Run %s is not in the database, so its result is lost", run_id)
                return
            run.status = status
            run.finished_at = utc_now()
            run.error_text = error_text
        if status is RunStatus.SUCCEEDED:
            LOG.info("Run %s succeeded", run_id)
        else:
            LOG.warning("Run %s failed: %s", run_id, error_text)


def mark_interrupted_runs(sessions: sessionmaker[Session]) -> int:
    """Write each ``running`` row as ``failed``, because no run survives a stop of the service.

    The end time stays empty, because the service does not know when the run stopped. Return the number of rows.
    """
    with sessions.begin() as session:
        result = session.execute(
            update(Run)
            .where(Run.status == RunStatus.RUNNING)
            .values(status=RunStatus.FAILED, error_text=INTERRUPTED_ERROR)
        )
        count = result.rowcount
    if count:
        LOG.warning("Marked %s interrupted run(s) as failed", count)
    return count


def run_once(run_id: int, settings: Settings) -> int:
    """The body of one sync in the child process. Return the exit code of the child.

    The parent owns the status of the ``run`` row. The child writes only what the sync finds: the ``run_action`` rows
    and the counts of the run.
    """
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            if session.get(Run, run_id) is None:
                LOG.error("Run %s is not in the database", run_id)
                return 1
        LOG.info("Run %s: the sync engine is not written yet, so the run changes nothing", run_id)
        return 0
    finally:
        engine.dispose()
