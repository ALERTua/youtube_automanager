"""The check of a cookie upload with YouTube in a child process, and the store of a text that YouTube accepts.

The settings page checks the text of an upload first without a request: the format, the login cookies and their
expiry. Then it starts ``python -m youtube_automanager check-upload`` through ``SyncRunner.run_short_child``. The
child follows the protocol of ``short_child``: it gets the yt-dlp folder of the data folder first on its import path,
it holds the sync lock while it runs, so no run works on the cookies at the same time, and a stop of the service stops
it.

The child gets the cookie text on its standard input, never on its command line or in its environment, which other
processes of the machine can read. No log line, no stderr line and no error text holds the text.

The child runs the session check of ``check-session`` with a client of the text. It stores the text only when YouTube
accepts the session: as a new upload, with the state of a passed check, and with the new cookie values of the check.
It ends with one of these results:

- Success: exit code 0, and ``{"stored": true}`` as one JSON line at the end of stderr.
- Failure: the exit code names the kind, and the cause is the last line of stderr. ``EXIT_LOGGED_OUT`` means that
  YouTube refused the session, ``EXIT_YOUTUBE_ERROR`` that the check gave no clear answer, and ``EXIT_UNAVAILABLE``
  that the service cannot store the text. In each case the child stores nothing. Any other exit code is a crash.

``check_upload_in_child`` runs the child and raises a ``ChildError`` when the text is not stored.
"""

import functools
import logging
import sys

from youtube_automanager import notify
from youtube_automanager.db import make_engine, make_session_factory
from youtube_automanager.runner import CHECK_TIMEOUT_S, SyncRunner
from youtube_automanager.settings import Settings
from youtube_automanager.short_child import (
    EXIT_LOGGED_OUT,
    EXIT_UNAVAILABLE,
    EXIT_YOUTUBE_ERROR,
    ChildFailed,
    fail,
    read_json,
    run_child,
    succeed,
)

# The check sends the request of a session check, so it gets the time of a session check. That time is shorter than
# ``SCHEDULED_WAIT_S`` of the runner, so an upload never makes a scheduled run give up.
UPLOAD_CHECK_TIMEOUT_S = CHECK_TIMEOUT_S
WHAT = "The check of the uploaded cookies"
STORED = {"stored": True}


def check_upload_command() -> list[str]:
    return [sys.executable, "-m", "youtube_automanager", "check-upload"]


def check_upload_once(settings: Settings, text: str) -> int:
    """The body of the check of a cookie upload in the child process. Return the exit code of the child.

    ``text`` is the cookie text from the standard input. The child stores it only when YouTube accepts its session.
    """
    # The account session imports yt-dlp, so only the child loads it, and a yt-dlp update reaches the next upload.
    from youtube_automanager.engine.account_session import AccountSession, CheckOutcome

    engine = make_engine(settings.database_url)
    try:
        sessions = make_session_factory(engine)
        account = AccountSession(sessions, settings, send=functools.partial(notify.send_to_stored_targets, sessions))
        result = account.check_upload(text)
        if result.outcome is CheckOutcome.ALIVE:
            account.write_back()
    finally:
        engine.dispose()
    if result.outcome is CheckOutcome.ALIVE:
        return succeed(STORED)
    if result.outcome is CheckOutcome.UNAVAILABLE:
        return fail(WHAT, EXIT_UNAVAILABLE, result.detail)
    # A session that YouTube refuses, or a check without a clear answer, is no fault of the service.
    code = EXIT_LOGGED_OUT if result.outcome is CheckOutcome.DEAD else EXIT_YOUTUBE_ERROR
    return fail(WHAT, code, result.detail, level=logging.WARNING)


def check_upload_in_child(runner: SyncRunner, text: str) -> None:
    """Check the cookie text with YouTube in a child process of ``runner``, which stores the text when it lives.

    The call blocks until the child ends. Raise a ``ChildError`` when the child did not store the text: ``ChildBusy``
    while another task holds the sync lock, ``ChildLoggedOut`` when YouTube refused the session, ``ChildUnavailable``
    when the service cannot store the text, and ``ChildYouTubeError`` or ``ChildFailed`` when the check gave no clear
    answer.
    """
    data = read_json(run_child(runner, check_upload_command(), "cookie check", UPLOAD_CHECK_TIMEOUT_S, text))
    # The flag must be the JSON boolean itself, because 1 == True in Python.
    if data != STORED or data["stored"] is not True:
        raise ChildFailed("The cookie check process ended without the confirmation of the store.")
