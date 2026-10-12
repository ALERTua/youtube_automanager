"""The upload of the YouTube cookies on the settings page: the check of the text, and the store of a good text.

A cookie text goes into the database only when it is a Netscape cookie file and holds each login cookie of
``.youtube.com``. A check names only a line number or the names of the missing cookies, never a cookie value.

The store holds the sync lock. A session that YouTube refuses during a run marks each newer cookie version dead, so an
upload during a run, a session check, a cookie rotation or a short child could die with the old session. This module
imports no NiceGUI, so its functions have tests of their own.
"""

import datetime

from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.runner import SyncRunner
from youtube_automanager.youtube.cookie_text import LOGIN_COOKIES, cookies
from youtube_automanager.youtube.session import CookieCipher, save_cookies

# The domain of the login cookies that the service needs.
YOUTUBE_DOMAIN = ".youtube.com"
# A cookie file of a YouTube export has some kilobytes, so a larger file is not one.
MAX_FILE_BYTES = 1024 * 1024
# The command that prints a new cookie key. The key goes into the environment variable COOKIES_KEY.
KEY_COMMAND = 'python -c "from youtube_automanager.youtube.session import generate_key; print(generate_key())"'
COMPOSE_KEY_COMMAND = f"docker compose run --rm youtube-automanager {KEY_COMMAND}"


def normalized(text: str) -> str:
    """Return the text without a byte order mark and blank lines in front, with one line end at the end of each line.

    Return an empty text when nothing else is left.
    """
    lines = text.lstrip("﻿").lstrip().splitlines()
    return "\n".join(lines) + "\n" if lines else ""


def missing_login_cookies(text: str) -> tuple[str, ...]:
    """Return the names of the login cookies that ``text`` lacks on ``.youtube.com``, sorted by name.

    An empty tuple means that each login cookie is there. Raise ValueError when the text is not a Netscape cookie file.
    The text of the error names only a line number.
    """
    present = {cookie.name for cookie in cookies(text) if cookie.domain.casefold() == YOUTUBE_DOMAIN}
    return tuple(sorted(LOGIN_COOKIES - present))


def store_upload(
    sessions: sessionmaker[Session],
    runner: SyncRunner,
    cipher: CookieCipher,
    text: str,
    *,
    now: datetime.datetime | None = None,
) -> None:
    """Store ``text`` as a new upload of the cookies. This call blocks.

    The state of the session is unknown until the next session check. Raise ``RunAlreadyActive`` and store nothing
    while a run, a session check, a cookie rotation or a short child holds the sync lock.
    """
    with runner.lock_held(timeout=0), sessions.begin() as session:
        save_cookies(session, cipher, text, now=now)
