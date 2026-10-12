"""Tests of the account cache with the test database and a fake client. No test reads YouTube."""

import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from youtube_automanager.account_cache import (
    MAX_AGE,
    REFRESHED_AT_KEY,
    RefreshResult,
    is_cache_stale,
    last_refresh,
    refresh_account_cache,
    refresh_if_stale,
)
from youtube_automanager.db.models import Channel, Playlist, Rule, RuleChannel, Setting
from youtube_automanager.runner import SCHEDULED_WAIT_S
from youtube_automanager.youtube.account import AccountError, AccountPlaylist, SubscribedChannel

A = "UCaaaaaaaaaaaaaaaaaaaaaa"
B = "UCbbbbbbbbbbbbbbbbbbbbbb"
C = "UCcccccccccccccccccccccc"
NOW = datetime.datetime(2026, 10, 12, 18, 0, tzinfo=datetime.UTC)
HOUR = datetime.timedelta(hours=1)
MARK = datetime.date(2026, 10, 1)


class FakeClient:
    """Replaces the account client: gives the two lists, or raises the error that a test sets."""

    def __init__(self, channels=(), playlists=()):
        self.channels = list(channels)
        self.library = list(playlists)
        self.channels_error = None
        self.playlists_error = None
        self.reads = []

    def subscribed_channels(self):
        self.reads.append("channels")
        if self.channels_error is not None:
            raise self.channels_error
        return list(self.channels)

    def playlists(self):
        self.reads.append("playlists")
        if self.playlists_error is not None:
            raise self.playlists_error
        return list(self.library)


class FakeOpener:
    """Replaces the function that opens the client, and counts the calls."""

    def __init__(self, client=None, error=None):
        self.client = client or FakeClient()
        self.error = error
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.client


def channel(channel_id, title="Title", handle=None, thumbnail_url=None):
    return SubscribedChannel(channel_id=channel_id, title=title, handle=handle, thumbnail_url=thumbnail_url)


def playlist(playlist_id, title="Title", is_own=True):
    return AccountPlaylist(playlist_id=playlist_id, title=title, is_own=is_own)


def counts(**values):
    """The result of a refresh, with 0 for each count that a test does not name."""
    return RefreshResult(
        **{
            "channels_new": 0,
            "channels_updated": 0,
            "channels_unsubscribed": 0,
            "playlists_new": 0,
            "playlists_updated": 0,
            **values,
        }
    )


def store(sessions, *rows):
    with sessions.begin() as session:
        session.add_all(rows)


def channel_row(sessions, channel_id):
    with sessions() as session:
        return session.get(Channel, channel_id)


def playlist_row(sessions, playlist_id):
    with sessions() as session:
        return session.get(Playlist, playlist_id)


def channel_ids(sessions):
    with sessions() as session:
        return set(session.scalars(select(Channel.channel_id)))


def playlist_ids(sessions):
    with sessions() as session:
        return set(session.scalars(select(Playlist.playlist_id)))


def stored_time(sessions):
    with sessions() as session:
        row = session.get(Setting, REFRESHED_AT_KEY)
        return None if row is None else row.value


def make_rule(target_playlist_id="WL"):
    return Rule(
        name="rule",
        enabled=True,
        position=1,
        condition={"==": [1, 1]},
        target_playlist_id=target_playlist_id,
        no_shorts=False,
    )


# The channels.


def test_a_subscribed_channel_gets_a_row_that_is_subscribed(db_sessions):
    client = FakeClient(channels=[channel(A, "Alpha", "@alpha", "https://img.example/a"), channel(B, "Beta")])

    result = refresh_account_cache(db_sessions, client, NOW)

    assert result == counts(channels_new=2)
    row_a = channel_row(db_sessions, A)
    assert (row_a.title, row_a.handle, row_a.thumbnail_url, row_a.is_subscribed) == (
        "Alpha",
        "@alpha",
        "https://img.example/a",
        True,
    )
    row_b = channel_row(db_sessions, B)
    assert (row_b.title, row_b.handle, row_b.thumbnail_url, row_b.is_subscribed) == ("Beta", None, None, True)
    assert (row_a.mark_date, row_a.last_checked_at) == (None, None)


