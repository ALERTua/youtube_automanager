"""One refresh of the account data in a child process, so the web process never imports yt-dlp.

The service starts ``python -m youtube_automanager refresh-account`` through ``SyncRunner.run_short_child``, as it
starts a session check. The child gets the yt-dlp folder of the data folder first on its import path, it holds the sync
lock while it runs, and a stop of the service stops it.

The child works with the account as a run does: it checks the session of the stored cookies, reads the lists, and
saves the updated cookies. It ends with one of these results:

- Success: exit code 0, and the counts of ``RefreshResult`` as one JSON line at the end of stderr. The runner copies
  and keeps only the stderr of a child, so the counts come on that stream.
- Failure: the exit code names the kind (``EXIT_UNAVAILABLE``, ``EXIT_LOGGED_OUT`` or ``EXIT_ACCOUNT_ERROR``), and the
  cause is the last line of stderr. Any other exit code is a crash, and its stderr never goes into an answer.

``refresh_in_child`` runs the child and turns its result into a ``RefreshResult`` or a ``RefreshError``.
"""

import dataclasses
import functools
import json
import logging
import sys

from youtube_automanager import notify
from youtube_automanager.account_cache import RefreshResult, refresh_account_cache
from youtube_automanager.db import make_engine, make_session_factory
from youtube_automanager.runner import BUSY_TEXT, UNAVAILABLE_EXIT_CODE, SyncRunner
from youtube_automanager.settings import Settings

LOG = logging.getLogger(__name__)

# The exit codes of the child for a failed refresh. A child that cannot use the stored cookies, or finds none, ends with
# the code of a session check child for the same case. The other two codes belong to the refresh only. Python uses 1 for
# an uncaught error, and argparse uses 2.
EXIT_UNAVAILABLE = UNAVAILABLE_EXIT_CODE
EXIT_LOGGED_OUT = 4
EXIT_ACCOUNT_ERROR = 5

# The child gets this long. It must stay shorter than ``SCHEDULED_WAIT_S`` of the runner, the time that a scheduled run
# waits for the sync lock, so a refresh never makes a scheduled run give up.
REFRESH_TIMEOUT_SECONDS = 300

# The cause that the child writes when the session check finds no cookie version that YouTube accepts.
NO_ACCEPTED_VERSION = "the session check found no stored cookie version that YouTube accepts"


class RefreshError(Exception):
    """The refresh failed. The text names the cause, and it holds no cookie value."""


class RefreshBusy(RefreshError):
    """A run, a session check, another refresh or a yt-dlp update holds the sync lock, so no child started."""


class RefreshUnavailable(RefreshError):
    """The service cannot open the stored account: the cookies or the cookie key are missing or not valid."""


class RefreshLoggedOut(RefreshError):
    """YouTube answered as logged out, or it refused each stored cookie version."""


class RefreshAccountError(RefreshError):
    """YouTube did not give the account data."""


class RefreshProcessError(RefreshError):
    """The child did not start, did not end in time, was stopped, crashed, or gave no counts."""


def refresh_command() -> list[str]:
    return [sys.executable, "-m", "youtube_automanager", "refresh-account"]


def refresh_once(settings: Settings) -> int:
    """The body of one refresh in the child process. Return the exit code of the child.

    The child checks the session of the stored cookies, reads the account, writes the account data to the database,
    saves the updated cookies, and writes the counts as the last line of stderr.
    """
    # The account session imports yt-dlp, so only the child loads it, and a yt-dlp update takes effect at the next
    # refresh.
    from youtube_automanager.engine.account_session import AccountSession, CheckOutcome
    from youtube_automanager.youtube.account import AccountError, NotLoggedInError

    engine = make_engine(settings.database_url)
    try:
        sessions = make_session_factory(engine)
        account = AccountSession(sessions, settings, send=functools.partial(notify.send_to_stored_targets, sessions))
        checked = account.check()
        if checked.outcome is CheckOutcome.UNAVAILABLE:
            return _fail(EXIT_UNAVAILABLE, checked.detail)
        if checked.outcome is CheckOutcome.DEAD:
            return _fail(EXIT_LOGGED_OUT, NO_ACCEPTED_VERSION)
        if checked.outcome is not CheckOutcome.ALIVE:
            return _fail(EXIT_ACCOUNT_ERROR, checked.detail)
        try:
            result = refresh_account_cache(sessions, account.open())
        except NotLoggedInError as error:
            account.died(error)
            return _fail(EXIT_LOGGED_OUT, str(error))
        except AccountError as error:
            return _fail(EXIT_ACCOUNT_ERROR, str(error))
        finally:
            account.write_back()
        print(json.dumps(dataclasses.asdict(result)), file=sys.stderr, flush=True)
        return 0
    finally:
        engine.dispose()


def _fail(exit_code: int, cause: str | None) -> int:
    """Write the cause as the last line of stderr, because the parent takes its answer from there."""
    line = " ".join((cause or "").split())
    LOG.error("The account data cannot refresh: %s", line)
    print(line, file=sys.stderr, flush=True)
    return exit_code


def refresh_in_child(runner: SyncRunner) -> RefreshResult:
    """Run the refresh in a child process of ``runner`` and wait for it. Raise a ``RefreshError`` when it fails."""
    end = runner.run_short_child(refresh_command(), "The account refresh", REFRESH_TIMEOUT_SECONDS)
    if end is None:
        raise RefreshBusy(BUSY_TEXT)
    if end.start_error is not None:
        raise RefreshProcessError(f"The service cannot start the refresh process: {end.start_error}")
    if end.timed_out:
        raise RefreshProcessError(
            f"The refresh did not end within {REFRESH_TIMEOUT_SECONDS} s, so the service stopped it."
        )
    cause = _last_line(end.error_text)
    code = end.returncode
    if code == 0:
        result = _counts(cause)
        if result is None:
            raise RefreshProcessError("The refresh process ended without the counts of the refresh.")
        return result
    if end.stopped:
        raise RefreshProcessError("The service stopped during the account refresh.")
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


def _counts(line: str) -> RefreshResult | None:
    """Read the counts from a line. Return None when the line is not the counts."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    names = {field.name for field in dataclasses.fields(RefreshResult)}
    if not isinstance(data, dict) or set(data) != names or any(type(value) is not int for value in data.values()):
        return None
    return RefreshResult(**data)
