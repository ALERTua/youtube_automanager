"""The protocol of a short child process that does one piece of YouTube work for the web process.

The web process never imports yt-dlp. Each piece of YouTube work that a page or a route asks for, for example a
refresh of the account lists, a channel search, a subscription change or the check of a cookie upload, runs in a child
process ``python -m youtube_automanager <command>``, which ``SyncRunner.run_short_child`` starts. The child gets the
yt-dlp folder of the data folder first on its import path, so a yt-dlp update reaches each piece of work. It holds the
sync lock while it runs, and a stop of the service stops it. A child that needs a secret, for example a cookie text,
gets it on its standard input, never on its command line or in its environment.

A child ends with one of these results:

- Success: exit code 0, and its result as one JSON line at the end of stderr. The runner copies and keeps only the
  stderr of a child, so the result comes on that stream. The log of the child is on stdout.
- Failure: the exit code names the kind (``EXIT_UNAVAILABLE``, ``EXIT_LOGGED_OUT`` or ``EXIT_YOUTUBE_ERROR``), and the
  cause is the last line of stderr. Any other exit code is a crash, and its stderr never goes into an answer.

``work_with_account`` is the body of a child that works with the account, and ``run_child`` runs a child in the parent
and turns a failure into a ``ChildError``.
"""

import functools
import json
import logging
import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager import notify
from youtube_automanager.db import make_engine, make_session_factory
from youtube_automanager.runner import BUSY_TEXT, UNAVAILABLE_EXIT_CODE, SyncRunner
from youtube_automanager.settings import Settings

if TYPE_CHECKING:
    from youtube_automanager.engine.account_session import ClientLike

LOG = logging.getLogger(__name__)

# The exit codes of a failed child. A child that cannot use the stored cookies, or finds none, ends with the code of a
# session check child for the same case. Python uses 1 for an uncaught error, and argparse uses 2.
EXIT_UNAVAILABLE = UNAVAILABLE_EXIT_CODE
# YouTube answered as logged out, or it refused each stored cookie version.
EXIT_LOGGED_OUT = 4
# YouTube did not give the data, or it did not confirm a change.
EXIT_YOUTUBE_ERROR = 5

# The cause that a child writes when the session check finds no cookie version that YouTube accepts.
NO_ACCEPTED_VERSION = "the session check found no stored cookie version that YouTube accepts"

type AccountWork = Callable[[sessionmaker[Session], ClientLike], object]


class ChildError(Exception):
    """The work of a child failed. The text names the cause, and it holds no cookie value."""


class ChildBusy(ChildError):
    """A run, a session check, another child or a yt-dlp update holds the sync lock, so no child started."""


class ChildUnavailable(ChildError):
    """The service cannot open the stored account: the cookies or the cookie key are missing or not valid."""


class ChildLoggedOut(ChildError):
    """YouTube answered as logged out, or it refused each stored cookie version."""


class ChildYouTubeError(ChildError):
    """YouTube did not give the data, or it did not confirm a change."""


class ChildFailed(ChildError):
    """The child did not start, did not end in time, was stopped, crashed, or gave no result."""


def fail(what: str, exit_code: int, cause: str | None, *, level: int = logging.ERROR) -> int:
    """Write the cause as the last line of stderr, because the parent takes its answer from there. Return the code.

    ``what`` names the work in the log, for example "The account refresh". ``level`` is the level of the log line.
    """
    line = " ".join((cause or "").split())
    LOG.log(level, "%s failed: %s", what, line)
    print(line, file=sys.stderr, flush=True)
    return exit_code


def succeed(result: object) -> int:
    """Write the result as one JSON line at the end of stderr. Return the exit code 0."""
    print(json.dumps(result), file=sys.stderr, flush=True)
    return 0


def work_with_account(settings: Settings, what: str, work: AccountWork) -> int:
    """The body of a child that works with the account. Return the exit code of the child.

    The child works with the account as a run does: it checks the session of the stored cookies, gives the checked
    client to ``work``, and saves the updated cookies. A logged-out answer during the work marks the session dead.
    ``work`` gets the sessions of the database and the client, and returns the result, which goes to stderr as JSON.
    ``what`` names the work in the log.
    """
    # The account session imports yt-dlp, so only the child loads it, and a yt-dlp update takes effect at the next
    # child.
    from youtube_automanager.engine.account_session import AccountSession, CheckOutcome
    from youtube_automanager.youtube.account import AccountError, NotLoggedInError

    engine = make_engine(settings.database_url)
    try:
        sessions = make_session_factory(engine)
        account = AccountSession(sessions, settings, send=functools.partial(notify.send_to_stored_targets, sessions))
        checked = account.check()
        if checked.outcome is CheckOutcome.UNAVAILABLE:
            return fail(what, EXIT_UNAVAILABLE, checked.detail)
        if checked.outcome is CheckOutcome.DEAD:
            return fail(what, EXIT_LOGGED_OUT, NO_ACCEPTED_VERSION)
        if checked.outcome is not CheckOutcome.ALIVE:
            return fail(what, EXIT_YOUTUBE_ERROR, checked.detail)
        try:
            result = work(sessions, account.open())
        except NotLoggedInError as error:
            account.died(error)
            return fail(what, EXIT_LOGGED_OUT, str(error))
        except AccountError as error:
            return fail(what, EXIT_YOUTUBE_ERROR, str(error))
        finally:
            account.write_back()
        return succeed(result)
    finally:
        engine.dispose()


def run_child(
    runner: SyncRunner, command: Sequence[str], noun: str, timeout_s: float, input_text: str | None = None
) -> str:
    """Run a child of ``runner`` and wait for it. Return the last line of its stderr after a success.

    ``noun`` names the work in the error texts, for example "refresh". ``input_text`` goes to the standard input of
    the child. Raise a ``ChildError`` when the child fails. The caller reads its result from the returned line.
    """
    end = runner.run_short_child(command, f"The {noun}", timeout_s, input_text)
    if end is None:
        raise ChildBusy(BUSY_TEXT)
    if end.start_error is not None:
        raise ChildFailed(f"The service cannot start the {noun} process: {end.start_error}")
    if end.timed_out:
        raise ChildFailed(f"The {noun} did not end within {timeout_s} s, so the service stopped it.")
    code = end.returncode
    if code == 0:
        return end.last_line
    if end.stopped:
        raise ChildFailed(f"The service stopped during the {noun}.")
    cause = end.last_line
    if code == EXIT_UNAVAILABLE:
        raise ChildUnavailable(cause or "The YouTube account is not available.")
    if code == EXIT_LOGGED_OUT:
        raise ChildLoggedOut(cause or "YouTube answered as logged out.")
    if code == EXIT_YOUTUBE_ERROR:
        raise ChildYouTubeError(cause or "YouTube did not give the data or did not confirm the change.")
    # Any other exit code is a crash. Its stderr can hold anything, so the answer names only the code.
    raise ChildFailed(f"The {noun} process exited with code {code}.")


def read_json(line: str) -> object:
    """Return the JSON value of a result line, or None when the line is not JSON."""
    try:
        return json.loads(line)
    except ValueError:
        return None
