"""Runs each sync and each session check in a child process, and keeps the history of the runs in the ``run`` table.

The parent writes the ``run`` row: ``running`` at the start, and the end time with ``succeeded`` or ``failed`` at the
end, and it sends the ``run_failed`` notification. The child does the sync itself, so a yt-dlp update in the data
folder takes effect at the next run. A session check on request also runs in a child, because the process of the
service never imports yt-dlp.
"""

import collections
import enum
import functools
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

import youtube_automanager
from youtube_automanager import notify
from youtube_automanager.db import make_engine, make_session_factory
from youtube_automanager.db.models import Run, RunStatus, RunTrigger, SessionState, utc_now
from youtube_automanager.settings import Settings
from youtube_automanager.youtube.session import account_row
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
# The longest time of a session check in its child. A check sends at most two requests, 5 s apart.
CHECK_TIMEOUT_S = 120
# The exit code of a session check child that cannot use the stored cookies, or finds none.
UNAVAILABLE_EXIT_CODE = 3
BUSY_TEXT = "A sync run, a session check or a yt-dlp update is active. Try again when it ends."
# How long a scheduled run waits for the sync lock. A cookie rotation or a session check keeps it for seconds, a
# refresh of the account lists or a yt-dlp update for a few minutes, and an apply of a rule for up to about 20 minutes,
# because each add sends two requests 5 s or more apart. A scheduled run that comes at that time starts late, but it
# is not lost.
SCHEDULED_WAIT_S = 1800
# The longest single wait for the sync lock, so a stop of the service ends a wait soon.
LOCK_POLL_S = 0.5

SOURCE_ROOT = Path(youtube_automanager.__file__).resolve().parent.parent


class RunAlreadyActive(Exception):
    """A sync run or a yt-dlp update holds the sync lock."""


@contextmanager
def sync_lock_held(timeout: float | None = None, lock: threading.Lock = SYNC_LOCK) -> Iterator[None]:
    """Hold the process-wide sync lock for the body of the ``with`` block, so no sync run starts in that time.

    ``timeout`` None waits until the active run ends, and 0 does not wait.
    Raise ``RunAlreadyActive`` when the lock stays taken for ``timeout`` seconds.
    """
    acquired = lock.acquire() if timeout is None else lock.acquire(timeout=timeout)
    if not acquired:
        raise RunAlreadyActive("A sync run or a yt-dlp update holds the sync lock")
    try:
        yield
    finally:
        lock.release()


class SessionCheckOutcome(enum.StrEnum):
    # The check got an answer, and the ``account`` row holds the state of the session.
    CHECKED = "checked"
    # No cookies are stored, or the service cannot open them.
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    # A run, another check or a yt-dlp update holds the sync lock, so no check started.
    BUSY = "busy"


@dataclass(frozen=True)
class SessionCheckResult:
    """The result of a session check on request, with the state of the session from the ``account`` row."""

    outcome: SessionCheckOutcome
    session_state: SessionState | None = None
    last_checked_at: datetime | None = None
    earliest_cookie_expiry: datetime | None = None
    detail: str | None = None


@dataclass(frozen=True)
class _ChildEnd:
    """How a child process ended. ``start_error`` is set when the child never started.

    ``error_text`` keeps only the end of a long stderr. ``last_line`` is the last stderr line that is not empty, in
    full, because a short child writes its result there, and a result can be longer than the error text.
    """

    returncode: int | None = None
    timed_out: bool = False
    stopped: bool = False
    start_error: str | None = None
    error_text: str = ""
    last_line: str = ""


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


