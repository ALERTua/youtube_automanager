"""Tests of the cookie session of a run or a check: the check, the rollback, the events and the write-back.

A fake YouTube answers the session checks. It knows a cookie version by the value of its SIDCC cookie. The cookie
values here are fake.
"""

import datetime

import pytest
from sqlalchemy import select

from youtube_automanager.account_cache import REFRESHED_AT_KEY, refresh_account_cache
from youtube_automanager.db.models import (
    Account,
    Channel,
    CookieOrigin,
    CookieVersion,
    CookieVersionState,
    Playlist,
    Run,
    RunStatus,
    RunTrigger,
    SessionState,
    Setting,
)
from youtube_automanager.engine import sync as sync_module
from youtube_automanager.engine.account_session import (
    DEAD_SESSION_TEXT,
    UPLOAD_REFUSED_TEXT,
    AccountSession,
    AccountUnavailable,
    CheckOutcome,
    CheckResult,
)
from youtube_automanager.notify import Event
from youtube_automanager.settings import Settings
from youtube_automanager.youtube.account import AccountError, AccountPlaylist, NotLoggedInError, SubscribedChannel
from youtube_automanager.youtube.cookie_text import cookies
from youtube_automanager.youtube.session import CookieCipher, generate_key, save_cookies

FAR = 4_000_000_000
NOW = datetime.datetime(2026, 10, 12, 18, 0, tzinfo=datetime.UTC)


def cookie_text(sidcc="fake-sidcc-0", *, login_expires=FAR):
    return (
        "# Netscape HTTP Cookie File\n"
        f".youtube.com\tTRUE\t/\tTRUE\t{login_expires}\tSAPISID\tfake-sapisid\n"
        f".youtube.com\tTRUE\t/\tFALSE\t{FAR}\tSIDCC\t{sidcc}\n"
        f".youtube.com\tTRUE\t/\tTRUE\t{FAR}\tPREF\tf6=1\n"
        f".youtube.com\tTRUE\t/\tFALSE\t1700000000\tST-fake\tfake-st\n"
    )


def sidcc(text):
    return {cookie.name: cookie.value for cookie in cookies(text)}["SIDCC"]


class FakeYouTube:
    """Answers the session checks. A refused SIDCC value is dead. After a check, the jar holds a new SIDCC value."""

    def __init__(self):
        self.refused = set()
        self.failing = False
        self.checks = []
        self.clients = []
        # The account lists of the fake account, and the error of a list read.
        self.channels = [SubscribedChannel("UCaaaaaaaaaaaaaaaaaaaaaa", "Alpha", "@alpha", None)]
        self.playlists = [AccountPlaylist("WL", "Watch later", True)]
        self.list_error = None
        self.list_reads = []

    def client(self, text):
        client = FakeClient(self, text)
        self.clients.append(client)
        return client


class FakeClient:
    def __init__(self, youtube, text):
        self.youtube = youtube
        self.text = text
        self.renewed = False

    def check_session(self):
        value = sidcc(self.text)
        self.youtube.checks.append(value)
        if value in self.youtube.refused:
            raise NotLoggedInError("YouTube answered https://www.youtube.com as to an anonymous user")
        # Each answer brings a new SIDCC value, also an answer that does not tell whether the session lives.
        self.renewed = True
        if self.youtube.failing:
            raise AccountError("yt-dlp failed to check the session: HTTP Error 503")

    def use_cookies(self, text):
        self.text = text
        self.renewed = False

    def cookies_text(self):
        """The jar as yt-dlp saves it: the expired ST cookie is gone, PREF has the values of yt-dlp, SIDCC is new."""
        text = self.text.replace("f6=1", "hl=en&tz=UTC")
        text = "".join(line + "\n" for line in text.splitlines() if "ST-fake" not in line)
        return text.replace(sidcc(self.text), sidcc(self.text) + "+1") if self.renewed else text

    def subscribed_channels(self):
        return self._list("channels")

    def playlists(self):
        return self._list("playlists")

    def _list(self, name):
        self.youtube.list_reads.append(name)
        if self.youtube.list_error is not None:
            raise self.youtube.list_error
        return list(getattr(self.youtube, name))

    def iter_subscription_feed(self):
        return iter(())

    def add_video(self, playlist_id, video_id):
        pass