def test_a_changed_channel_gets_its_title_handle_and_thumbnail_and_an_unchanged_one_counts_nowhere(db_sessions):
    store(
        db_sessions,
        Channel(channel_id=A, title="Old", handle="@old", thumbnail_url="https://img.example/old", is_subscribed=True),
        Channel(channel_id=B, title="Same", handle="@same", thumbnail_url="https://img.example/b", is_subscribed=True),
    )
    client = FakeClient(
        channels=[
            channel(A, "New", "@new", "https://img.example/new"),
            channel(B, "Same", "@same", "https://img.example/b"),
        ]
    )

    result = refresh_account_cache(db_sessions, client, NOW)

    assert result == counts(channels_updated=1)
    row_a = channel_row(db_sessions, A)
    assert (row_a.title, row_a.handle, row_a.thumbnail_url) == ("New", "@new", "https://img.example/new")


def test_a_channel_that_lost_its_handle_and_thumbnail_gets_none(db_sessions):
    store(
        db_sessions,
        Channel(
            channel_id=A, title="Alpha", handle="@alpha", thumbnail_url="https://img.example/a", is_subscribed=True
        ),
    )

    result = refresh_account_cache(db_sessions, FakeClient(channels=[channel(A, "Alpha")]), NOW)

    assert result == counts(channels_updated=1)
    row = channel_row(db_sessions, A)
    assert (row.handle, row.thumbnail_url) == (None, None)


def test_an_update_keeps_the_mark_date_and_the_last_check_of_the_sync_engine(db_sessions):
    checked = NOW - HOUR
    store(
        db_sessions,
        Channel(channel_id=A, title="Old", is_subscribed=True, mark_date=MARK, last_checked_at=checked),
        Channel(channel_id=B, title="Gone", is_subscribed=False, mark_date=MARK, last_checked_at=checked),
        Channel(channel_id=C, title="Left", is_subscribed=True, mark_date=MARK, last_checked_at=checked),
    )
    # A is updated, B subscribes again, and C is unsubscribed.
    client = FakeClient(channels=[channel(A, "New"), channel(B, "Back")])

    result = refresh_account_cache(db_sessions, client, NOW)

    assert result == counts(channels_updated=2, channels_unsubscribed=1)
    for channel_id in (A, B, C):
        row = channel_row(db_sessions, channel_id)
        assert (row.mark_date, row.last_checked_at) == (MARK, checked), channel_id


def test_a_channel_that_is_not_subscribed_any_more_keeps_its_row_and_its_rule_link(db_sessions):
    store(
        db_sessions,
        Playlist(playlist_id="WL", title="Watch later", is_own=True),
        Channel(channel_id=A, title="Alpha", is_subscribed=True),
        Channel(channel_id=B, title="Beta", handle="@beta", is_subscribed=True),
    )
    with db_sessions.begin() as session:
        rule = make_rule()
        session.add(rule)
        session.flush()
        session.add(RuleChannel(rule_id=rule.id, channel_id=B))
        rule_id = rule.id

    first = refresh_account_cache(db_sessions, FakeClient(channels=[channel(A, "Alpha")]), NOW)
    second = refresh_account_cache(db_sessions, FakeClient(channels=[channel(A, "Alpha")]), NOW + HOUR)

    assert first == counts(channels_unsubscribed=1)
    assert second == counts()
    assert channel_ids(db_sessions) == {A, B}
    row = channel_row(db_sessions, B)
    assert (row.title, row.handle, row.is_subscribed) == ("Beta", "@beta", False)
    with db_sessions() as session:
        assert session.get(RuleChannel, (rule_id, B)) is not None


def test_a_channel_that_is_subscribed_again_is_updated(db_sessions):
    store(db_sessions, Channel(channel_id=A, title="Alpha", is_subscribed=False))

    result = refresh_account_cache(db_sessions, FakeClient(channels=[channel(A, "Alpha")]), NOW)

    assert result == counts(channels_updated=1)
    assert channel_row(db_sessions, A).is_subscribed is True


def test_an_empty_list_unsubscribes_each_channel_and_deletes_none(db_sessions):
    store(
        db_sessions,
        Channel(channel_id=A, title="Alpha", is_subscribed=True),
        Channel(channel_id=B, title="Beta", is_subscribed=True),
    )

    result = refresh_account_cache(db_sessions, FakeClient(), NOW)

    assert result == counts(channels_unsubscribed=2)
    assert channel_ids(db_sessions) == {A, B}
    assert not channel_row(db_sessions, A).is_subscribed
    assert not channel_row(db_sessions, B).is_subscribed


