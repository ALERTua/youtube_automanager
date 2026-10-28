"""The rotation of the cookie session in the process of the service, as an open YouTube tab does it.

A browser tab renews the session of ``.youtube.com`` with two requests to ``accounts.youtube.com``: a GET of the
rotation page for a rotation id, and a POST of that id. The answer of the POST holds new values of the PSIDTS and
SIDCC cookies, and the interval until the next rotation. A second rotation within 1 to 10 minutes can give HTTP 429,
so the service never rotates more often than the answer allows, and never more often than each ``INTERVAL_S``.

The rotation is the setting ROTATION_KEY, on by default. It never runs while a sync run or a session check holds the
sync lock, and it saves the new values through the guarded write-back. Each of its two requests takes a token of the
request budget that the requests of yt-dlp with cookies share. While the breaker is open, the rotation waits for its
end. HTTP 429 opens it. It uses only the standard library, because the process of the service never imports yt-dlp.
"""

import datetime
import http.client
import json
import logging
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence

from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import CookieOrigin, SessionState, Setting, utc_now
from youtube_automanager.pacing import BreakerOpen, RequestBudget
from youtube_automanager.runner import SYNC_LOCK, RunAlreadyActive, sync_lock_held
from youtube_automanager.settings import Settings
from youtube_automanager.youtube.cookie_text import from_cookiejar, to_cookiejar
from youtube_automanager.youtube.session import CookieCipher, CookieKeyError, account_row, load_current, write_back
from youtube_automanager.youtube.user_agent import USER_AGENT

LOG = logging.getLogger(__name__)

# The setting that turns the rotation on, and its default.
ROTATION_KEY = "cookie_rotation"
ROTATION_DEFAULT = True
# The time before which the next rotation must not come. It survives a restart of the service.
NEXT_ROTATION_KEY = "cookie_rotation_next_at"

PAGE_URL = "https://accounts.youtube.com/RotateCookiesPage?origin=https://www.youtube.com&yt_pid=1"
ROTATE_URL = "https://accounts.youtube.com/RotateCookies"
_REFERER = "https://www.youtube.com/"

# The interval that the answer gave in the measurement. The service never rotates more often.
INTERVAL_S = 600.0
# The wait before the next try when a run holds the sync lock.
RETRY_S = 60.0
# The longest wait after failed rotations.
MAX_DELAY_S = 3600.0
TIMEOUT_S = 30.0
_ROTATION_ID = re.compile(r"init\('(-?\d+)'")


def rotation_enabled(session: Session) -> bool:
    """Return the setting of the rotation. Without a stored value, the rotation is on.

    A stored value that is not true or false keeps the rotation off.
    """
    row = session.get(Setting, ROTATION_KEY)
    if row is None:
        return ROTATION_DEFAULT
    if isinstance(row.value, bool):
        return row.value
    LOG.warning("The setting %r is not true or false, so the cookie rotation stays off", ROTATION_KEY)
    return False


def _stored_time(row: Setting | None) -> datetime.datetime | None:
    """Return the time of a setting row. A value that is not an ISO time with a time zone counts as no time."""
    if row is None or not isinstance(row.value, str):
        return None
    try:
        value = datetime.datetime.fromisoformat(row.value)
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def _next_interval(answer: str) -> float | None:
    """Return the interval in seconds from the answer of the POST: JSON after a prefix of 5 characters."""
    try:
        value = json.loads(answer[5:])[0][1]
    except ValueError, IndexError, TypeError, KeyError:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return float(value)


