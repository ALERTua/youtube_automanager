"""Tests of the checks of a cookie upload before any request to YouTube. Each cookie value is fake."""

import datetime

import pytest

from youtube_automanager.web.ui.cookie_upload import login_expired_at, missing_login_cookies, normalized
from youtube_automanager.youtube.cookie_text import LOGIN_COOKIES

SECRET = "s3cr3tCookieValue"
# 2030-01-01 00:00 UTC.
EXPIRES = 1_893_456_000


def line(name: str, value: str | None = None, *, domain: str = ".youtube.com", expires: int = EXPIRES) -> str:
    flag = "TRUE" if domain.startswith(".") else "FALSE"
    return f"{domain}\t{flag}\t/\tTRUE\t{expires}\t{name}\t{value or SECRET + name}"


def cookie_text(names=LOGIN_COOKIES, *extra: str, domain: str = ".youtube.com") -> str:
    return (
        "\n".join(["# Netscape HTTP Cookie File", *(line(name, domain=domain) for name in sorted(names)), *extra])
        + "\n"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ""),
        ("\n \n", ""),
        ("﻿# Netscape HTTP Cookie File\r\na\tb\r\n", "# Netscape HTTP Cookie File\na\tb\n"),
        ("\n\n  # Netscape HTTP Cookie File\nlast\t", "# Netscape HTTP Cookie File\nlast\t\n"),
    ],
)
def test_normalized_drops_the_byte_order_mark_and_the_blank_start_and_keeps_each_line(text, expected):
    assert normalized(text) == expected


def test_a_full_export_lacks_no_login_cookie():
    assert missing_login_cookies(cookie_text(LOGIN_COOKIES, line("GPS"), line("NID", domain=".google.com"))) == ()


def test_the_missing_login_cookies_come_sorted_by_name():
    text = cookie_text(LOGIN_COOKIES - {"SID", "LOGIN_INFO", "__Secure-3PSIDTS"})

    assert missing_login_cookies(text) == ("LOGIN_INFO", "SID", "__Secure-3PSIDTS")


def test_only_the_cookies_of_the_youtube_domain_count():
    google = cookie_text(LOGIN_COOKIES, domain=".google.com")
    host_only = cookie_text(LOGIN_COOKIES, domain="www.youtube.com")

    assert missing_login_cookies(google) == tuple(sorted(LOGIN_COOKIES))
    assert missing_login_cookies(host_only) == tuple(sorted(LOGIN_COOKIES))
    assert missing_login_cookies(cookie_text(LOGIN_COOKIES, domain=".YouTube.com")) == ()


@pytest.mark.parametrize(
    "text",
    [
        "SID\tvalue\n",
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\tsoon\tSID\t" + SECRET + "\n",
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID " + SECRET + "\n",
    ],
)
def test_a_text_that_is_not_a_netscape_cookie_file_gives_an_error_without_a_value(text):
    with pytest.raises(ValueError) as error:
        missing_login_cookies(text)

    assert SECRET not in str(error.value)


# The expiry of the login cookies


NOW = datetime.datetime(2026, 10, 12, 18, 0, tzinfo=datetime.UTC)
SECOND = datetime.timedelta(seconds=1)


def expiring_at(moment: datetime.datetime, name: str = "SID") -> str:
    """A full export in which the login cookie ``name`` expires at ``moment`` and each other cookie in 2030."""
    expires = int(moment.timestamp())
    return cookie_text(LOGIN_COOKIES - {name}, line(name, expires=expires), line("GPS", expires=expires - 3600))


@pytest.mark.parametrize(
    ("moment", "expired"),
    [(NOW - SECOND, True), (NOW, True), (NOW + SECOND, False)],
    ids=["just before now", "at now", "just after now"],
)
def test_an_export_whose_login_cookie_expired_by_now_gives_the_expiry(moment, expired):
    assert login_expired_at(expiring_at(moment), NOW) == (moment if expired else None)


def test_each_login_cookie_gives_the_expiry_and_another_cookie_does_not():
    assert login_expired_at(expiring_at(NOW, "__Secure-3PSIDCC"), NOW) == NOW
    # GPS is no login cookie, so its expiry in 1970 does not count.
    assert login_expired_at(cookie_text(LOGIN_COOKIES, line("GPS", expires=1)), NOW) is None


def test_an_export_without_a_login_expiry_date_did_not_expire():
    session_cookies = cookie_text(set(), *(line(name, expires=0) for name in sorted(LOGIN_COOKIES)))

    assert login_expired_at(session_cookies, NOW) is None
