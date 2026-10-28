"""The check of a new yt-dlp release in a child process, before the release replaces the active one.

``ytdlp_updater.update`` installs a new release into the folder ``<target>.new`` and gives that folder to
``check_release_in_child``. That function starts ``python -m youtube_automanager check-ytdlp`` with the new folder
first on the import path of the child, while the update holds the sync lock. The child follows the protocol of
``short_child``, and a stop of the service stops it. The child makes sure of four things, in this order:

1. ``yt_dlp`` comes from the new folder, and its version is the version that the update installed.
2. Each attribute of ``account.YTDLP_INTERNALS`` exists in the new release.
3. One anonymous flat read of the uploads of the official YouTube channel gives an entry with an id.
4. When the database stores cookies, the session check of ``AccountClient`` runs with the new release. A live session
   and a logged-out answer both pass, because both prove that the code path works. The check saves no cookie, and its
   request takes a token of the request budget on the database of the service.

The child gets the cookie text on its standard input, never on its command line or in its environment. No log line,
no stderr line and no error text holds the text. A failure of any step, a network failure included, ends the child
with ``EXIT_YOUTUBE_ERROR`` and the cause, and the update then keeps the active release.

This module imports yt-dlp only in the child, so the process of the service can use it.
"""

import logging
import sys
import traceback
from pathlib import Path

from packaging.version import InvalidVersion, Version
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db import make_engine, make_session_factory
from youtube_automanager.pacing import RequestBudget
from youtube_automanager.runner import SyncRunner
from youtube_automanager.settings import Settings
from youtube_automanager.short_child import EXIT_YOUTUBE_ERROR, ChildError, child_result, fail, read_json, succeed
from youtube_automanager.youtube.session import CookieCipher, CookieKeyError, load_current

LOG = logging.getLogger(__name__)

# The official YouTube channel. Its uploads playlist is the target of the anonymous read of the check.
CHECK_CHANNEL_ID = "UCBR8-60-B28hp2BmDPdntcQ"
CHECK_TIMEOUT_S = 120
WHAT = "The check of a new yt-dlp release"
NOUN = "check of the new yt-dlp"


def check_command(folder: Path, version: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "youtube_automanager",
        "check-ytdlp",
        "--folder",
        str(folder),
        "--version",
        version,
    ]


def _same_release(module_version: str, installed: str) -> bool:
    """Return True when the ``__version__`` of yt-dlp names the PyPI version ``installed``.

    ``__version__`` has no leading zeros dropped and no ``.dev0``: ``2026.08.19`` for ``2026.8.19``, and
    ``2026.10.10.233141`` for ``2026.10.10.233141.dev0``. So only the release numbers count.
    """
    try:
        return Version(module_version).release == Version(installed).release
    except InvalidVersion:
        return False


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def check_once(folder: Path, version: str, cookies: str, database_url: str) -> int:
    """The body of the check in the child process. Return the exit code of the child.

    ``folder`` holds the new release of ``version``. ``cookies`` is the cookie text from the standard input, or an
    empty text when the database stores no cookies. ``database_url`` names the database of the service, whose request
    budget the session check uses.
    """
    try:
        import yt_dlp
        from yt_dlp.version import __version__ as module_version
    except Exception as error:
        LOG.exception("yt-dlp does not import")
        return fail(WHAT, EXIT_YOUTUBE_ERROR, f"yt-dlp does not import: {_error_text(error)}")
    source = Path(yt_dlp.__file__).resolve().parent
    if not source.is_relative_to(folder.resolve()):
        return fail(WHAT, EXIT_YOUTUBE_ERROR, f"The child imported yt-dlp from {source}, not from {folder}")
    if not _same_release(module_version, version):
        return fail(
            WHAT, EXIT_YOUTUBE_ERROR, f"yt-dlp says that it is {module_version}, but the update installed {version}"
        )

    try:
        from youtube_automanager.youtube import account, public

        missing = account.missing_internals()
    except Exception as error:
        LOG.exception("The client of the account does not load with yt-dlp %s", module_version)
        return fail(WHAT, EXIT_YOUTUBE_ERROR, f"The client of the account does not load: {_error_text(error)}")
    if missing:
        return fail(WHAT, EXIT_YOUTUBE_ERROR, f"yt-dlp {module_version} lacks {', '.join(missing)}")

    try:
        entries = public.iter_channel_uploads(CHECK_CHANNEL_ID)
        first = next(entries, None)
        entries.close()
    except Exception as error:
        LOG.exception("The anonymous read of the uploads failed")
        return fail(WHAT, EXIT_YOUTUBE_ERROR, f"The anonymous read of the uploads failed: {_error_text(error)}")
    if first is None or not first.video_id:
        return fail(WHAT, EXIT_YOUTUBE_ERROR, f"The anonymous read of the uploads of {CHECK_CHANNEL_ID} gave no video")

    session = "not checked"
    if cookies.strip():
        engine = make_engine(database_url)
        try:
            budget = RequestBudget(make_session_factory(engine))
            account.AccountClient(cookies, budget=budget).check_session()
            session = "live"
        except account.NotLoggedInError:
            session = "logged out"
        except account.AccountError as error:
            # An AccountError never holds a cookie value.
            return fail(WHAT, EXIT_YOUTUBE_ERROR, f"The session check failed: {error}")
        except Exception as error:  # noqa: BLE001
            # The text of another error can hold a part of the cookie text, so only its kind and its trace go out.
            LOG.error(
                "The session check raised %s at:\n%s",
                type(error).__name__,
                "".join(traceback.format_tb(error.__traceback__)),
            )
            return fail(WHAT, EXIT_YOUTUBE_ERROR, f"The session check raised {type(error).__name__}")
        finally:
            engine.dispose()
    LOG.info("yt-dlp %s passed the check of a new release, and the session is %s", module_version, session)
    return succeed({"version": module_version, "session": session})


def stored_cookie_text(sessions: sessionmaker[Session], settings: Settings) -> str | None:
    """Return the text of the current cookie version, or None when the service cannot open stored cookies."""
    if settings.cookies_key is None:
        return None
    try:
        cipher = CookieCipher(settings.cookies_key.get_secret_value())
        with sessions() as session:
            stored = load_current(session, cipher)
    except CookieKeyError as error:
        LOG.warning("The check of the new yt-dlp runs without the session check: %s", error)
        return None
    return None if stored is None else stored.text


def check_release_in_child(
    runner: SyncRunner, sessions: sessionmaker[Session], settings: Settings, folder: Path, version: str
) -> str | None:
    """Check the release of ``version`` in ``folder`` in a child process of ``runner``. The caller holds the lock.

    Return None when the release passed, else the cause. The call blocks until the child ends.
    """
    cookies = stored_cookie_text(sessions, settings)
    end = runner.run_child_in_lock(check_command(folder, version), WHAT, CHECK_TIMEOUT_S, folder, cookies)
    try:
        line = child_result(end, NOUN, CHECK_TIMEOUT_S)
    except ChildError as error:
        return str(error)
    data = read_json(line)
    if not isinstance(data, dict) or not isinstance(data.get("session"), str):
        return "The check process ended without its result."
    LOG.info("yt-dlp %s passed the check of a new release, and the session is %s", version, data["session"])
    return None