class CookieRotation:
    """Rotates the session of the stored cookies, one turn at a time.

    ``handlers`` are more urllib handlers for the requests, for example a fake transport in a test. ``lock`` is the
    sync lock of the runs. ``budget`` is the request budget of the requests with cookies. Without it, the rotation
    uses the budget on the database of ``sessions``.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        settings: Settings,
        *,
        lock: threading.Lock = SYNC_LOCK,
        handlers: Sequence[urllib.request.BaseHandler] = (),
        clock: Callable[[], datetime.datetime] = utc_now,
        budget: RequestBudget | None = None,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._lock = lock
        self._handlers = tuple(handlers)
        self._clock = clock
        self._budget = budget or RequestBudget(sessions, default_timezone=settings.tz)
        self._failures = 0

    def turn(self) -> float:
        """Rotate the session once, when the setting is on and the time of the next rotation has come.

        Skip the turn without a wait when a run holds the sync lock. Return the seconds until the next turn.
        """
        now = self._clock()
        with self._sessions() as session:
            enabled = rotation_enabled(session)
            next_at = _stored_time(session.get(Setting, NEXT_ROTATION_KEY))
        if not enabled:
            return INTERVAL_S
        if next_at is not None and now < next_at:
            return max((next_at - now).total_seconds(), 1.0)
        try:
            with sync_lock_held(timeout=0, lock=self._lock):
                delay_s = self._rotate()
        except RunAlreadyActive:
            LOG.info("The cookie rotation waits, because a run, a session check or a yt-dlp update is active")
            return RETRY_S
        if delay_s is None:
            return INTERVAL_S
        with self._sessions.begin() as session:
            next_at = self._clock() + datetime.timedelta(seconds=delay_s)
            row = session.get(Setting, NEXT_ROTATION_KEY)
            if row is None:
                session.add(Setting(key=NEXT_ROTATION_KEY, value=next_at.isoformat()))
            else:
                row.value = next_at.isoformat()
        return delay_s

    def _rotate(self) -> float | None:
        """Send the two requests and save the new cookie values. Return the delay of the next rotation.

        Return None when there is nothing to rotate: no cookie key, no stored cookies, or a dead session.
        """
        if self._settings.cookies_key is None:
            return None
        try:
            cipher = CookieCipher(self._settings.cookies_key.get_secret_value())
            with self._sessions() as session:
                account = account_row(session)
                if account is None or account.session_state is SessionState.DEAD:
                    return None
                stored = load_current(session, cipher)
        except CookieKeyError as error:
            LOG.warning("The cookie rotation cannot open the stored cookies: %s", error)
            return None
        if stored is None:
            return None
        try:
            jar = to_cookiejar(stored.text)
        except ValueError as error:
            LOG.warning("The cookie rotation cannot read the stored cookies: %s", error)
            return None
        # The cookie processor sends the cookies of the jar and takes each new cookie of an answer into it.
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), *self._handlers)
        try:
            status, page = self._send(
                opener, urllib.request.Request(PAGE_URL, headers={"User-Agent": USER_AGENT, "Referer": _REFERER})
            )
            found = _ROTATION_ID.search(page) if status == 200 else None
            if found is None:
                LOG.warning("The rotation page answered HTTP %s without a rotation id", status)
                return self._failed()
            status, answer = self._send(
                opener,
                urllib.request.Request(
                    ROTATE_URL,
                    data=json.dumps([None, found.group(1), 1]).encode("ascii"),
                    method="POST",
                    headers={
                        "User-Agent": USER_AGENT,
                        "Content-Type": "application/json",
                        "Origin": "https://accounts.youtube.com",
                        "Referer": PAGE_URL,
                        "Sec-Fetch-Site": "same-origin",
                        "Sec-Fetch-Mode": "same-origin",
                    },
                ),
            )
        except BreakerOpen as breaker_open:
            LOG.info("The cookie rotation waits: %s", breaker_open)
            return max((breaker_open.pause.until - self._clock()).total_seconds(), RETRY_S)
        except (OSError, http.client.HTTPException) as error:
            # The text of the error can hold a URL, but never a cookie. Only its type goes into the log.
            LOG.warning("The cookie rotation failed: %s", type(error).__name__)
            return self._failed()
        if status != 200:
            LOG.warning("The cookie rotation answered HTTP %s", status)
            return self._failed()
        self._failures = 0
        self._budget.end_if_answered()
        interval = max(_next_interval(answer) or INTERVAL_S, INTERVAL_S)
        with self._sessions.begin() as session:
            changed = write_back(
                session, cipher, stored.version_id, from_cookiejar(jar), CookieOrigin.ROTATION, now=self._clock()
            )
        LOG.info("The cookie rotation saved new values of %s", ", ".join(changed) or "no cookie")
        return interval

    def _failed(self) -> float:
        """Count a failed rotation. Each failure doubles the wait, up to MAX_DELAY_S."""
        self._failures += 1
        return min(INTERVAL_S * 2**self._failures, MAX_DELAY_S)

    def _send(self, opener: urllib.request.OpenerDirector, request: urllib.request.Request) -> tuple[int, str]:
        """Send one request with a token of the budget. Return the HTTP status and the body text, also for an HTTP
        error.

        The budget keeps the end of the answer also after an error answer or a failed connection, and it gets the
        status of the answer for its breaker. Raise ``BreakerOpen`` without a request while the breaker is open.
        """
        self._budget.before_request()
        status = None
        try:
            with opener.open(request, timeout=TIMEOUT_S) as response:
                status = response.status
                return status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            status = error.code
            with error:
                return status, error.read().decode("utf-8", "replace")
        finally:
            self._budget.after_answer(status)
