"""Tests of the encrypted cookie storage. The cookie values here are fake."""

import pytest
from sqlalchemy import select

from youtube_automanager.db import make_engine, make_session_factory, upgrade_to_head
from youtube_automanager.db.models import Account
from youtube_automanager.youtube.session import (
    CookieCipher,
    CookieKeyError,
    generate_key,
    load_cookies,
    save_cookies,
)

FAKE_VALUES = ("fakeSIDvalue0123456789abcdef", "fakePSIDTSvalue9876543210fedcba")
COOKIES = (
    "# Netscape HTTP Cookie File\n"
    f".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\t{FAKE_VALUES[0]}\n"
    f".youtube.com\tTRUE\t/\tTRUE\t1893456000\t__Secure-1PSIDTS\t{FAKE_VALUES[1]}\n"
)


@pytest.fixture
def key():
    return generate_key()


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "test.sqlite"
    url = f"sqlite:///{path}"
    upgrade_to_head(url)
    engine = make_engine(url)
    yield path, make_session_factory(engine)
    engine.dispose()


def test_decrypt_gives_back_the_encrypted_text(key):
    cipher = CookieCipher(key)

    assert cipher.decrypt(cipher.encrypt(COOKIES)) == COOKIES


def test_encrypted_text_holds_no_cookie_value(key):
    token = CookieCipher(key).encrypt(COOKIES)

    for value in FAKE_VALUES:
        assert value.encode() not in token


def test_another_key_cannot_open_the_cookies(key):
    token = CookieCipher(key).encrypt(COOKIES)

    with pytest.raises(CookieKeyError, match="Upload the cookies again"):
        CookieCipher(generate_key()).decrypt(token)


@pytest.mark.parametrize("keys", ["", " ", ",", " , "])
def test_missing_key_is_refused(keys):
    with pytest.raises(CookieKeyError, match="No cookie key is set"):
        CookieCipher(keys)


@pytest.mark.parametrize("bad_key", ["short", "x" * 44, "not base64 at all !!!!!!!!!!!!!!!!!!!!!!!!!!"])
def test_invalid_key_is_refused_without_repeating_it(bad_key):
    with pytest.raises(CookieKeyError, match="not valid") as raised:
        CookieCipher(bad_key)

    assert bad_key not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def test_an_old_key_still_opens_the_cookies_and_the_newest_key_encrypts(key):
    old_token = CookieCipher(key).encrypt(COOKIES)
    new_key = generate_key()
    both = CookieCipher(f"{new_key}, {key}")

    assert both.decrypt(old_token) == COOKIES
    assert CookieCipher(new_key).decrypt(both.encrypt(COOKIES)) == COOKIES


def test_load_without_stored_cookies_gives_none(database, key):
    _, sessions = database
    with sessions() as session:
        assert load_cookies(session, CookieCipher(key)) is None


def test_save_and_load(database, key):
    _, sessions = database
    cipher = CookieCipher(key)
    with sessions.begin() as session:
        save_cookies(session, cipher, COOKIES)

    with sessions() as session:
        assert load_cookies(session, cipher) == COOKIES


def test_a_second_save_replaces_the_cookies_of_the_one_account(database, key):
    _, sessions = database
    cipher = CookieCipher(key)
    with sessions.begin() as session:
        save_cookies(session, cipher, COOKIES)
    with sessions.begin() as session:
        save_cookies(session, cipher, "# Netscape HTTP Cookie File\n")

    with sessions() as session:
        assert len(session.scalars(select(Account)).all()) == 1
        assert load_cookies(session, cipher) == "# Netscape HTTP Cookie File\n"


def test_a_copy_of_the_database_holds_no_cookie_value(database, key):
    path, sessions = database
    with sessions.begin() as session:
        save_cookies(session, CookieCipher(key), COOKIES)

    content = path.read_bytes()

    assert content
    for value in FAKE_VALUES:
        assert value.encode() not in content
