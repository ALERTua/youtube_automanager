"""The text of a Netscape cookie file without yt-dlp: the check of its lines, the expiry date of the login, and the
guarded merge of updated cookies.

A function here never puts a cookie value into an error text or a log entry. An error names only a line number.
"""

import datetime
import http.cookiejar
import re
from dataclasses import dataclass

NETSCAPE_HEADER = re.compile(r"#( Netscape)? HTTP Cookie File")
HEADER_LINE = "# Netscape HTTP Cookie File"
HTTPONLY_PREFIX = "#HttpOnly_"
_EXPIRES = re.compile(r"[0-9]+(?:\.[0-9]+)?")
# A tab or a line break in a value would break the line of the cookie file.
_UNSAFE = re.compile(r"[\t\r\n]")
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)

# The cookies of the Google sign-in that a logged-in YouTube session needs. A fresh export also holds cookies that live
# only minutes, for example GPS and CONSISTENCY, and ST-* cookies that are already expired. So only the login cookies
# give the expiry date of the login.
LOGIN_COOKIES = frozenset(
    {
        "SID",
        "HSID",
        "SSID",
        "APISID",
        "SAPISID",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "__Secure-1PAPISID",
        "__Secure-3PAPISID",
        "__Secure-1PSIDTS",
        "__Secure-3PSIDTS",
        "SIDCC",
        "__Secure-1PSIDCC",
        "__Secure-3PSIDCC",
        "LOGIN_INFO",
    }
)
# yt-dlp writes its own language and time zone into PREF when it starts, so a new value of PREF is no news from YouTube.
NOT_WRITTEN_BACK = frozenset({"PREF"})


@dataclass(frozen=True)
class CookieLine:
    """One cookie of a Netscape cookie file. ``expires`` is 0 for a session cookie."""

    domain: str
    path: str
    secure: bool
    expires: int
    name: str
    value: str
    http_only: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return self.domain, self.path, self.name


@dataclass(frozen=True)
class Merge:
    """The text after a guarded merge, and the names of the cookies that got a new value, in the order of the text."""

    text: str
    changed: tuple[str, ...]


def checked_lines(text: str) -> list[str]:
    """Split the text of a Netscape cookie file into lines. Refuse a text with a line that yt-dlp would skip or refuse.

    yt-dlp writes a skipped line with its cookie value to stderr, and Python puts a refused line into its error
    message. So this check comes first, and its error names only the line number. The lines lose their line ends, so a
    text with CRLF line ends gives no value with a trailing CR.
    """
    lines = text.splitlines()
    if not lines or not NETSCAPE_HEADER.match(lines[0]):
        raise ValueError("the cookie text does not start with the header line of a Netscape cookie file")
    for number, line in enumerate(lines[1:], start=2):
        line = line.removeprefix(HTTPONLY_PREFIX)
        if line.startswith("#") or not line.strip():
            continue
        fields = line.split("\t")
        if (
            len(fields) != 7
            or (fields[4] and not _EXPIRES.fullmatch(fields[4]))
            or (fields[1] == "TRUE") != fields[0].startswith(".")
        ):
            raise ValueError(f"line {number} of the cookie text is not a line of a Netscape cookie file")
    return lines


def _cookie(line: str) -> CookieLine | None:
    """Return the cookie of a line, or None for a comment or an empty line. A line must have 7 fields."""
    http_only = line.startswith(HTTPONLY_PREFIX)
    body = line.removeprefix(HTTPONLY_PREFIX)
    if body.startswith("#") or not body.strip():
        return None
    domain, _, path, secure, expires, name, value = body.split("\t")
    return CookieLine(domain, path, secure == "TRUE", int(float(expires)) if expires else 0, name, value, http_only)


def cookies(text: str) -> list[CookieLine]:
    """Return the cookies of the text of a Netscape cookie file, in the order of the text."""
    return [cookie for line in checked_lines(text)[1:] if (cookie := _cookie(line)) is not None]