def check_command() -> list[str]:
    return [sys.executable, "-m", "youtube_automanager", "check-session"]


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
        send: Callable[[notify.Notification], object] | None = None,
    ):
        """``send`` sends a notification. By default it sends to the targets of the database."""
        self._sessions = sessions
        self._send = send or functools.partial(notify.send_to_stored_targets, sessions)
        self._ytdlp_dir = ytdlp_dir
        self._database_url = database_url
        self._child_timeout_s = child_timeout_s
        self._start = start
        self._lock = lock
        # Guards `_child` and `_stopping` between the job thread and `stop`.
        self._state_lock = threading.Lock()
        self._child: ChildProcess | None = None
        self._stopping = False

    def claim(self, trigger: RunTrigger, wait_s: float = 0) -> int | None:
        """Take the sync lock and write a ``running`` row. Return the id of the run.

        Wait up to ``wait_s`` seconds for the lock. Return None and write nothing when a run, a session check, a cookie
        rotation or a yt-dlp update keeps the lock, or when the service stops. The caller must give the id to
        ``execute`` or ``abandon``, which release the lock.
        """
        if not self._acquire(wait_s):
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

    def execute(self, run_id: int, command: Sequence[str] | None = None) -> RunStatus:
        """Run the child for the claimed run ``run_id``, write the result, and release the sync lock.

        ``command`` starts the child, and by default it is the sync of ``child_command``. Each child of a run ends as
        the sync does: with exit code 0, or with the cause as the last line of stderr.
        """
        try:
            try:
                status, error_text = self._run_child(run_id, command or child_command(run_id))
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

    def _acquire(self, wait_s: float) -> bool:
        """Take the sync lock, and wait up to ``wait_s`` seconds for it. A stop of the service ends the wait."""
        deadline = time.monotonic() + wait_s
        while not self._lock.acquire(timeout=min(LOCK_POLL_S, max(deadline - time.monotonic(), 0))):
            with self._state_lock:
                stopping = self._stopping
            if stopping or time.monotonic() >= deadline:
                return False
        return True

    def run_scheduled(self) -> None:
        """The job of the schedule: one run, or nothing when a run is already active.

        A cookie rotation, a session check, a refresh of the account lists or a yt-dlp update keeps the sync lock for
        a while, so the scheduled run waits up to SCHEDULED_WAIT_S for it, and is not lost to them.
        """
        run_id = self.claim(RunTrigger.SCHEDULE, wait_s=SCHEDULED_WAIT_S)
        if run_id is None:
            LOG.warning("The scheduled run did not start, because a run is already active or the service stops")
            return
        self.execute(run_id)

    def is_active(self) -> bool:
        return self._lock.locked()

    @contextmanager
    def lock_held(self, timeout: float | None = None) -> Iterator[None]:
        """Hold the sync lock of this runner for the body of the ``with`` block, so no run and no child starts.

        Raise ``RunAlreadyActive`` when the lock stays taken for ``timeout`` seconds. 0 does not wait.
        """
        with sync_lock_held(timeout, self._lock):
            yield

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

    def check_session(self) -> SessionCheckResult:
        """Check the cookie session now in a child process, and return the state that the check wrote.

        The call blocks until the check ends. It starts nothing and returns BUSY when a run, another check or a yt-dlp
        update holds the sync lock, so a check never works on the cookies at the same time as a run.
        """
        if not self._lock.acquire(blocking=False):
            return SessionCheckResult(SessionCheckOutcome.BUSY, detail=BUSY_TEXT)
        try:
            end = self._run_process(check_command(), "The session check", CHECK_TIMEOUT_S)
        finally:
            self._lock.release()
        if end.start_error is not None:
            outcome, detail = (
                SessionCheckOutcome.FAILED,
                f"The service cannot start the session check: {end.start_error}",
            )
        elif end.timed_out:
            outcome = SessionCheckOutcome.FAILED
            detail = f"The session check did not end within {CHECK_TIMEOUT_S} s, so the service stopped it."
        elif end.returncode == 0:
            outcome, detail = SessionCheckOutcome.CHECKED, None
        elif end.stopped:
            outcome, detail = SessionCheckOutcome.FAILED, "The service stopped during the session check."
        else:
            outcome = (
                SessionCheckOutcome.UNAVAILABLE
                if end.returncode == UNAVAILABLE_EXIT_CODE
                else SessionCheckOutcome.FAILED
            )
            detail = end.error_text or f"The session check exited with code {end.returncode}."
        with self._sessions() as session:
            account = account_row(session)
        if account is None:
            return SessionCheckResult(outcome, detail=detail)
        return SessionCheckResult(
            outcome, account.session_state, account.last_checked_at, account.earliest_cookie_expiry, detail
        )

    def run_short_child(self, command: Sequence[str], name: str, timeout_s: float) -> _ChildEnd | None:
        """Run a child that works with the cookies, and tell how it ended. The call blocks until the child ends.

        The child holds the sync lock, so a run never works on the cookies at the same time, and ``stop`` stops the
        child. Return None and start nothing when a run, a session check, another child or a yt-dlp update holds the
        lock.
        """
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self._run_process(command, name, timeout_s)
        finally:
            self._lock.release()

    def _run_child(self, run_id: int, command: Sequence[str]) -> tuple[RunStatus, str | None]:
        end = self._run_process(command, f"Run {run_id}", self._child_timeout_s)
        if end.start_error is not None:
            return RunStatus.FAILED, f"The service cannot start the sync process: {end.start_error}"
        if end.timed_out:
            return RunStatus.FAILED, f"The run did not end within {self._child_timeout_s} s, so the service stopped it."
        if end.returncode == 0:
            return RunStatus.SUCCEEDED, None
        if end.stopped:
            return RunStatus.FAILED, INTERRUPTED_ERROR
        return RunStatus.FAILED, end.error_text or f"The sync process exited with code {end.returncode}."

    def _run_process(self, command: Sequence[str], name: str, timeout_s: float) -> _ChildEnd:
        """Start a child, copy its stderr, wait up to ``timeout_s`` seconds, and tell how it ended.

        ``stop`` terminates the child. A child that does not end in time gets killed.
        """
        env = child_environment(self._ytdlp_dir, self._database_url)
        with self._state_lock:
            if self._stopping:
                return _ChildEnd(stopped=True)
            # The start happens under the lock, so `stop` either sees this child or stops the work before it starts.
            try:
                child = self._start(command, env)
            except (OSError, subprocess.SubprocessError) as error:
                LOG.error("%s: cannot start the child process: %s", name, error)
                return _ChildEnd(start_error=str(error))
            self._child = child

        tail: collections.deque[str] = collections.deque(maxlen=ERROR_TAIL_LINES)
        reader = None
        if child.stderr is not None:
            reader = threading.Thread(
                target=_forward_stderr, args=(child.stderr, tail), name=f"{name}-stderr", daemon=True
            )
            reader.start()

        timed_out = False
        try:
            returncode = child.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            LOG.error("%s did not end within %s s, so the service stops it", name, timeout_s)
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
        # The reader can still run when the wait ended, so the lines come from one copy of the tail.
        kept = list(tail)
        lines = [line.strip() for line in kept if line.strip()]
        return _ChildEnd(
            returncode,
            timed_out,
            stopped,
            error_text=error_text_from_tail(kept),
            last_line=lines[-1] if lines else "",
        )

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
            return
        LOG.warning("Run %s failed: %s", run_id, error_text)
        # A run that the stop of the service ended needs no message: the user stopped the service.
        if error_text != INTERRUPTED_ERROR:
            try:
                self._send(notify.run_failed(error_text or "The run failed without an error text."))
            except Exception as error:  # noqa: BLE001
                # A failed notification must never change the result of the run.
                LOG.warning("Run %s: cannot send the failure notification: %s", run_id, type(error).__name__)


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
    and the counts of the run. When the run cannot go on, the child writes the cause as the last line of its stderr,
    because the parent takes the error text of the run from there.
    """
    # The sync engine imports yt-dlp, so only the child loads it, and a yt-dlp update takes effect at the next run.
    from youtube_automanager.engine.sync import SyncAborted, run_with_youtube

    engine = make_engine(settings.database_url)
    try:
        sessions = make_session_factory(engine)
        with sessions() as session:
            if session.get(Run, run_id) is None:
                LOG.error("Run %s is not in the database", run_id)
                return 1
        try:
            run_with_youtube(sessions, run_id, settings)
        except SyncAborted as error:
            LOG.error("Run %s stopped: %s", run_id, error)
            print(error, file=sys.stderr, flush=True)
            return 1
        return 0
    finally:
        engine.dispose()


def check_session_once(settings: Settings) -> int:
    """The body of a session check in the child process. Return the exit code of the child.

    The child writes the state of the session into the ``account`` row, sends the session events and saves the updated
    cookies. When the check cannot use the stored cookies or gets no clear answer, the child writes the cause as the
    last line of its stderr, and exits with UNAVAILABLE_EXIT_CODE or 1.
    """
    # The account client imports yt-dlp, so only the child loads it.
    from youtube_automanager.engine.account_session import AccountSession, CheckOutcome

    engine = make_engine(settings.database_url)
    try:
        sessions = make_session_factory(engine)
        account = AccountSession(sessions, settings, send=functools.partial(notify.send_to_stored_targets, sessions))
        result = account.check()
        account.write_back()
    finally:
        engine.dispose()
    LOG.info("The session check gave %s", result.outcome.value)
    if result.outcome in (CheckOutcome.ALIVE, CheckOutcome.DEAD):
        return 0
    print(result.detail, file=sys.stderr, flush=True)
    return UNAVAILABLE_EXIT_CODE if result.outcome is CheckOutcome.UNAVAILABLE else 1