class Harness:
    def __init__(self, sessions, tmp_path):
        self.sessions = sessions
        self.key = generate_key()
        self.cipher = CookieCipher(self.key)
        self.tmp_path = tmp_path
        self.settings = self.make_settings()
        self.youtube = FakeYouTube()
        self.sent = []
        self.now = NOW

    def make_settings(self, *, key=..., tz="UTC"):
        key = self.key if key is ... else key
        return Settings(home=self.tmp_path, database_url="sqlite://", tz=tz, cookies_key=key)

    def upload(self, text=None):
        with self.sessions.begin() as session:
            return save_cookies(session, self.cipher, text or cookie_text(), now=self.now).id

    def account_session(self, **options):
        return AccountSession(
            self.sessions,
            options.pop("settings", self.settings),
            send=self.sent.append,
            make_client=self.youtube.client,
            clock=lambda: self.now,
            **options,
        )

    def check(self):
        account = self.account_session()
        return account, account.check()

    def account(self):
        with self.sessions() as session:
            return session.scalars(select(Account)).one()

    def versions(self):
        with self.sessions() as session:
            rows = session.scalars(select(CookieVersion).order_by(CookieVersion.id))
            return [(sidcc(self.cipher.decrypt(row.cookies)), row.origin, row.state) for row in rows]

    def stored_text(self):
        return self.cipher.decrypt(self.account().cookies)

    def events(self):
        return [notification.event for notification in self.sent]


@pytest.fixture
def harness(db_sessions, tmp_path):
    return Harness(db_sessions, tmp_path)


# Without usable cookies


@pytest.mark.parametrize(
    ("prepare", "expected"),
    [
        (lambda h: setattr(h, "settings", h.make_settings(key=None)), "COOKIES_KEY is not set"),
        (lambda h: None, "no YouTube cookies are stored"),
        (
            lambda h: setattr(h, "settings", h.make_settings(key=generate_key())) or h.upload(),
            "cannot be opened: The cookie key does not open",
        ),
        (lambda h: setattr(h, "settings", h.make_settings(key="not a key")), "A cookie key is not valid"),
    ],
    ids=["no key", "no cookies", "other key", "bad key"],
)
def test_a_check_without_usable_cookies_sends_no_request(harness, prepare, expected):
    prepare(harness)

    account, result = harness.check()

    assert result.outcome is CheckOutcome.UNAVAILABLE
    assert expected in result.detail
    assert harness.youtube.clients == []
    with pytest.raises(AccountUnavailable, match=expected):
        account.open()
    assert account.write_back() == ()


def test_a_check_of_stored_text_that_is_not_a_cookie_file_names_no_value(harness):
    with harness.sessions.begin() as session:
        save_cookies(session, harness.cipher, "SID=fakeSIDvalue")

    result = AccountSession(harness.sessions, harness.settings, send=harness.sent.append).check()

    assert result.outcome is CheckOutcome.UNAVAILABLE
    assert result.detail is not None
    assert "stored cookies are not valid" in result.detail
    assert "fakeSIDvalue" not in result.detail


# A live session


def test_a_live_session_makes_the_version_good_and_the_session_ok(harness):
    harness.upload()

    account, result = harness.check()

    assert result == CheckResult(CheckOutcome.ALIVE)
    assert harness.versions() == [("fake-sidcc-0", CookieOrigin.UPLOAD, CookieVersionState.GOOD)]
    stored = harness.account()
    assert (stored.session_state, stored.last_checked_at) == (SessionState.OK, NOW)
    assert stored.earliest_cookie_expiry == datetime.datetime.fromtimestamp(FAR, datetime.UTC)
    assert harness.sent == []
    assert account.open() is harness.youtube.clients[0]


def test_the_write_back_saves_only_new_values_of_known_cookies(harness):
    harness.upload()
    account, _ = harness.check()

    assert account.write_back() == ("SIDCC",)

    assert harness.versions()[-1] == ("fake-sidcc-0+1", CookieOrigin.WRITE_BACK, CookieVersionState.UNCHECKED)
    stored = harness.stored_text()
    # The removed ST cookie and the PREF of yt-dlp stay out.
    assert stored == cookie_text("fake-sidcc-0+1")


