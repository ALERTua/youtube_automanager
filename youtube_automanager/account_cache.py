"""A copy of the subscriptions and the playlists of the YouTube account in the ``channel`` and ``playlist`` tables.

The rules page and the subscriptions page read this copy, so a page load makes no request to YouTube.
``refresh_account_cache`` replaces the copy with a new read of the account. The setting ``REFRESHED_AT_KEY`` holds the
time of the last refresh, and ``refresh_if_stale`` reads the account only when that time is older than ``MAX_AGE``.

A row never goes away, because a rule can still point at it. A channel that the account left keeps its row with
``is_subscribed`` false. A playlist that the account left keeps its row and its old ``last_synced_at``, so a
``last_synced_at`` before the time of the last refresh shows a playlist that is not in the library any more.

The module never imports yt-dlp. It works with each client that has the two read methods of ``AccountClient``.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import Channel, Playlist, Setting, utc_now

LOG = logging.getLogger(__name__)

# The key of the ``setting`` table that holds the time of the last refresh, as ISO text in UTC.
REFRESHED_AT_KEY = "account_cache_refreshed_at"
# A copy that is older than this is stale. A daily run starts at about the same time each day, and a scheduled run can
# wait up to 10 minutes for the sync lock. The limit thus stays well under one day, so each daily run refreshes the copy.
MAX_AGE = timedelta(hours=20)


class ChannelLike(Protocol):
    """The part of ``SubscribedChannel`` that the refresh reads."""

    @property
    def channel_id(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def handle(self) -> str | None: ...

    @property
    def thumbnail_url(self) -> str | None: ...


class PlaylistLike(Protocol):
    """The part of ``AccountPlaylist`` that the refresh reads."""

    @property
    def playlist_id(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def is_own(self) -> bool: ...


class AccountReader(Protocol):
    """The part of ``AccountClient`` that the refresh uses."""

    def subscribed_channels(self) -> Iterable[ChannelLike]: ...

    def playlists(self) -> Iterable[PlaylistLike]: ...


type OpenClient = Callable[[], AccountReader]


@dataclass(frozen=True)
class RefreshResult:
    """What a refresh changed. A row that the new read leaves as it is counts nowhere."""

    channels_new: int
    channels_updated: int
    channels_unsubscribed: int
    playlists_new: int
    playlists_updated: int


def refresh_account_cache(
    sessions: sessionmaker[Session], client: AccountReader, now: datetime | None = None
) -> RefreshResult:
    """Copy the subscriptions and the playlists of the account into the ``channel`` and ``playlist`` tables.

    The refresh reads both lists first, so a failed read changes nothing. Then it writes both tables and the time of the
    refresh in one transaction, so a failed write changes nothing either.

    A subscribed channel gets its row, or its title, handle and thumbnail updated, and ``is_subscribed`` true. A channel
    row that the subscriptions do not list any more gets ``is_subscribed`` false. ``mark_date`` and
    ``last_checked_at`` belong to the sync engine, and the refresh never writes them. An empty title from YouTube
    keeps the stored title, and a new row with an empty title gets the id as its title.

    A playlist, also Watch Later with the id ``WL``, gets its row, or its title and ``is_own`` updated, and
    ``last_synced_at`` set to ``now``. ``now`` is the current time by default, and it must have a time zone.

    The caller makes sure that no second refresh runs at the same time.
    """
    now = _utc(now) if now is not None else utc_now()
    channels = {channel.channel_id: channel for channel in client.subscribed_channels()}
    playlists = {playlist.playlist_id: playlist for playlist in client.playlists()}
    with sessions.begin() as session:
        channels_new, channels_updated, channels_unsubscribed = _write_channels(session, channels)
        playlists_new, playlists_updated = _write_playlists(session, playlists, now)
        _store_refresh_time(session, now)
    result = RefreshResult(
        channels_new=channels_new,
        channels_updated=channels_updated,
        channels_unsubscribed=channels_unsubscribed,
        playlists_new=playlists_new,
        playlists_updated=playlists_updated,
    )
    LOG.info(
        "Refreshed the account data: channels %d new, %d updated, %d unsubscribed; playlists %d new, %d updated",
        channels_new,
        channels_updated,
        channels_unsubscribed,
        playlists_new,
        playlists_updated,
    )
    return result


def last_refresh(sessions: sessionmaker[Session]) -> datetime | None:
    """Return the time of the last refresh in UTC, or None when there is none or the stored value is not a time."""
    with sessions() as session:
        row = session.get(Setting, REFRESHED_AT_KEY)
        value = row.value if row is not None else None
    if not isinstance(value, str):
        return None
    try:
        stored = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stored.replace(tzinfo=UTC) if stored.tzinfo is None else stored.astimezone(UTC)


def is_cache_stale(sessions: sessionmaker[Session], max_age: timedelta = MAX_AGE, now: datetime | None = None) -> bool:
    """Tell whether the copy is older than ``max_age``. A copy without a stored refresh time is stale.

    A refresh time in the future cannot be trusted, for example after the clock moved back, so it is stale too.
    """
    now = _utc(now) if now is not None else utc_now()
    refreshed = last_refresh(sessions)
    return refreshed is None or refreshed > now or now - refreshed > max_age


def refresh_if_stale(
    sessions: sessionmaker[Session],
    open_client: OpenClient,
    max_age: timedelta = MAX_AGE,
    now: datetime | None = None,
) -> RefreshResult | None:
    """Refresh the copy when it is stale, and return the result. Return None when the copy is fresh.

    ``open_client`` is called only for a stale copy, so a fresh copy needs no cookies and makes no request.
    """
    now = _utc(now) if now is not None else utc_now()
    if not is_cache_stale(sessions, max_age, now):
        return None
    return refresh_account_cache(sessions, open_client(), now)


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"A time without a time zone cannot go into the account cache: {moment!r}")
    return moment.astimezone(UTC)


def _title(new: str, stored: str | None, key: str) -> str:
    """Return the title to store: the new title, else the stored one, else the id."""
    return new or stored or key


def _write_channels(session: Session, channels: dict[str, ChannelLike]) -> tuple[int, int, int]:
    """Write the subscribed channels. Return the numbers of new, updated and unsubscribed rows."""
    rows = {row.channel_id: row for row in session.scalars(select(Channel))}
    new = updated = unsubscribed = 0
    for channel_id, channel in channels.items():
        row = rows.get(channel_id)
        if row is None:
            session.add(
                Channel(
                    channel_id=channel_id,
                    title=_title(channel.title, None, channel_id),
                    handle=channel.handle,
                    thumbnail_url=channel.thumbnail_url,
                    is_subscribed=True,
                )
            )
            new += 1
            continue
        title = _title(channel.title, row.title, channel_id)
        if (row.title, row.handle, row.thumbnail_url, row.is_subscribed) != (
            title,
            channel.handle,
            channel.thumbnail_url,
            True,
        ):
            row.title = title
            row.handle = channel.handle
            row.thumbnail_url = channel.thumbnail_url
            row.is_subscribed = True
            updated += 1
    for channel_id, row in rows.items():
        if channel_id not in channels and row.is_subscribed:
            row.is_subscribed = False
            unsubscribed += 1
    return new, updated, unsubscribed


def _write_playlists(session: Session, playlists: dict[str, PlaylistLike], now: datetime) -> tuple[int, int]:
    """Write the playlists of the library. Return the numbers of new and updated rows."""
    rows = {row.playlist_id: row for row in session.scalars(select(Playlist))}
    new = updated = 0
    for playlist_id, playlist in playlists.items():
        row = rows.get(playlist_id)
        if row is None:
            session.add(
                Playlist(
                    playlist_id=playlist_id,
                    title=_title(playlist.title, None, playlist_id),
                    is_own=playlist.is_own,
                    last_synced_at=now,
                )
            )
            new += 1
            continue
        title = _title(playlist.title, row.title, playlist_id)
        if (row.title, row.is_own) != (title, playlist.is_own):
            row.title = title
            row.is_own = playlist.is_own
            updated += 1
        row.last_synced_at = now
    return new, updated


def _store_refresh_time(session: Session, now: datetime) -> None:
    value = now.isoformat()
    row = session.get(Setting, REFRESHED_AT_KEY)
    if row is None:
        session.add(Setting(key=REFRESHED_AT_KEY, value=value))
    else:
        row.value = value
