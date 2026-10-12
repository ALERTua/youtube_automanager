"""Keeps the cookies of the YouTube account in the database, encrypted with a key that only the environment holds.

A copy of the database without the key holds no cookie value.
"""

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from youtube_automanager.db.models import Account


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


def _account(session: Session) -> Account | None:
    """The service has one account, so the first row holds it."""
    return session.scalars(select(Account).order_by(Account.id).limit(1)).first()


def save_cookies(session: Session, cipher: CookieCipher, cookies_text: str) -> None:
    """Store the Netscape cookie text of the account, encrypted. The caller commits the session."""
    account = _account(session) or Account()
    account.cookies = cipher.encrypt(cookies_text)
    session.add(account)


def load_cookies(session: Session, cipher: CookieCipher) -> str | None:
    """Return the stored Netscape cookie text of the account, or None when no cookies are stored."""
    account = _account(session)
    if account is None or account.cookies is None:
        return None
    return cipher.decrypt(account.cookies)
