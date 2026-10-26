"""The rules in the database: read, save, delete, and the checks of a rule before a save.

The rules page and the HTTP API of the rules call these functions, so both use the same code and the same texts. The
module imports nothing from NiceGUI or from ``web``, so the API and a child process can import it too. Each function
that touches the database blocks, so a caller on the event loop runs it with ``asyncio.to_thread``.
"""

import functools
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from youtube_automanager.db.models import Channel, Playlist, Rule, RuleChannel, utc_now
from youtube_automanager.engine.rules import validate
from youtube_automanager.i18n import _
from youtube_automanager.runner import SyncRunner
from youtube_automanager.scheduler import stored_timezone


@dataclass(frozen=True)
class ChannelChoice:
    channel_id: str
    title: str
    handle: str | None
    is_subscribed: bool


@dataclass(frozen=True)
class PlaylistChoice:
    playlist_id: str
    title: str
    is_own: bool


@dataclass(frozen=True)
class RuleRecord:
    id: int
    name: str
    enabled: bool
    condition: Any
    playlist_id: str
    no_shorts: bool
    # In the order of the channel titles.
    channel_ids: tuple[str, ...]
    updated_at: datetime
    # The first day of the older videos that the next run also adds. None when the rule has no catch-up.
    catch_up_since: date | None = None


@dataclass(frozen=True)
class Stored:
    """The rules, and the stored copy of the channels and the playlists of the account."""

    # In the order of the evaluation.
    rules: list[RuleRecord]
    # By id, in the order of the titles.
    channels: dict[str, ChannelChoice]
    playlists: dict[str, PlaylistChoice]
    timezone: str

    def rule(self, rule_id: int) -> RuleRecord | None:
        return next((rule for rule in self.rules if rule.id == rule_id), None)

    @functools.cached_property
    def channel_titles(self) -> dict[str, str]:
        return {channel.channel_id: channel.title for channel in self.channels.values()}

    def playlist_title(self, playlist_id: str) -> str:
        playlist = self.playlists.get(playlist_id)
        return playlist.title if playlist else playlist_id


@dataclass(frozen=True)
class RuleDraft:
    """A rule as the editor saves it."""

    name: str
    enabled: bool
    channel_ids: tuple[str, ...]
    playlist_id: str
    no_shorts: bool
    condition: Any
    catch_up_since: date | None = None


def validate_name(name: str | None) -> str:
    """Return the name without the spaces around it. Raise ValueError with a message for the user when it is empty."""
    stripped = (name or "").strip()
    if not stripped:
        raise ValueError(_("Enter a name."))
    return stripped


def validate_playlist(playlist_id: str | None) -> str:
    """Return the id of the target playlist. Raise ValueError with a message for the user when it is empty."""
    if not playlist_id:
        raise ValueError(_("Choose a target playlist."))
    return playlist_id


def validate_draft(draft: RuleDraft) -> None:
    """Check a draft before a save, in this order: the name, the target playlist, the condition.

    Raise ValueError with a message for the user at the first check that fails, and ``RuleError``, a ValueError, when the
    condition is no valid rule tree. The editor does not call this function. ``RuleEditor.read_draft`` calls
    ``validate_name`` and ``validate_playlist`` first, in the same order, and then converts its widgets, which can
    raise their own errors, for example a leaf that is not complete. A caller that has no editor calls this function
    alone.
    """
    validate_name(draft.name)
    validate_playlist(draft.playlist_id)
    validate(draft.condition)


def read_stored(sessions: sessionmaker[Session], default_timezone: str) -> Stored:
    """Read the rules with their channels, the stored channels and playlists, and the time zone. This call blocks."""
    with sessions() as session:
        channels = sorted(session.scalars(select(Channel)), key=lambda row: (row.title.casefold(), row.channel_id))
        order = {row.channel_id: index for index, row in enumerate(channels)}
        links: dict[int, list[str]] = {}
        for rule_id, channel_id in session.execute(select(RuleChannel.rule_id, RuleChannel.channel_id)):
            links.setdefault(rule_id, []).append(channel_id)
        rules = [
            RuleRecord(
                id=rule.id,
                name=rule.name,
                enabled=rule.enabled,
                condition=rule.condition,
                playlist_id=rule.target_playlist_id,
                no_shorts=rule.no_shorts,
                channel_ids=tuple(sorted(links.get(rule.id, ()), key=lambda channel_id: order.get(channel_id, 0))),
                updated_at=rule.updated_at,
                catch_up_since=rule.catch_up_since,
            )
            for rule in session.scalars(select(Rule).order_by(Rule.position, Rule.id))
        ]
        playlists = sorted(session.scalars(select(Playlist)), key=lambda row: (row.title.casefold(), row.playlist_id))
        timezone = stored_timezone(session, default_timezone)
        return Stored(
            rules=rules,
            channels={
                row.channel_id: ChannelChoice(row.channel_id, row.title, row.handle, row.is_subscribed)
                for row in channels
            },
            playlists={row.playlist_id: PlaylistChoice(row.playlist_id, row.title, row.is_own) for row in playlists},
            timezone=timezone,
        )


def save_rule(sessions: sessionmaker[Session], rule_id: int | None, draft: RuleDraft) -> int | None:
    """Create a rule when ``rule_id`` is None, else change it. Return the id of the rule. This call blocks.

    A new rule goes to the end of the evaluation order. Return None when the rule to change does not exist any more.
    """
    with sessions.begin() as session:
        if rule_id is None:
            position = (session.scalar(select(func.max(Rule.position))) or 0) + 1
            rule = Rule(position=position)
            session.add(rule)
        else:
            rule = session.get(Rule, rule_id)
            if rule is None:
                return None
        rule.name = draft.name
        rule.enabled = draft.enabled
        rule.target_playlist_id = draft.playlist_id
        rule.no_shorts = draft.no_shorts
        rule.condition = draft.condition
        rule.catch_up_since = draft.catch_up_since
        # The change tracking compares a JSON value with Python ==, which takes 1 for true. The save always writes the
        # condition of the editor.
        flag_modified(rule, "condition")
        # Each save counts as a change of the rule, also a save that changes only its channels.
        rule.updated_at = utc_now()
        session.flush()
        stored = set(session.scalars(select(RuleChannel.channel_id).where(RuleChannel.rule_id == rule.id)))
        wanted = set(draft.channel_ids)
        if stored - wanted:
            session.execute(
                delete(RuleChannel).where(RuleChannel.rule_id == rule.id, RuleChannel.channel_id.in_(stored - wanted))
            )
        session.add_all(RuleChannel(rule_id=rule.id, channel_id=channel_id) for channel_id in sorted(wanted - stored))
        return rule.id


def delete_rule(sessions: sessionmaker[Session], runner: SyncRunner, rule_id: int) -> None:
    """Delete the rule. Its channel links go with it, and the history keeps its actions. This call blocks.

    The delete holds the sync lock. A run writes the id of the rule with each video that it adds, so a rule that went
    during the run would fail that write after YouTube already added the video. Raise ``RunAlreadyActive`` while a run,
    a session check or a short child holds the lock.
    """
    with runner.lock_held(timeout=0), sessions.begin() as session:
        session.execute(delete(Rule).where(Rule.id == rule_id))
