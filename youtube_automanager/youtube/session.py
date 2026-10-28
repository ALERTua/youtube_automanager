"""Keeps the cookies of the YouTube account in the database, encrypted with a key that only the environment holds.

A copy of the database without the key holds no cookie value.

Each stored state of the cookies is a row of ``cookie_version``. An upload starts a new chain of versions. A
write-back of updated cookies adds a version, and only new values of the cookies that the current version already has
go into it. The current version is the newest version that is not dead, and ``account.cookies`` holds a copy of it.
A version that passed a session check is good. When a version dies, each newer version goes with it, and the newest
good version becomes the current one again.

The module uses no yt-dlp, so the process of the service can use it.
"""

import datetime
import logging
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from youtube_automanager.db.models import (
    Account,
    CookieOrigin,
    CookieVersion,
    CookieVersionState,
    SessionState,
    utc_now,
)
from youtube_automanager.youtube.cookie_text import earliest_login_expiry, merge_known

LOG = logging.getLogger(__name__)

# The number of the newest versions that stay. The current version and the newest good version also stay.
VERSIONS_KEPT = 10


class CookieKeyError(Exception):
    """The cookie key is missing or not valid, or it does not open the stored cookies."""


def generate_key() -> str:
    """Return a new random cookie key in the form that the environment variable takes."""
    return Fernet.generate_key().decode("ascii")


class CookieCipher:
    """Encrypts the cookie text with the first key and decrypts it with any of the keys.

    ``keys`` holds one key, or several keys separated by commas with the newest first. An old key then still opens
    the stored cookies, and the next save encrypts them with the newest key.
    """

    def __init__(self, keys: str):
        parts = [part.strip() for part in keys.split(",") if part.strip()]
        if not parts:
            raise CookieKeyError("No cookie key is set")
        try:
            self._fernet = MultiFernet([Fernet(part) for part in parts])
        except ValueError:
            # The error text never repeats the key.
            raise CookieKeyError("A cookie key is not valid: it must be 32 url-safe base64-encoded bytes") from None

    def encrypt(self, cookies_text: str) -> bytes:
        return self._fernet.encrypt(cookies_text.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except InvalidToken:
            raise CookieKeyError("The cookie key does not open the stored cookies. Upload the cookies again.") from None


@dataclass(frozen=True)
class StoredCookies:
    """The text of a cookie version and the id of that version."""

    version_id: int
    text: str


def account_row(session: Session) -> Account | None:
    """The service has one account, so the first row holds it."""
    return session.scalars(select(Account).order_by(Account.id).limit(1)).first()


def save_cookies(
    session: Session, cipher: CookieCipher, cookies_text: str, *, now: datetime.datetime | None = None
) -> CookieVersion:
    """Store an upload of the Netscape cookie text as the first version of a new chain. The caller commits the session.

    The versions of an older upload belong to another export, maybe of another account, so they go. The state of the
    session and its events start again: the state is unknown until the next check, and the expiry warnings come again.
    """
    account = account_row(session) or Account()
    session.add(account)
    session.execute(delete(CookieVersion))
    version = CookieVersion(
        created_at=now or utc_now(),
        origin=CookieOrigin.UPLOAD,
        state=CookieVersionState.UNCHECKED,
        cookies=cipher.encrypt(cookies_text),
    )
    session.add(version)
    session.flush()
    account.cookies = version.cookies
    account.session_state = SessionState.UNKNOWN
    account.last_checked_at = None
    account.earliest_cookie_expiry = earliest_login_expiry(cookies_text)
    account.expiry_warning_days = None
    return version


def current_version(session: Session) -> CookieVersion | None:
    """Return the current cookie version: the newest version that is not dead. None when no such version exists."""
    return session.scalars(
        select(CookieVersion)
        .where(CookieVersion.state != CookieVersionState.DEAD)
        .order_by(CookieVersion.id.desc())
        .limit(1)
    ).first()


def load_current(session: Session, cipher: CookieCipher) -> StoredCookies | None:
    """Return the text of the current cookie version, or None when no version that is not dead exists."""
    version = current_version(session)
    if version is None:
        return None
    return StoredCookies(version.id, cipher.decrypt(version.cookies))


def _newest_good(session: Session, before_id: int | None = None) -> CookieVersion | None:
    query = select(CookieVersion).where(CookieVersion.state == CookieVersionState.GOOD)
    if before_id is not None:
        query = query.where(CookieVersion.id < before_id)
    return session.scalars(query.order_by(CookieVersion.id.desc()).limit(1)).first()


def set_earliest_expiry(account: Account, expiry: datetime.datetime | None) -> None:
    """Write the earliest expiry of the login cookies. A new date gets the expiry warnings again."""
    if account.earliest_cookie_expiry != expiry:
        account.earliest_cookie_expiry = expiry
        account.expiry_warning_days = None


def mark_good(session: Session, version_id: int, now: datetime.datetime) -> None:
    """Write that the version passed a session check at ``now``."""
    version = session.get(CookieVersion, version_id)
    if version is not None:
        version.state = CookieVersionState.GOOD
        version.checked_at = now


def retire(session: Session, version_id: int, now: datetime.datetime) -> CookieVersion | None:
    """Write the version as dead and go back to the newest good version before it. Return that version, or None.

    Each version that is newer than that good version goes too, so the newest version that is not dead is the good
    version. Without a good version, no version stays.
    """
    version = session.get(CookieVersion, version_id)
    if version is not None:
        version.state = CookieVersionState.DEAD
        version.checked_at = now
    good = _newest_good(session, before_id=version_id)
    session.execute(
        update(CookieVersion)
        .where(CookieVersion.id > (good.id if good is not None else 0), CookieVersion.state != CookieVersionState.DEAD)
        .values(state=CookieVersionState.DEAD)
    )
    if good is not None:
        account = account_row(session)
        if account is not None:
            account.cookies = good.cookies
    return good


def write_back(
    session: Session,
    cipher: CookieCipher,
    base_version_id: int,
    candidate_text: str,
    origin: CookieOrigin,
    *,
    now: datetime.datetime,
) -> tuple[str, ...]:
    """Store the new values of the known cookies of ``candidate_text`` as a new version. The caller commits the session.

    ``base_version_id`` is the version that the work started from. When another version became current in the
    meantime, for example a new upload, nothing is stored. Return the names of the cookies with a new value, or an
    empty tuple when nothing was stored.
    """
    current = current_version(session)
    if current is None or current.id != base_version_id:
        LOG.warning("The stored cookies changed during the work, so the service does not save the updated cookies")
        return ()
    merged = merge_known(cipher.decrypt(current.cookies), candidate_text)
    if not merged.changed:
        return ()
    version = CookieVersion(
        created_at=now, origin=origin, state=CookieVersionState.UNCHECKED, cookies=cipher.encrypt(merged.text)
    )
    session.add(version)
    session.flush()
    account = account_row(session)
    if account is not None:
        account.cookies = version.cookies
        set_earliest_expiry(account, earliest_login_expiry(merged.text))
    _prune(session)
    return merged.changed


def _prune(session: Session) -> None:
    """Keep the newest versions, the current version and the newest good version, and remove the others."""
    ids = session.scalars(select(CookieVersion.id).order_by(CookieVersion.id.desc())).all()
    kept = set(ids[:VERSIONS_KEPT])
    kept.update(version.id for version in (current_version(session), _newest_good(session)) if version is not None)
    stale = [version_id for version_id in ids if version_id not in kept]
    if stale:
        session.execute(delete(CookieVersion).where(CookieVersion.id.in_(stale)))
