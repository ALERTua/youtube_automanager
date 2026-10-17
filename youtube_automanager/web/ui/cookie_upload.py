"""The upload of the YouTube cookies on the settings page: the checks of the text before any request to YouTube.

A cookie text goes to the check with YouTube only when it is a Netscape cookie file, holds each login cookie of
``.youtube.com``, and its login cookies did not expire yet. A check names only a line number, the names of the missing
cookies or a date, never a cookie value.

The check with YouTube and the store run in a child process, through ``cookie_upload_check``. This module imports no
NiceGUI, so its functions have tests of their own.
"""

import datetime

from youtube_automanager.youtube.cookie_text import LOGIN_COOKIES, cookies, earliest_login_expiry

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


def login_expired_at(text: str, now: datetime.datetime) -> datetime.datetime | None:
    """Return the earliest expiry of the login cookies of ``text`` when it is ``now`` or earlier, else None.

    A cookie whose expiry is ``now`` is expired, as ``http.cookiejar`` counts it, so YouTube would not get it.
    """
    expiry = earliest_login_expiry(text)
    return expiry if expiry is not None and expiry <= now else None
