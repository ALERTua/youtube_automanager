"""The cookie session of the account in the child process of a run, of a session check or of a cookie upload.

A check reads the page config of the YouTube home page with the current cookie version. A dying session first answers
as an anonymous user and only later with HTTP 401, and ``AccountClient`` raises ``NotLoggedInError`` for both. When
the current version is dead, the newest good version gets a check of its own, and the work uses it when it lives.

The check of a cookie upload is the same check with the uploaded text. The text goes into the database only after
YouTube accepted its session, so a dead export never replaces the stored cookies.

The expiry dates in the cookie file do not show the end of a session, so they give only early warnings. After the
work, the updated cookies of the client go back to the database through the guarded write-back.
"""

import datetime
import enum
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager import notify
from youtube_automanager.account_cache import ChannelLike, PlaylistLike
from youtube_automanager.db.models import CookieOrigin, SessionState, utc_now
from youtube_automanager.settings import Settings
from youtube_automanager.youtube import session as cookie_store
from youtube_automanager.youtube.account import AccountClient, AccountError, NotLoggedInError
from youtube_automanager.youtube.cookie_text import earliest_login_expiry
from youtube_automanager.youtube.session import CookieCipher, CookieKeyError, StoredCookies

LOG = logging.getLogger(__name__)

# The days before the earliest expiry of the login cookies when a warning goes out, each once for the same date.
EXPIRY_WARNING_DAYS = (3, 1, 0)
DEAD_SESSION_TEXT = (
    "YouTube does not accept the stored cookies any more. "
    "Export new YouTube cookies from your browser and upload them to the service."
)
UPLOAD_REFUSED_TEXT = (
    "YouTube does not accept the uploaded cookies, so the service did not store them and keeps the stored cookies. "
    "Export new YouTube cookies from your browser and upload them to the service."
)


class AccountUnavailable(Exception):
    """The work needs the account, but the service cannot use it. The text names the cause and what the user can do."""


class SessionDead(AccountUnavailable):
    """YouTube refused each stored cookie version that the service could try."""


class ClientLike(Protocol):
    """The part of ``AccountClient`` that a run, a check, a refresh of the account data and a subscription change use."""

    def check_session(self) -> None: ...

    def use_cookies(self, cookies: str) -> None: ...

    def cookies_text(self) -> str: ...

    def subscribed_channels(self) -> Iterable[ChannelLike]: ...

    def playlists(self) -> Iterable[PlaylistLike]: ...

    def iter_subscription_feed(self) -> Iterable[Any]: ...

    def add_video(self, playlist_id: str, video_id: str) -> None: ...

    def subscribe(self, channel_id: str) -> None: ...

    def unsubscribe(self, channel_id: str) -> None: ...


class CheckOutcome(enum.StrEnum):
    ALIVE = "alive"
    DEAD = "dead"
    # No cookies are stored, or the cookie key does not open them.
    UNAVAILABLE = "unavailable"
    # The check got no answer that tells whether the session lives.
    FAILED = "failed"


@dataclass(frozen=True)
class CheckResult:
    outcome: CheckOutcome
    detail: str | None = None


def load_stored(sessions: sessionmaker[Session], settings: Settings) -> tuple[CookieCipher, StoredCookies]:
    """Return the cipher and the current cookie version. Raise AccountUnavailable with the cause."""
    if settings.cookies_key is None:
        raise AccountUnavailable(
            "The service needs the YouTube account, but COOKIES_KEY is not set, so the service cannot open the stored "
            "cookies. Set COOKIES_KEY and upload the YouTube cookies."
        )
    try:
        cipher = CookieCipher(settings.cookies_key.get_secret_value())
        with sessions() as session:
            stored = cookie_store.load_current(session, cipher)
            account = cookie_store.account_row(session)
            has_cookies = account is not None and account.cookies is not None
    except CookieKeyError as error:
        raise AccountUnavailable(
            f"The service needs the YouTube account, but the cookies cannot be opened: {error}"
        ) from None
    if stored is None:
        if has_cookies:
            raise SessionDead(DEAD_SESSION_TEXT)
        raise AccountUnavailable(
            "The service needs the YouTube account, but no YouTube cookies are stored. Upload the YouTube cookies."
        )
    return cipher, stored