def test_an_empty_title_keeps_the_stored_title_and_a_new_row_gets_the_id(db_sessions):
    store(db_sessions, Channel(channel_id=A, title="Alpha", handle="@alpha", is_subscribed=True))

    result = refresh_account_cache(db_sessions, FakeClient(channels=[channel(A, "", "@alpha"), channel(B, "")]), NOW)

    assert result == counts(channels_new=1)
    assert channel_row(db_sessions, A).title == "Alpha"
    assert channel_row(db_sessions, B).title == B


def test_a_channel_that_the_list_names_twice_counts_once(db_sessions):
    client = FakeClient(channels=[channel(A, "First"), channel(A, "Second")])

    result = refresh_account_cache(db_sessions, client, NOW)

    assert result == counts(channels_new=1)
    assert channel_row(db_sessions, A).title == "Second"


# The playlists.


def test_playlists_get_rows_with_watch_later_as_a_normal_row(db_sessions):
    client = FakeClient(
        playlists=[
            playlist("WL", "Watch later", True),
            playlist("LL", "Liked videos", False),
            playlist("PLmine", "Mine", True),
        ]
    )

    result = refresh_account_cache(db_sessions, client, NOW)

    assert result == counts(playlists_new=3)
    rows = {playlist_id: playlist_row(db_sessions, playlist_id) for playlist_id in ("WL", "LL", "PLmine")}
    assert {key: (row.title, row.is_own, row.last_synced_at) for key, row in rows.items()} == {
        "WL": ("Watch later", True, NOW),
        "LL": ("Liked videos", False, NOW),
        "PLmine": ("Mine", True, NOW),
    }


def test_a_changed_playlist_is_updated_and_each_listed_playlist_gets_the_time(db_sessions):
    old = NOW - HOUR
    store(
        db_sessions,
        Playlist(playlist_id="PL1", title="Old", is_own=False, last_synced_at=old),
        Playlist(playlist_id="PL2", title="Same", is_own=True, last_synced_at=old),
    )
    client = FakeClient(playlists=[playlist("PL1", "New", True), playlist("PL2", "Same", True)])

    result = refresh_account_cache(db_sessions, client, NOW)

    assert result == counts(playlists_updated=1)
    row_1 = playlist_row(db_sessions, "PL1")
    assert (row_1.title, row_1.is_own, row_1.last_synced_at) == ("New", True, NOW)
    # An unchanged playlist counts nowhere, but it still gets the time of the refresh.
    assert playlist_row(db_sessions, "PL2").last_synced_at == NOW


def test_a_playlist_that_left_the_library_stays_with_its_old_time_and_its_rule_link(db_sessions):
    old = NOW - HOUR
    store(
        db_sessions,
        Playlist(playlist_id="WL", title="Watch later", is_own=True, last_synced_at=old),
        Playlist(playlist_id="PLgone", title="Gone", is_own=True, last_synced_at=old),
    )
    with db_sessions.begin() as session:
        session.add(make_rule("PLgone"))

    result = refresh_account_cache(db_sessions, FakeClient(playlists=[playlist("WL", "Watch later", True)]), NOW)

    assert result == counts()
    assert playlist_ids(db_sessions) == {"WL", "PLgone"}
    gone = playlist_row(db_sessions, "PLgone")
    assert (gone.title, gone.is_own, gone.last_synced_at) == ("Gone", True, old)
    with db_sessions() as session:
        assert session.scalars(select(Rule.target_playlist_id)).all() == ["PLgone"]
    # The old time of the row tells that the library lost the playlist, so the table needs no flag column.
    assert gone.last_synced_at < last_refresh(db_sessions)
    assert playlist_row(db_sessions, "WL").last_synced_at == last_refresh(db_sessions)


def test_an_empty_playlist_title_keeps_the_stored_title_and_a_new_row_gets_the_id(db_sessions):
    store(db_sessions, Playlist(playlist_id="PL1", title="Kept", is_own=True))

    result = refresh_account_cache(db_sessions, FakeClient(playlists=[playlist("PL1", ""), playlist("PL2", "")]), NOW)

    assert result == counts(playlists_new=1)
    assert playlist_row(db_sessions, "PL1").title == "Kept"
    assert playlist_row(db_sessions, "PL2").title == "PL2"