def test_a_session_that_dies_turns_dead_once_and_sends_the_event_once(harness):
    harness.upload()
    harness.youtube.refused.add("fake-sidcc-0")

    account, result = harness.check()

    assert result == CheckResult(CheckOutcome.DEAD, DEAD_SESSION_TEXT)
    assert (harness.account().session_state, harness.account().last_checked_at) == (SessionState.DEAD, NOW)
    assert harness.versions() == [("fake-sidcc-0", CookieOrigin.UPLOAD, CookieVersionState.DEAD)]
    assert harness.events() == [Event.COOKIE_SESSION_DEAD]
    with pytest.raises(AccountUnavailable, match="does not accept the stored cookies"):
        account.open()
    assert account.write_back() == ()

    # Without a version to try, the next check sends no request and no second event.
    _, again = harness.check()

    assert again.outcome is CheckOutcome.DEAD
    assert harness.youtube.checks == ["fake-sidcc-0"]
    assert harness.events() == [Event.COOKIE_SESSION_DEAD]


def test_a_new_upload_gets_the_dead_event_again(harness):
    harness.upload()
    harness.youtube.refused.add("fake-sidcc-0")
    harness.check()

    harness.upload(cookie_text("fake-sidcc-new"))
    _, result = harness.check()
    harness.youtube.refused.add("fake-sidcc-new")
    _, dead = harness.check()

    assert (result.outcome, dead.outcome) == (CheckOutcome.ALIVE, CheckOutcome.DEAD)
    assert harness.events() == [Event.COOKIE_SESSION_DEAD, Event.COOKIE_SESSION_DEAD]


def test_a_failed_check_keeps_the_state_and_the_client(harness):
    harness.upload()
    harness.check()
    harness.youtube.failing = True
    harness.now += datetime.timedelta(hours=1)

    account, result = harness.check()

    assert result.outcome is CheckOutcome.FAILED
    assert "HTTP Error 503" in result.detail
    assert (harness.account().session_state, harness.account().last_checked_at) == (SessionState.OK, NOW)
    assert harness.sent == []
    assert account.open() is harness.youtube.clients[-1]
    # Without a passed check, the new values of the answer do not go back.
    assert account.write_back() == ()
    assert len(harness.versions()) == 1


def test_a_failing_notification_does_not_fail_the_check(harness):
    harness.upload()
    harness.youtube.refused.add("fake-sidcc-0")

    def broken_send(notification):
        raise RuntimeError("the targets are broken")

    account = AccountSession(
        harness.sessions, harness.settings, send=broken_send, make_client=harness.youtube.client, clock=lambda: NOW
    )

    assert account.check().outcome is CheckOutcome.DEAD
    assert harness.account().session_state is SessionState.DEAD


# The rollback to the last good version


def test_a_dead_version_goes_back_to_the_last_good_version_that_still_lives(harness):
    harness.upload()
    first, _ = harness.check()
    first.write_back()
    harness.youtube.refused.add("fake-sidcc-0+1")

    account, result = harness.check()

    assert result.outcome is CheckOutcome.ALIVE
    # One client checks both versions, so the pause between its requests holds.
    assert len(harness.youtube.clients) == 2
    assert harness.youtube.checks == ["fake-sidcc-0", "fake-sidcc-0+1", "fake-sidcc-0"]
    assert harness.versions() == [
        ("fake-sidcc-0", CookieOrigin.UPLOAD, CookieVersionState.GOOD),
        ("fake-sidcc-0+1", CookieOrigin.WRITE_BACK, CookieVersionState.DEAD),
    ]
    assert harness.account().session_state is SessionState.OK
    assert harness.stored_text() == cookie_text("fake-sidcc-0")
    assert harness.sent == []
    assert account.open() is harness.youtube.clients[1]
    # The write-back starts from the good version.
    assert account.write_back() == ("SIDCC",)
    assert harness.versions()[-1][0] == "fake-sidcc-0+1"


def test_a_dead_version_without_a_living_good_version_kills_the_session(harness):
    harness.upload()
    first, _ = harness.check()
    first.write_back()
    harness.youtube.refused.update({"fake-sidcc-0", "fake-sidcc-0+1"})

    _, result = harness.check()

    assert result.outcome is CheckOutcome.DEAD
    assert harness.youtube.checks == ["fake-sidcc-0", "fake-sidcc-0+1", "fake-sidcc-0"]
    assert {state for _, _, state in harness.versions()} == {CookieVersionState.DEAD}
    assert harness.events() == [Event.COOKIE_SESSION_DEAD]


