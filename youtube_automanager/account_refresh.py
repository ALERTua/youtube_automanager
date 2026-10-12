"""One refresh of the account data in a child process, so the web process never imports yt-dlp.

The service starts ``python -m youtube_automanager refresh-account`` through ``SyncRunner.run_short_child``, as it
starts a session check. The child follows the protocol of ``short_child``: it gets the yt-dlp folder of the data folder
first on its import path, it holds the sync lock while it runs, and a stop of the service stops it.

The child works with the account as a run does: it checks the session of the stored cookies, reads the lists, and
saves the updated cookies. It ends with one of these results:

- Success: exit code 0, and the counts of ``RefreshResult`` as one JSON line at the end of stderr.
- Failure: the exit code names the kind (``EXIT_UNAVAILABLE``, ``EXIT_LOGGED_OUT`` or ``EXIT_ACCOUNT_ERROR``), and the
  cause is the last line of stderr. Any other exit code is a crash, and its stderr never goes into an answer.

``refresh_in_child`` runs the child and turns its result into a ``RefreshResult`` or a ``RefreshError``.
"""

import dataclasses
import sys

from youtube_automanager import short_child
from youtube_automanager.account_cache import RefreshResult, refresh_account_cache
from youtube_automanager.runner import SyncRunner
from youtube_automanager.settings import Settings
from youtube_automanager.short_child import read_json, run_child, work_with_account

# The exit codes of a failed refresh are the codes of each short child.
EXIT_UNAVAILABLE = short_child.EXIT_UNAVAILABLE
EXIT_LOGGED_OUT = short_child.EXIT_LOGGED_OUT
EXIT_ACCOUNT_ERROR = short_child.EXIT_YOUTUBE_ERROR
NO_ACCEPTED_VERSION = short_child.NO_ACCEPTED_VERSION

# The errors of a refresh are the errors of each short child.
RefreshError = short_child.ChildError
RefreshBusy = short_child.ChildBusy
RefreshUnavailable = short_child.ChildUnavailable
RefreshLoggedOut = short_child.ChildLoggedOut
RefreshAccountError = short_child.ChildYouTubeError
RefreshProcessError = short_child.ChildFailed

# The child gets this long. It must stay shorter than ``SCHEDULED_WAIT_S`` of the runner, the time that a scheduled run
# waits for the sync lock, so a refresh never makes a scheduled run give up.
REFRESH_TIMEOUT_SECONDS = 300


def refresh_command() -> list[str]:
    return [sys.executable, "-m", "youtube_automanager", "refresh-account"]


def refresh_once(settings: Settings) -> int:
    """The body of one refresh in the child process. Return the exit code of the child.

    The child checks the session of the stored cookies, reads the account, writes the account data to the database,
    saves the updated cookies, and writes the counts as the last line of stderr.
    """
    return work_with_account(
        settings,
        "The account refresh",
        lambda sessions, client: dataclasses.asdict(refresh_account_cache(sessions, client)),
    )


def refresh_in_child(runner: SyncRunner) -> RefreshResult:
    """Run the refresh in a child process of ``runner`` and wait for it. Raise a ``RefreshError`` when it fails."""
    result = _counts(run_child(runner, refresh_command(), "refresh", REFRESH_TIMEOUT_SECONDS))
    if result is None:
        raise RefreshProcessError("The refresh process ended without the counts of the refresh.")
    return result


def _counts(line: str) -> RefreshResult | None:
    """Read the counts from a line. Return None when the line is not the counts."""
    data = read_json(line)
    names = {field.name for field in dataclasses.fields(RefreshResult)}
    if not isinstance(data, dict) or set(data) != names or any(type(value) is not int for value in data.values()):
        return None
    return RefreshResult(**data)
