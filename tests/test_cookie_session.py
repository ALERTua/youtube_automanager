"""Tests of the encrypted cookie storage and its versions. The cookie values here are fake."""

import datetime

import pytest
from sqlalchemy import select

from youtube_automanager.db import make_engine, make_session_factory, upgrade_to_head
from youtube_automanager.db.models import Account, CookieOrigin, CookieVersion, CookieVersionState, SessionState
from youtube_automanager.youtube.session import (
    VERSIONS_KEPT,
    CookieCipher,
    CookieKeyError,
    StoredCookies,
    current_version,
    generate_key,
    load_cookies,
    load_current,
    mark_good,
    retire,
    save_cookies,
    write_back,
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


# The versions of the cookies

NOW = datetime.datetime(2026, 10, 12, 18, 0, tzinfo=datetime.UTC)


def cookie_text(**values):
    """A cookie file with SID and SIDCC, and ``values`` as the new values."""
    values = {"SID": "fake-sid", "SIDCC": "fake-sidcc-0"} | values
    lines = [f".youtube.com\tTRUE\t/\tTRUE\t1893456000\t{name}\t{value}" for name, value in values.items()]
    return "\n".join(["# Netscape HTTP Cookie File", *lines]) + "\n"


@pytest.fixture
def store(database, key):
    _, sessions = database
    return VersionStore(sessions, CookieCipher(key))


class VersionStore:
    """Calls the version functions each in its own transaction, as the service does."""

    def __init__(self, sessions, cipher):
        self.sessions = sessions
        self.cipher = cipher

    def upload(self, text):
        with self.sessions.begin() as session:
            return save_cookies(session, self.cipher, text, now=NOW).id

    def write_back(self, base, text, origin=CookieOrigin.WRITE_BACK):
        with self.sessions.begin() as session:
            return write_back(session, self.cipher, base, text, origin, now=NOW)

    def add(self, base, sidcc):
        """Write back a new SIDCC value on top of ``base``, and return the id of the new version."""
        assert self.write_back(base, cookie_text(SIDCC=sidcc)) == ("SIDCC",)
        return self.current().version_id

    def good(self, version_id):
        with self.sessions.begin() as session:
            mark_good(session, version_id, NOW)

    def retire(self, version_id):
        with self.sessions.begin() as session:
            good = retire(session, version_id, NOW)
            return None if good is None else good.id

    def current(self):
        with self.sessions() as session:
            return load_current(session, self.cipher)

    def versions(self):
        with self.sessions() as session:
            rows = session.scalars(select(CookieVersion).order_by(CookieVersion.id))
            return [(row.id, row.origin, row.state) for row in rows]

    def account(self):
        with self.sessions() as session:
            return session.scalars(select(Account)).one()

    def mirror_is_current(self):
        """``account.cookies`` holds the copy of the current version."""
        with self.sessions() as session:
            account = session.scalars(select(Account)).one()
            return account.cookies == current_version(session).cookies


def test_an_upload_is_the_first_unchecked_version_and_resets_the_session(store):
    first = store.upload(cookie_text())
    with store.sessions.begin() as session:
        account = session.scalars(select(Account)).one()
        account.session_state = SessionState.DEAD
        account.last_checked_at = NOW
        account.expiry_warning_days = 1

    second = store.upload(cookie_text(SID="fake-sid-other"))

    assert store.versions() == [(second, CookieOrigin.UPLOAD, CookieVersionState.UNCHECKED)]
    assert first != second
    account = store.account()
    assert (account.session_state, account.last_checked_at, account.expiry_warning_days) == (
        SessionState.UNKNOWN,
        None,
        None,
    )
    assert account.earliest_cookie_expiry == datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)
    assert store.current().text == cookie_text(SID="fake-sid-other")
    assert store.mirror_is_current()


def test_a_write_back_with_new_values_adds_a_version(store):
    first = store.upload(cookie_text())

    changed = store.write_back(first, cookie_text(SIDCC="fake-sidcc-1", NEW="fake-new"))

    assert changed == ("SIDCC",)
    (_, (second, origin, state)) = store.versions()
    assert (origin, state) == (CookieOrigin.WRITE_BACK, CookieVersionState.UNCHECKED)
    assert store.current() == StoredCookies(second, cookie_text(SIDCC="fake-sidcc-1"))
    assert store.mirror_is_current()


def test_a_write_back_without_new_values_adds_no_version(store):
    first = store.upload(cookie_text())

    assert store.write_back(first, cookie_text()) == ()
    assert [version_id for version_id, _, _ in store.versions()] == [first]


def test_a_write_back_on_an_old_version_adds_no_version(store):
    first = store.upload(cookie_text())
    second = store.upload(cookie_text(SID="fake-sid-new-upload"))

    assert store.write_back(first, cookie_text(SIDCC="fake-sidcc-1")) == ()
    assert [version_id for version_id, _, _ in store.versions()] == [second]
    assert store.current().text == cookie_text(SID="fake-sid-new-upload")


def test_retire_goes_back_to_the_newest_good_version_and_gives_up_the_newer_ones(store):
    first = store.upload(cookie_text())
    second = store.add(first, "fake-sidcc-1")
    store.good(second)
    third = store.add(second, "fake-sidcc-2")
    fourth = store.add(third, "fake-sidcc-3")

    assert store.retire(fourth) == second

    states = {version_id: state for version_id, _, state in store.versions()}
    assert states == {
        first: CookieVersionState.UNCHECKED,
        second: CookieVersionState.GOOD,
        third: CookieVersionState.DEAD,
        fourth: CookieVersionState.DEAD,
    }
    assert store.current().version_id == second
    assert store.mirror_is_current()


def test_retire_without_a_good_version_leaves_no_current_version(store):
    first = store.upload(cookie_text())
    second = store.add(first, "fake-sidcc-1")

    assert store.retire(second) is None

    assert store.current() is None
    assert {state for _, _, state in store.versions()} == {CookieVersionState.DEAD}
    # The account keeps the last cookies, so the service still knows that cookies were stored.
    assert store.account().cookies is not None


def test_retire_of_a_good_version_goes_back_to_an_older_good_version(store):
    first = store.upload(cookie_text())
    store.good(first)
    second = store.add(first, "fake-sidcc-1")
    store.good(second)

    assert store.retire(second) == first
    assert store.current().version_id == first


def test_the_store_keeps_ten_versions_and_the_newest_good_one(store):
    first = store.upload(cookie_text())
    store.good(first)
    base = first
    for number in range(1, VERSIONS_KEPT + 5):
        store.write_back(base, cookie_text(SIDCC=f"fake-sidcc-{number}"))
        base = store.current().version_id

    ids = [version_id for version_id, _, _ in store.versions()]
    assert len(ids) == VERSIONS_KEPT + 1
    assert ids[0] == first
    assert ids[-1] == base
    assert store.retire(base) == first