def test_a_dead_version_without_a_good_version_tries_no_other_version(harness):
    harness.upload()
    first = harness.account_session()
    first.check()
    first.write_back()
    with harness.sessions.begin() as session:
        for version in session.scalars(select(CookieVersion)):
            version.state = CookieVersionState.UNCHECKED
    harness.youtube.refused.add("fake-sidcc-0+1")

    _, result = harness.check()

    assert result.outcome is CheckOutcome.DEAD
    assert harness.youtube.checks == ["fake-sidcc-0", "fake-sidcc-0+1"]


# A session that dies during the work


def test_a_session_that_dies_during_the_work_is_dead_and_the_next_check_tries_the_last_good_version(harness):
    harness.upload()
    first, _ = harness.check()
    first.write_back()
    account, _ = harness.check()

    account.died(NotLoggedInError("YouTube refused the session with HTTP 401"))
    account.died(NotLoggedInError("YouTube refused the session with HTTP 401"))

    assert harness.account().session_state is SessionState.DEAD
    assert harness.events() == [Event.COOKIE_SESSION_DEAD]
    with pytest.raises(AccountUnavailable, match="does not accept the stored cookies"):
        account.open()
    assert account.write_back() == ()

    _, result = harness.check()

    assert result.outcome is CheckOutcome.ALIVE
    assert harness.youtube.checks[-1] == "fake-sidcc-0"
    assert harness.account().session_state is SessionState.OK


def test_a_session_that_is_dead_already_gets_no_second_event(harness):
    harness.upload()
    first, _ = harness.check()
    first.write_back()
    account, _ = harness.check()
    account.died(NotLoggedInError("YouTube refused the session with HTTP 401"))
    harness.youtube.refused.add("fake-sidcc-0")

    _, result = harness.check()

    assert result.outcome is CheckOutcome.DEAD
    assert harness.youtube.checks[-1] == "fake-sidcc-0"
    assert harness.events() == [Event.COOKIE_SESSION_DEAD]


# The expiry warnings


def expiring(harness, expiry):
    harness.upload(cookie_text(login_expires=int(expiry.timestamp())))


@pytest.mark.parametrize(
    ("days", "state"),
    [(4, SessionState.OK), (3, SessionState.EXPIRING), (0, SessionState.EXPIRING), (-1, SessionState.EXPIRING)],
)
def test_a_live_session_within_three_days_of_the_expiry_is_expiring(harness, days, state):
    expiring(harness, NOW + datetime.timedelta(days=days))

    harness.check()

    assert harness.account().session_state is state


def test_each_expiry_warning_comes_once_at_three_one_and_zero_days(harness):
    expiry = datetime.datetime(2026, 10, 15, 12, 0, tzinfo=datetime.UTC)
    expiring(harness, expiry)
    titles = []
    for day in (11, 12, 12, 13, 14, 14, 15, 15, 16):
        harness.now = datetime.datetime(2026, 10, day, 18, 0, tzinfo=datetime.UTC)
        harness.check()
        titles.append([n.title for n in harness.sent])
        harness.sent.clear()

    assert titles == [
        [],
        ["YouTube AutoManager: the YouTube cookies expire in 3 days"],
        [],
        [],
        ["YouTube AutoManager: the YouTube cookies expire in 1 day"],
        [],
        ["YouTube AutoManager: the YouTube cookies expire today"],
        [],
        [],
    ]


def test_a_check_after_two_warning_days_sends_one_warning(harness):
    expiring(harness, NOW)

    harness.check()
    harness.check()

    assert [n.title for n in harness.sent] == ["YouTube AutoManager: the YouTube cookies expire today"]
    assert harness.events() == [Event.COOKIE_EXPIRY_WARNING]


def test_a_new_upload_gets_the_expiry_warnings_again(harness):
    expiring(harness, NOW + datetime.timedelta(days=1))
    harness.check()
    expiring(harness, NOW + datetime.timedelta(days=1))

    harness.check()

    assert harness.events() == [Event.COOKIE_EXPIRY_WARNING, Event.COOKIE_EXPIRY_WARNING]


