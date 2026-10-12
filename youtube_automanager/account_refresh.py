"""One refresh of the account data in a child process, so the web process never imports yt-dlp.

The service starts ``python -m youtube_automanager refresh-account`` as it starts the child of a run. The child gets the
yt-dlp folder of the data folder first on its import path, so a yt-dlp update takes effect at the next refresh.

The child ends with one of these results:

- Success: exit code 0, and the counts of ``RefreshResult`` as one JSON line at the end of stdout.
- Failure: the exit code names the kind (``EXIT_UNAVAILABLE``, ``EXIT_LOGGED_OUT`` or ``EXIT_ACCOUNT_ERROR``), and the
  cause is the last line of stderr. Any other exit code is a crash, and its stderr never goes into an answer.

``refresh_in_child`` runs the child and turns its result into a ``RefreshResult`` or a ``RefreshError``. The caller
holds the sync lock, so no run and no second refresh sends requests with the cookies at the same time.
"""

import dataclasses
import json
import logging
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence

from youtube_automanager.account_cache import RefreshResult, refresh_account_cache
from youtube_automanager.db import make_engine, make_session_factory
from youtube_automanager.runner import child_environment
from youtube_automanager.settings import Settings

LOG = logging.getLogger(__name__)

# The exit codes of the child for a failed refresh. Python uses 1 for an uncaught error, and argparse uses 2.
EXIT_UNAVAILABLE = 3
EXIT_LOGGED_OUT = 4
EXIT_ACCOUNT_ERROR = 5

# The child gets this long. A scheduled run waits longer than this for the sync lock, so a refresh never loses a run.
REFRESH_TIMEOUT_SECONDS = 300


class RefreshError(Exception):
    """The refresh failed. The text names the cause, and it holds no cookie value."""


class RefreshUnavailable(RefreshError):
    """The service cannot open the stored account: the cookies or the cookie key are missing or not valid."""


class RefreshLoggedOut(RefreshError):
    """YouTube answered as logged out, or it refused the session."""


class RefreshAccountError(RefreshError):
    """YouTube did not give the account data."""


class RefreshProcessError(RefreshError):
    """The child did not start, did not end in time, crashed, or gave no counts."""


type RunChild = Callable[[Sequence[str], Mapping[str, str], float], subprocess.CompletedProcess[str]]


def refresh_once(settings: Settings) -> int:
    """The body of one refresh in the child process. Return the exit code of the child.

    The child reads the account with the stored cookies, writes the account data to the database, and prints the counts.
    """
    # The engine imports yt-dlp, so only the child loads it, and a yt-dlp update takes effect at the next refresh.
    from youtube_automanager.engine.sync import AccountUnavailable, open_account
    from youtube_automanager.youtube.account import AccountError, NotLoggedInError

    engine = make_engine(settings.database_url)
    try:
        sessions = make_session_factory(engine)
        try:
            result = refresh_account_cache(sessions, open_account(sessions, settings))
        except AccountUnavailable as error:
            return _fail(EXIT_UNAVAILABLE, error)
        except NotLoggedInError as error:
            return _fail(EXIT_LOGGED_OUT, error)
        except AccountError as error:
            return _fail(EXIT_ACCOUNT_ERROR, error)
        print(json.dumps(dataclasses.asdict(result)), flush=True)
        return 0
    finally:
        engine.dispose()


def _fail(exit_code: int, error: Exception) -> int:
    """Write the cause as the last line of stderr, because the parent takes its answer from there."""
    cause = " ".join(str(error).split())
    LOG.error("The account data cannot refresh: %s", cause)
    print(cause, file=sys.stderr, flush=True)
    return exit_code


def run_child(command: Sequence[str], env: Mapping[str, str], timeout_s: float) -> subprocess.CompletedProcess[str]:
    """Run the child with its stdout and stderr captured. A child that does not end in ``timeout_s`` is killed."""
    return subprocess.run(
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )


def refresh_in_child(
    settings: Settings, *, timeout_s: float = REFRESH_TIMEOUT_SECONDS, run: RunChild | None = None
) -> RefreshResult:
    """Run the refresh in a child process and wait for it. Raise a ``RefreshError`` when it fails.

    ``run`` starts the child and gives its result. It is ``run_child`` by default, and a test gives a fake one.
    """
    command = [sys.executable, "-m", "youtube_automanager", "refresh-account"]
    env = child_environment(settings.ytdlp_dir, settings.database_url)
    try:
        completed = (run or run_child)(command, env, timeout_s)
    except subprocess.TimeoutExpired:
        raise RefreshProcessError(f"The refresh did not end within {timeout_s} s, so the service stopped it.") from None
    except (OSError, subprocess.SubprocessError) as error:
        raise RefreshProcessError(f"The service cannot start the refresh process: {error}") from error

    # The child writes its log to stdout and the cause of a failure to stderr, so they go on to the log of the service.
    sys.stdout.write(completed.stdout or "")
    sys.stderr.write(completed.stderr or "")
    cause = _last_line(completed.stderr)
    code = completed.returncode
    if code == 0:
        result = _counts(completed.stdout)
        if result is None:
            raise RefreshProcessError("The refresh process ended without the counts of the refresh.")
        return result
    if code == EXIT_UNAVAILABLE:
        raise RefreshUnavailable(cause or "The YouTube account is not available.")
    if code == EXIT_LOGGED_OUT:
        raise RefreshLoggedOut(cause or "YouTube answered as logged out.")
    if code == EXIT_ACCOUNT_ERROR:
        raise RefreshAccountError(cause or "yt-dlp failed to read the account.")
    # Any other exit code is a crash. Its stderr can hold anything, so the answer names only the code.
    raise RefreshProcessError(f"The refresh process exited with code {code}.")


def _last_line(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _counts(stdout: str | None) -> RefreshResult | None:
    """Read the counts from the last line of stdout. Return None when the line is not the counts."""
    line = _last_line(stdout)
    try:
        data = json.loads(line)
    except ValueError:
        return None
    names = {field.name for field in dataclasses.fields(RefreshResult)}
    if not isinstance(data, dict) or set(data) != names or any(type(value) is not int for value in data.values()):
        return None
    return RefreshResult(**data)