# The refresh time.


def test_the_refresh_time_is_stored_as_iso_text_in_utc_and_replaced_at_the_next_refresh(db_sessions):
    refresh_account_cache(db_sessions, FakeClient(), NOW)

    assert stored_time(db_sessions) == "2026-10-12T18:00:00+00:00"
    assert last_refresh(db_sessions) == NOW

    plus_three = datetime.timezone(datetime.timedelta(hours=3))
    refresh_account_cache(db_sessions, FakeClient(), datetime.datetime(2026, 10, 13, 21, 30, tzinfo=plus_three))

    assert stored_time(db_sessions) == "2026-10-13T18:30:00+00:00"


def test_the_refresh_without_a_time_uses_the_current_time(db_sessions):
    before = datetime.datetime.now(datetime.UTC)

    refresh_account_cache(db_sessions, FakeClient(playlists=[playlist("WL", "Watch later")]))

    after = datetime.datetime.now(datetime.UTC)
    assert before <= last_refresh(db_sessions) <= after
    assert playlist_row(db_sessions, "WL").last_synced_at == last_refresh(db_sessions)


def test_a_time_without_a_time_zone_is_refused_before_any_read(db_sessions):
    client = FakeClient(channels=[channel(A)])

    with pytest.raises(ValueError, match="time zone"):
        refresh_account_cache(db_sessions, client, datetime.datetime(2026, 10, 12, 18, 0))

    assert client.reads == []
    assert stored_time(db_sessions) is None


@pytest.mark.parametrize("failing", ["channels", "playlists"])
def test_a_failed_read_changes_nothing(db_sessions, failing):
    store(db_sessions, Channel(channel_id=A, title="Alpha", is_subscribed=True))
    client = FakeClient(channels=[channel(B, "Beta")], playlists=[playlist("WL", "Watch later")])
    setattr(client, f"{failing}_error", AccountError(f"yt-dlp failed to read {failing}"))

    with pytest.raises(AccountError, match=f"read {failing}"):
        refresh_account_cache(db_sessions, client, NOW)

    assert channel_ids(db_sessions) == {A}
    assert channel_row(db_sessions, A).is_subscribed is True
    assert playlist_ids(db_sessions) == set()
    assert stored_time(db_sessions) is None


def test_a_failed_playlist_write_stores_no_channel_no_playlist_and_no_refresh_time(db_sessions):
    # A playlist without a value for ``is_own`` breaks the NOT NULL rule of the table at the commit of the playlists.
    store(db_sessions, Channel(channel_id=B, title="Beta", is_subscribed=True))
    client = FakeClient(
        channels=[channel(A, "Alpha")],
        playlists=[playlist("WL", "Watch later", True), playlist("PL1", "Broken", None)],
    )

    with pytest.raises(IntegrityError):
        refresh_account_cache(db_sessions, client, NOW)

    # Both tables and the time share one transaction, so the channels that came before are not stored either.
    assert channel_ids(db_sessions) == {B}
    assert channel_row(db_sessions, B).is_subscribed is True
    assert playlist_ids(db_sessions) == set()
    assert stored_time(db_sessions) is None


# The age of the copy.


def test_a_copy_without_a_refresh_time_is_stale(db_sessions):
    assert is_cache_stale(db_sessions, MAX_AGE, NOW) is True
    assert last_refresh(db_sessions) is None


def test_a_copy_is_stale_only_after_the_max_age(db_sessions):
    max_age = datetime.timedelta(hours=6)
    refresh_account_cache(db_sessions, FakeClient(), NOW)

    at_the_limit = NOW + max_age
    just_after = at_the_limit + datetime.timedelta(microseconds=1)

    assert is_cache_stale(db_sessions, max_age, NOW) is False
    assert is_cache_stale(db_sessions, max_age, at_the_limit - datetime.timedelta(microseconds=1)) is False
    assert is_cache_stale(db_sessions, max_age, at_the_limit) is False
    assert is_cache_stale(db_sessions, max_age, just_after) is True