def test_the_warning_days_count_in_the_time_zone_of_the_service(harness):
    # 22:30 UTC on 12 October is 01:30 on 13 October in Kyiv, and the expiry is on 15 October in both zones.
    harness.now = datetime.datetime(2026, 10, 12, 22, 30, tzinfo=datetime.UTC)
    expiring(harness, datetime.datetime(2026, 10, 15, 1, 0, tzinfo=datetime.UTC))
    harness.settings = harness.make_settings(tz="Europe/Kyiv")

    harness.check()

    (warning,) = harness.sent
    assert warning.title.endswith("expire in 2 days")
    assert "2026-10-15 04:00+03:00" in warning.body


# The check of an upload


def stored_state(harness, *, with_ids=False):
    """Each stored column of the account and of each cookie version, with the decrypted cookie texts."""
    with harness.sessions() as session:
        account = session.scalars(select(Account)).one_or_none()
        rows = session.scalars(select(CookieVersion).order_by(CookieVersion.id)).all()
        versions = [
            ((row.id,) if with_ids else ())
            + (harness.cipher.decrypt(row.cookies), row.origin, row.state, row.created_at, row.checked_at)
            for row in rows
        ]
        if account is None:
            return None, versions
        return (
            (account.id,) if with_ids else (),
            harness.cipher.decrypt(account.cookies) if account.cookies else None,
            account.session_state,
            account.last_checked_at,
            account.earliest_cookie_expiry,
            account.expiry_warning_days,
        ), versions


def test_an_upload_that_youtube_accepts_gets_the_state_of_a_check_followed_by_a_write_back(harness):
    harness.upload(cookie_text("fake-sidcc-new"))
    checked, _ = harness.check()
    checked.write_back()
    expected = stored_state(harness)
    # An older upload with two versions, which the new upload replaces.
    harness.upload()
    harness.check()[0].write_back()

    account = harness.account_session()
    result = account.check_upload(cookie_text("fake-sidcc-new"))

    assert result == CheckResult(CheckOutcome.ALIVE)
    assert harness.youtube.checks[-1] == "fake-sidcc-new"
    assert harness.versions() == [("fake-sidcc-new", CookieOrigin.UPLOAD, CookieVersionState.GOOD)]
    assert (harness.account().session_state, harness.account().last_checked_at) == (SessionState.OK, NOW)
    assert account.write_back() == ("SIDCC",)
    assert stored_state(harness) == expected
    assert harness.stored_text() == cookie_text("fake-sidcc-new+1")
    assert harness.sent == []


def test_a_first_upload_that_youtube_accepts_makes_the_account(harness):
    result = harness.account_session().check_upload(cookie_text())

    assert result.outcome is CheckOutcome.ALIVE
    assert harness.versions() == [("fake-sidcc-0", CookieOrigin.UPLOAD, CookieVersionState.GOOD)]
    stored = harness.account()
    assert (stored.session_state, stored.last_checked_at) == (SessionState.OK, NOW)
    assert stored.earliest_cookie_expiry == datetime.datetime.fromtimestamp(FAR, datetime.UTC)


@pytest.mark.parametrize(
    ("days", "state", "titles"),
    [
        (4, SessionState.OK, []),
        (3, SessionState.EXPIRING, ["YouTube AutoManager: the YouTube cookies expire in 3 days"]),
    ],
)
def test_an_upload_within_three_days_of_the_expiry_is_expiring_and_gets_the_warning(harness, days, state, titles):
    expiry = NOW + datetime.timedelta(days=days)

    harness.account_session().check_upload(cookie_text(login_expires=int(expiry.timestamp())))

    assert harness.account().session_state is state
    assert [notification.title for notification in harness.sent] == titles
    # The next check sends no second warning for the same day.
    harness.check()
    assert [notification.title for notification in harness.sent] == titles


@pytest.mark.parametrize("stored", [True, False], ids=["stored cookies", "no stored cookies"])
def test_an_upload_that_youtube_refuses_changes_nothing_and_sends_no_event(harness, stored):
    if stored:
        harness.upload()
        harness.check()[0].write_back()
    before = stored_state(harness, with_ids=True)
    harness.youtube.refused.add("fake-sidcc-new")
    account = harness.account_session()

    result = account.check_upload(cookie_text("fake-sidcc-new"))

    assert result == CheckResult(CheckOutcome.DEAD, UPLOAD_REFUSED_TEXT)
    assert harness.youtube.checks[-1] == "fake-sidcc-new"
    assert account.write_back() == ()
    assert stored_state(harness, with_ids=True) == before
    assert harness.sent == []