def _from_epoch(seconds: int) -> datetime.datetime | None:
    try:
        return _EPOCH + datetime.timedelta(seconds=seconds)
    except OverflowError:
        return None


def earliest_login_expiry(text: str) -> datetime.datetime | None:
    """Return the earliest expiry date of the login cookies, or None when the text gives no such date.

    The date gives only an early warning, so a line that is not a cookie line counts as a line without a date.
    """
    dates = []
    for line in text.splitlines()[1:]:
        try:
            cookie = _cookie(line)
        except ValueError:
            continue
        if cookie is not None and cookie.name in LOGIN_COOKIES and cookie.expires > 0:
            date = _from_epoch(cookie.expires)
            if date is not None:
                dates.append(date)
    return min(dates, default=None)


def merge_known(stored: str, candidate: str) -> Merge:
    """Take from ``candidate`` only the new value of each cookie that ``stored`` already has.

    A cookie that ``candidate`` lacks stays as it is, so a removal never goes through. A cookie that only ``candidate``
    has stays out. PREF stays out, and so does an empty value. A new value brings its expiry date with it when that
    date is later, because ``http.cookiejar`` stops to send a cookie after its stored date. A new expiry date without a
    new value stays out. The lines of ``stored`` keep their order, their comments and their HttpOnly marks. Raise
    ValueError when a text is not a Netscape cookie file.
    """
    lines = checked_lines(stored)
    updates = {cookie.key: cookie for cookie in cookies(candidate)}
    merged = [lines[0]]
    changed = []
    for line in lines[1:]:
        cookie = _cookie(line)
        if cookie is None or cookie.name in NOT_WRITTEN_BACK:
            merged.append(line)
            continue
        update = updates.get(cookie.key)
        if update is None or not update.value or update.value == cookie.value:
            merged.append(line)
            continue
        fields = line.removeprefix(HTTPONLY_PREFIX).split("\t")
        if cookie.expires and update.expires > cookie.expires:
            fields[4] = str(update.expires)
        fields[6] = update.value
        merged.append((HTTPONLY_PREFIX if cookie.http_only else "") + "\t".join(fields))
        changed.append(cookie.name)
    if not changed:
        return Merge(stored, ())
    return Merge("\n".join(merged) + "\n", tuple(changed))


def to_cookiejar(text: str) -> http.cookiejar.CookieJar:
    """Load the text of a Netscape cookie file into a standard cookie jar, also the expired and the session cookies."""
    jar = http.cookiejar.CookieJar()
    for cookie in cookies(text):
        initial_dot = cookie.domain.startswith(".")
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=cookie.name,
                value=cookie.value,
                port=None,
                port_specified=False,
                domain=cookie.domain,
                domain_specified=initial_dot,
                domain_initial_dot=initial_dot,
                path=cookie.path,
                path_specified=False,
                secure=cookie.secure,
                expires=cookie.expires or None,
                discard=not cookie.expires,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": ""} if cookie.http_only else {},
            )
        )
    return jar


def from_cookiejar(jar: http.cookiejar.CookieJar) -> str:
    """Write the cookies of a standard cookie jar as the text of a Netscape cookie file.

    A cookie without a value, or with a tab or a line break in its name or value, stays out.
    """
    lines = [HEADER_LINE]
    for cookie in jar:
        if not cookie.name or cookie.value is None or _UNSAFE.search(cookie.name) or _UNSAFE.search(cookie.value):
            continue
        initial_dot = cookie.domain.startswith(".")
        lines.append(
            "\t".join(
                (
                    cookie.domain,
                    "TRUE" if initial_dot else "FALSE",
                    cookie.path,
                    "TRUE" if cookie.secure else "FALSE",
                    str(int(cookie.expires)) if cookie.expires else "0",
                    cookie.name,
                    cookie.value,
                )
            )
        )
    return "\n".join(lines) + "\n"