def test_the_default_max_age_is_20_hours(db_sessions):
    refresh_account_cache(db_sessions, FakeClient(), NOW)

    assert MAX_AGE == datetime.timedelta(hours=20)
    assert is_cache_stale(db_sessions, now=NOW + MAX_AGE) is False
    assert is_cache_stale(db_sessions, now=NOW + MAX_AGE + datetime.timedelta(seconds=1)) is True


def test_each_daily_run_finds_the_copy_of_the_run_before_stale(db_sessions):
    # Yesterday the scheduled run waited the longest time for the sync lock, and today it starts on time.
    refresh_account_cache(db_sessions, FakeClient(), NOW + datetime.timedelta(seconds=SCHEDULED_WAIT_S))

    assert is_cache_stale(db_sessions, now=NOW + datetime.timedelta(days=1)) is True


@pytest.mark.parametrize("value", ["yesterday", "", 5, None, ["2026-10-12T18:00:00+00:00"]])
def test_a_stored_value_that_is_not_a_time_makes_the_copy_stale(db_sessions, value):
    store(db_sessions, Setting(key=REFRESHED_AT_KEY, value=value))

    assert last_refresh(db_sessions) is None
    assert is_cache_stale(db_sessions, MAX_AGE, NOW) is True


def test_a_stored_time_in_the_future_makes_the_copy_stale(db_sessions):
    store(db_sessions, Setting(key=REFRESHED_AT_KEY, value=(NOW + HOUR).isoformat()))

    assert is_cache_stale(db_sessions, MAX_AGE, NOW) is True


def test_a_stored_time_without_a_time_zone_counts_as_utc(db_sessions):
    store(db_sessions, Setting(key=REFRESHED_AT_KEY, value="2026-10-12T17:00:00"))

    assert last_refresh(db_sessions) == NOW - HOUR
    assert is_cache_stale(db_sessions, MAX_AGE, NOW) is False


def test_the_check_of_the_age_refuses_a_time_without_a_time_zone(db_sessions):
    with pytest.raises(ValueError, match="time zone"):
        is_cache_stale(db_sessions, MAX_AGE, datetime.datetime(2026, 10, 12, 18, 0))


# The refresh of a stale copy.


def test_a_fresh_copy_opens_no_client_and_changes_nothing(db_sessions):
    refresh_account_cache(db_sessions, FakeClient(channels=[channel(A, "Alpha")]), NOW)
    opener = FakeOpener(FakeClient(channels=[channel(B, "Beta")]))

    result = refresh_if_stale(db_sessions, opener, now=NOW + HOUR)

    assert result is None
    assert opener.calls == 0
    assert opener.client.reads == []
    assert channel_ids(db_sessions) == {A}
    assert last_refresh(db_sessions) == NOW


def test_a_copy_without_a_refresh_opens_the_client_once_and_refreshes(db_sessions):
    opener = FakeOpener(FakeClient(channels=[channel(A, "Alpha")], playlists=[playlist("WL", "Watch later")]))

    result = refresh_if_stale(db_sessions, opener, now=NOW)

    assert result == counts(channels_new=1, playlists_new=1)
    assert opener.calls == 1
    assert last_refresh(db_sessions) == NOW
    assert playlist_row(db_sessions, "WL").last_synced_at == NOW


def test_a_stale_copy_opens_the_client_and_a_custom_max_age_decides(db_sessions):
    refresh_account_cache(db_sessions, FakeClient(), NOW)
    later = NOW + 2 * HOUR
    default_age = FakeOpener()
    short_age = FakeOpener(FakeClient(channels=[channel(A, "Alpha")]))

    assert refresh_if_stale(db_sessions, default_age, now=later) is None
    result = refresh_if_stale(db_sessions, short_age, max_age=HOUR, now=later)

    assert default_age.calls == 0
    assert short_age.calls == 1
    assert result == counts(channels_new=1)
    assert last_refresh(db_sessions) == later


def test_a_client_that_cannot_open_stops_the_refresh_before_any_write(db_sessions):
    opener = FakeOpener(error=AccountError("the account is not available"))

    with pytest.raises(AccountError, match="not available"):
        refresh_if_stale(db_sessions, opener, now=NOW)

    assert opener.calls == 1
    assert stored_time(db_sessions) is None
    assert channel_ids(db_sessions) == set()