def test_an_upload_whose_check_gives_no_clear_answer_changes_nothing(harness):
    harness.upload()
    harness.check()
    before = stored_state(harness, with_ids=True)
    harness.youtube.failing = True
    account = harness.account_session()

    result = account.check_upload(cookie_text("fake-sidcc-new"))

    assert result.outcome is CheckOutcome.FAILED
    assert (
        result.detail == "The check of the uploaded cookies failed: yt-dlp failed to check the session: HTTP Error 503"
    )
    assert account.write_back() == ()
    assert stored_state(harness, with_ids=True) == before
    assert harness.sent == []


@pytest.mark.parametrize(
    ("key", "expected"),
    [(None, "COOKIES_KEY is not set"), ("not a key", "A cookie key is not valid")],
    ids=["no key", "bad key"],
)
def test_an_upload_without_a_usable_key_sends_no_request_and_stores_nothing(harness, key, expected):
    harness.settings = harness.make_settings(key=key)

    result = harness.account_session().check_upload(cookie_text())

    assert result.outcome is CheckOutcome.UNAVAILABLE
    assert expected in result.detail
    assert harness.youtube.clients == []
    assert stored_state(harness) == (None, [])


def test_an_upload_that_is_not_a_cookie_file_sends_no_request_and_names_no_value(harness):
    account = AccountSession(harness.sessions, harness.settings, send=harness.sent.append)

    result = account.check_upload("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID fakeSIDvalue\n")

    assert result.outcome is CheckOutcome.UNAVAILABLE
    assert result.detail == (
        "The uploaded cookies are not valid: line 2 of the cookie text is not a line of a Netscape cookie file"
    )
    assert stored_state(harness) == (None, [])


# The run with the real wiring


def run_with_the_fake_youtube(harness, monkeypatch):
    """Do one run through ``run_with_youtube``, with the fake YouTube behind the account session."""

    def account_session(sessions, settings, *, send):
        return AccountSession(sessions, settings, send=send, make_client=harness.youtube.client)

    monkeypatch.setattr(sync_module, "AccountSession", account_session)
    with harness.sessions.begin() as session:
        run = Run(status=RunStatus.RUNNING, trigger=RunTrigger.MANUAL)
        session.add(run)
        session.flush()
        run_id = run.id
    return sync_module.run_with_youtube(harness.sessions, run_id, harness.settings)


def stored_account_data(harness):
    with harness.sessions() as session:
        channels = set(session.scalars(select(Channel.channel_id)))
        playlists = set(session.scalars(select(Playlist.playlist_id)))
        refreshed = session.get(Setting, REFRESHED_AT_KEY)
        return channels, playlists, refreshed is not None


def test_a_run_checks_the_session_first_uses_the_checked_client_and_saves_the_updated_cookies(harness, monkeypatch):
    harness.upload()

    run_with_the_fake_youtube(harness, monkeypatch)

    assert harness.youtube.checks == ["fake-sidcc-0"]
    assert harness.account().session_state is SessionState.OK
    assert harness.versions()[-1] == ("fake-sidcc-0+1", CookieOrigin.WRITE_BACK, CookieVersionState.UNCHECKED)


def test_a_run_with_a_live_session_refreshes_a_stale_copy_of_the_account_data_with_the_checked_client(
    harness, monkeypatch
):
    harness.upload()

    run_with_the_fake_youtube(harness, monkeypatch)

    assert len(harness.youtube.clients) == 1
    assert harness.youtube.list_reads == ["channels", "playlists"]
    assert stored_account_data(harness) == ({"UCaaaaaaaaaaaaaaaaaaaaaa"}, {"WL"}, True)


def test_a_run_with_a_fresh_copy_of_the_account_data_reads_no_list(harness, monkeypatch):
    harness.upload()
    # The copy of a refresh a moment ago is fresh.
    harness.youtube.channels = []
    harness.youtube.playlists = []
    refresh_account_cache(harness.sessions, harness.youtube.client(cookie_text()))
    harness.youtube.list_reads.clear()

    run_with_the_fake_youtube(harness, monkeypatch)

    assert harness.youtube.list_reads == []


def test_a_run_without_a_live_session_does_not_refresh_the_account_data(harness, monkeypatch):
    harness.upload()
    harness.youtube.refused.add("fake-sidcc-0")

    run_with_the_fake_youtube(harness, monkeypatch)

    assert harness.account().session_state is SessionState.DEAD
    assert harness.youtube.list_reads == []
    assert stored_account_data(harness) == (set(), set(), False)