def _upload_cipher(settings: Settings) -> CookieCipher:
    """Return the cipher of a new upload. Raise AccountUnavailable with the cause, which never holds the key."""
    if settings.cookies_key is None:
        raise AccountUnavailable(
            "The service cannot store the uploaded cookies, because COOKIES_KEY is not set. Set COOKIES_KEY and upload "
            "the cookies again."
        )
    try:
        return CookieCipher(settings.cookies_key.get_secret_value())
    except CookieKeyError as error:
        raise AccountUnavailable(f"The service cannot store the uploaded cookies: {error}") from None


def _warning_threshold(days_left: int | None) -> int | None:
    """Return the smallest warning day that ``days_left`` reached, or None. A date in the past gets no warning."""
    if days_left is None or days_left < 0:
        return None
    return min((days for days in EXPIRY_WARNING_DAYS if days_left <= days), default=None)


class AccountSession:
    """The account of one run or one check.

    ``check`` checks the session of the current cookie version, and ``open`` gives the checked client to the work.
    ``check_upload`` checks an uploaded cookie text instead, and stores it when it lives. ``died`` takes a session that
    YouTube refused during the work, and ``write_back`` saves the updated cookies.
    ``send`` sends a notification, and ``make_client`` builds the account client from a cookie text.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        settings: Settings,
        *,
        send: Callable[[notify.Notification], object],
        make_client: Callable[[str], ClientLike] | None = None,
        clock: Callable[[], datetime.datetime] = utc_now,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._zone = ZoneInfo(settings.tz)
        self._send = send
        self._make_client = make_client or AccountClient
        self._clock = clock
        self._cipher: CookieCipher | None = None
        self._client: ClientLike | None = None
        self._version_id: int | None = None
        self._unavailable: AccountUnavailable | None = None
        self._alive = False

    def check(self) -> CheckResult:
        """Check the session of the current cookie version, and go back to the newest good version when it is dead.

        Write the state of the session into the ``account`` row and send the session events. A check without stored
        cookies sends no request.
        """
        try:
            self._cipher, stored = load_stored(self._sessions, self._settings)
            try:
                self._client = self._make_client(stored.text)
            except ValueError as error:
                raise AccountUnavailable(
                    f"The service needs the YouTube account, but the stored cookies are not valid: {error}. "
                    "Upload the YouTube cookies again."
                ) from None
        except AccountUnavailable as error:
            self._unavailable = error
            outcome = CheckOutcome.DEAD if isinstance(error, SessionDead) else CheckOutcome.UNAVAILABLE
            return CheckResult(outcome, str(error))
        self._version_id = stored.version_id
        return self._verify(stored.text, roll_back=True)

    def check_upload(self, text: str) -> CheckResult:
        """Check the session of an uploaded cookie text, and store the text only when YouTube accepts the session.

        The check is the check of the stored cookies, with a client of ``text``. A text that YouTube accepts replaces
        the stored cookies and their versions as a new upload, and its version and the account get the state of a
        passed check. When YouTube refuses the session, or the check gives no clear answer, nothing changes: the stored
        cookies, their versions and the account stay as they were, and no session event goes out, because the stored
        session did not die. A text that is not a cookie file, or a missing cookie key, sends no request.
        """
        try:
            cipher = _upload_cipher(self._settings)
            try:
                client = self._make_client(text)
            except ValueError as error:
                raise AccountUnavailable(f"The uploaded cookies are not valid: {error}") from None
        except AccountUnavailable as error:
            return CheckResult(CheckOutcome.UNAVAILABLE, str(error))
        try:
            client.check_session()
        except NotLoggedInError as error:
            LOG.warning("YouTube refused the uploaded cookies, so the service keeps the stored cookies: %s", error)
            return CheckResult(CheckOutcome.DEAD, UPLOAD_REFUSED_TEXT)
        except AccountError as error:
            LOG.warning("The check of the uploaded cookies failed, so the service stores nothing: %s", error)
            return CheckResult(CheckOutcome.FAILED, f"The check of the uploaded cookies failed: {error}")
        now = self._clock()
        with self._sessions.begin() as session:
            self._version_id = cookie_store.save_cookies(session, cipher, text, now=now).id
            warning = self._write_passed(session, text, now)
        LOG.info("YouTube accepted the uploaded cookies, and they are stored as cookie version %s", self._version_id)
        self._cipher, self._client, self._alive = cipher, client, True
        if warning is not None:
            self._notify(warning)
        return CheckResult(CheckOutcome.ALIVE)

    def open(self) -> ClientLike:
        """Return the client of the account. Raise AccountUnavailable when the work must go on without the account."""
        if self._unavailable is not None:
            raise AccountUnavailable(str(self._unavailable))
        if self._client is None:
            raise AccountUnavailable("The service did not check the session of the YouTube account")
        return self._client

    def died(self, error: NotLoggedInError) -> None:
        """Take a session that YouTube refused during the work: the version is dead, and so is the session.

        The next check tries the newest good version, so the work itself does not wait for one more check.
        """
        if self._version_id is None or self._unavailable is not None:
            return
        LOG.warning("YouTube refused cookie version %s during the work: %s", self._version_id, error)
        self._retire()
        self._dead()

    def write_back(self) -> tuple[str, ...]:
        """Save the new values of the known cookies of the client as a new version, when the session lives.

        Return the names of the cookies with a new value.
        """
        if not self._alive or self._client is None or self._cipher is None or self._version_id is None:
            return ()
        candidate = self._client.cookies_text()
        with self._sessions.begin() as session:
            changed = cookie_store.write_back(
                session, self._cipher, self._version_id, candidate, CookieOrigin.WRITE_BACK, now=self._clock()
            )
        if changed:
            LOG.info("Saved new values of %d cookie(s) as a new cookie version: %s", len(changed), ", ".join(changed))
        return changed

    def _verify(self, text: str, *, roll_back: bool) -> CheckResult:
        try:
            self._client.check_session()
        except NotLoggedInError as error:
            LOG.warning("YouTube refused cookie version %s: %s", self._version_id, error)
            good = self._retire()
            if good is None or not roll_back:
                return self._dead()
            LOG.info("Going back to cookie version %s, the newest version that passed a check", good.version_id)
            self._version_id = good.version_id
            self._client.use_cookies(good.text)
            return self._verify(good.text, roll_back=False)
        except AccountError as error:
            LOG.warning("The session check failed: %s", error)
            return CheckResult(CheckOutcome.FAILED, f"The session check failed: {error}")
        self._passed(text)
        return CheckResult(CheckOutcome.ALIVE)

    def _retire(self) -> StoredCookies | None:
        with self._sessions.begin() as session:
            good = cookie_store.retire(session, self._version_id, self._clock())
            return None if good is None else StoredCookies(good.id, self._cipher.decrypt(good.cookies))

    def _dead(self) -> CheckResult:
        """Write the session as dead. Send the event only when the session turns dead."""
        with self._sessions.begin() as session:
            account = cookie_store.account_row(session)
            turned_dead = account is not None and account.session_state is not SessionState.DEAD
            if account is not None:
                account.session_state = SessionState.DEAD
                account.last_checked_at = self._clock()
        self._alive = False
        self._unavailable = SessionDead(DEAD_SESSION_TEXT)
        if turned_dead:
            self._notify(notify.cookie_session_dead())
        return CheckResult(CheckOutcome.DEAD, DEAD_SESSION_TEXT)

    def _passed(self, text: str) -> None:
        """Write the version as good and the session as live, and send an expiry warning when one is due."""
        with self._sessions.begin() as session:
            warning = self._write_passed(session, text, self._clock())
        self._alive = True
        if warning is not None:
            self._notify(warning)

    def _write_passed(self, session: Session, text: str, now: datetime.datetime) -> notify.Notification | None:
        """Write that the current version passed a check at ``now``. Return the expiry warning when one is due."""
        expiry = earliest_login_expiry(text)
        days_left = None
        if expiry is not None:
            days_left = (expiry.astimezone(self._zone).date() - now.astimezone(self._zone).date()).days
        cookie_store.mark_good(session, self._version_id, now)
        account = cookie_store.account_row(session)
        if account is None:
            return None
        cookie_store.set_earliest_expiry(account, expiry)
        expiring = days_left is not None and days_left <= EXPIRY_WARNING_DAYS[0]
        account.session_state = SessionState.EXPIRING if expiring else SessionState.OK
        account.last_checked_at = now
        threshold = _warning_threshold(days_left)
        if threshold is None or (account.expiry_warning_days is not None and threshold >= account.expiry_warning_days):
            return None
        # One warning for each check, also when the check comes after two warning days at once.
        account.expiry_warning_days = threshold
        return notify.cookie_expiry(days_left, expiry.astimezone(self._zone))

    def _notify(self, notification: notify.Notification) -> None:
        try:
            self._send(notification)
        except Exception as error:  # noqa: BLE001
            # A failed notification must never fail the work.
            LOG.warning("Cannot send %s: %s", notification.event.value, type(error).__name__)