def test_a_refresh_that_youtube_refuses_marks_the_session_dead_and_the_run_goes_on(harness, monkeypatch):
    harness.upload()
    harness.youtube.list_error = NotLoggedInError("YouTube answered https://www.youtube.com as to an anonymous user")

    report = run_with_the_fake_youtube(harness, monkeypatch)

    assert report.errors == []
    assert harness.account().session_state is SessionState.DEAD
    assert harness.versions() == [("fake-sidcc-0", CookieOrigin.UPLOAD, CookieVersionState.DEAD)]
    assert stored_account_data(harness) == (set(), set(), False)


def test_an_unexpected_refresh_error_goes_to_the_log_and_the_run_goes_on(harness, monkeypatch, caplog):
    harness.upload()
    harness.youtube.list_error = RuntimeError("a defect in the refresh")

    report = run_with_the_fake_youtube(harness, monkeypatch)

    assert report.errors == []
    assert harness.account().session_state is SessionState.OK
    assert harness.versions()[-1] == ("fake-sidcc-0+1", CookieOrigin.WRITE_BACK, CookieVersionState.UNCHECKED)
    (record,) = [record for record in caplog.records if "refresh of the account data failed" in record.getMessage()]
    assert record.exc_info[1] is harness.youtube.list_error


def test_a_refresh_that_fails_leaves_the_session_live_and_the_run_goes_on(harness, monkeypatch):
    harness.upload()
    harness.youtube.list_error = AccountError("yt-dlp failed to read the subscriptions: HTTP Error 503")

    report = run_with_the_fake_youtube(harness, monkeypatch)

    assert report.errors == []
    assert harness.account().session_state is SessionState.OK
    assert harness.versions()[-1] == ("fake-sidcc-0+1", CookieOrigin.WRITE_BACK, CookieVersionState.UNCHECKED)
    assert stored_account_data(harness) == (set(), set(), False)


def test_a_run_whose_yt_dlp_changed_an_internal_of_the_client_goes_on_without_the_account(
    harness, sync, monkeypatch, caplog
):
    """The real client, on a yt-dlp that lacks ``_download_webpage``: the session check fails, the run reads the
    public channels, and the session stays as it was."""
    from yt_dlp import YoutubeDL
    from yt_dlp.extractor.common import InfoExtractor
    from yt_dlp.version import __version__ as ytdlp_version

    from youtube_automanager.youtube import public
    from youtube_automanager.youtube.account import AccountClient

    channel = "UCaaaaaaaaaaaaaaaaaaaaaa"
    reads = []

    def iter_channel_uploads(channel_id):
        reads.append(channel_id)
        return iter(())

    def refuse(ydl, request):
        raise AssertionError("the client sent a request although its yt-dlp internal is gone")

    monkeypatch.setattr(public, "iter_channel_uploads", iter_channel_uploads)
    monkeypatch.setattr(YoutubeDL, "urlopen", refuse)
    monkeypatch.delattr(InfoExtractor, "_download_webpage")
    monkeypatch.setattr(
        sync_module,
        "AccountSession",
        lambda sessions, settings, *, send: AccountSession(sessions, settings, send=send, make_client=AccountClient),
    )
    sync.rule("all", {"==": [1, 1]}, channels=[channel])
    harness.upload()
    with harness.sessions.begin() as session:
        run = Run(status=RunStatus.RUNNING, trigger=RunTrigger.MANUAL)
        session.add(run)
        session.flush()
        run_id = run.id

    report = sync_module.run_with_youtube(harness.sessions, run_id, harness.settings)

    assert reads == [channel]
    assert report.errors == []
    assert harness.account().session_state is SessionState.UNKNOWN
    assert harness.versions() == [("fake-sidcc-0", CookieOrigin.UPLOAD, CookieVersionState.UNCHECKED)]
    check_failed = [record.getMessage() for record in caplog.records if "session check failed" in record.getMessage()]
    assert check_failed == [
        (
            f"The session check failed: yt-dlp {ytdlp_version} does not fit the client: "
            "YoutubeTab._download_webpage raised AttributeError: "
            "'YoutubeTabIE' object has no attribute '_download_webpage'"
        )
    ]
    assert len([record for record in caplog.records if record.exc_info is not None]) == 1
