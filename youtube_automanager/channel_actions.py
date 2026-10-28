"""The channel actions of the web pages: a search of channels on YouTube, a subscription change, and a channel for a rule.

The search and the subscription change need yt-dlp, so each runs in a short child process that follows the protocol of
``short_child``:

- ``python -m youtube_automanager find-channels --query=<text>`` finds channels anonymously. It needs no cookies and no
  database, but it holds the sync lock like each short child.
- ``subscribe --channel-id=<id> [--title=<title>]`` and ``unsubscribe --channel-id=<id>`` change a subscription of the
  account. The child works with the account as a refresh does. After YouTube confirms the change, it writes
  ``channel.is_subscribed``, and a subscribe adds the ``channel`` row when the row is missing.

``add_channel_to_rule`` sends no request to YouTube, so it runs in the web process. A rule knows a channel only by its
id, and a ``channel`` row never goes away, because a rule can point at it. ``mark_date`` and ``last_checked_at`` of a
channel belong to the sync engine, and this module never writes them.
"""

import dataclasses
import datetime
import enum
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import Channel, Rule, RuleChannel
from youtube_automanager.runner import SyncRunner
from youtube_automanager.settings import Settings
from youtube_automanager.short_child import (
    EXIT_YOUTUBE_ERROR,
    ChildFailed,
    fail,
    read_json,
    run_child,
    succeed,
    work_with_account,
)

if TYPE_CHECKING:
    from youtube_automanager.engine.account_session import ClientLike

# The search gives at most this many channels.
SEARCH_LIMIT = 20
# A search sends one or two anonymous requests. The time includes the start of the child.
SEARCH_TIMEOUT_SECONDS = 90
# A subscription change sends a session check, a read of the page config and the change, and each takes a token of
# the request budget. Each timeout must stay shorter than ``SCHEDULED_WAIT_S`` of the runner, so a channel action
# never makes a scheduled run give up.
SUBSCRIPTION_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class FoundChannel:
    """A channel that the search found. ``handle`` and ``subscriber_count`` are None when YouTube does not show them."""

    channel_id: str
    title: str
    handle: str | None
    subscriber_count: int | None
    thumbnail_url: str | None


class AddOutcome(enum.StrEnum):
    ADDED = "added"
    # The rule already reads the channel, so nothing changed.
    ALREADY_IN_RULE = "already_in_rule"
    # The rule does not exist, for example because it was deleted after the page showed it.
    NO_RULE = "no_rule"


def find_channels_command(query: str) -> list[str]:
    # The form with "=" keeps a query that starts with "-" a value, not an option.
    return [sys.executable, "-m", "youtube_automanager", "find-channels", f"--query={query}"]


def subscribe_command(channel_id: str, title: str | None = None) -> list[str]:
    command = [sys.executable, "-m", "youtube_automanager", "subscribe", f"--channel-id={channel_id}"]
    if title:
        command.append(f"--title={title}")
    return command


def unsubscribe_command(channel_id: str) -> list[str]:
    return [sys.executable, "-m", "youtube_automanager", "unsubscribe", f"--channel-id={channel_id}"]


# The bodies of the children.


def find_channels_once(query: str) -> int:
    """The body of a channel search in the child process. Return the exit code of the child.

    The child writes the found channels as one JSON list at the end of stderr.
    """
    # The public source imports yt-dlp, so only the child loads it.
    from youtube_automanager.youtube.public import PublicSourceError, find_channels

    try:
        channels = find_channels(query, SEARCH_LIMIT)
    except PublicSourceError as error:
        return fail("The channel search", EXIT_YOUTUBE_ERROR, str(error))
    return succeed(
        [
            dataclasses.asdict(
                FoundChannel(
                    channel_id=channel.channel_id,
                    title=channel.title,
                    handle=channel.handle,
                    subscriber_count=None if channel.subscriber_count is None else int(channel.subscriber_count),
                    thumbnail_url=channel.thumbnail_url,
                )
            )
            for channel in channels
        ]
    )


def change_subscription_once(settings: Settings, channel_id: str, *, subscribe: bool, title: str | None = None) -> int:
    """The body of a subscribe or an unsubscribe in the child process. Return the exit code of the child.

    After YouTube confirms the change, the child writes ``channel.is_subscribed``. A subscribe adds a missing
    ``channel`` row with ``title``, or with the id when ``title`` is empty. An unsubscribe adds no row. The child
    writes the channel id and the new state as one JSON line at the end of stderr.
    """
    from youtube_automanager.youtube._ytdlp import CHANNEL_ID

    if not CHANNEL_ID.fullmatch(channel_id):
        # The pages give only stored or found ids, so another text is a bug. It fails before any request.
        raise ValueError(f"not a YouTube channel id: {channel_id!r}")

    def work(sessions: sessionmaker[Session], client: ClientLike) -> dict:
        if subscribe:
            client.subscribe(channel_id)
        else:
            client.unsubscribe(channel_id)
        _store_subscription(sessions, channel_id, subscribe, title)
        return {"channel_id": channel_id, "subscribed": subscribe}

    return work_with_account(settings, f"The subscription change of channel {channel_id}", work)


def _store_subscription(sessions: sessionmaker[Session], channel_id: str, subscribed: bool, title: str | None) -> None:
    with sessions.begin() as session:
        row = session.get(Channel, channel_id)
        if row is not None:
            row.is_subscribed = subscribed
        elif subscribed:
            session.add(Channel(channel_id=channel_id, title=(title or "").strip() or channel_id, is_subscribed=True))


# The parent.


def find_channels_in_child(runner: SyncRunner, query: str) -> list[FoundChannel]:
    """Find channels in a child process of ``runner`` and wait for it. Raise a ``ChildError`` when it fails.

    Each channel comes once, in the order of YouTube.
    """
    channels = _found_channels(
        run_child(runner, find_channels_command(query), "channel search", SEARCH_TIMEOUT_SECONDS)
    )
    if channels is None:
        raise ChildFailed("The channel search process ended without the found channels.")
    unique: dict[str, FoundChannel] = {}
    for channel in channels:
        unique.setdefault(channel.channel_id, channel)
    return list(unique.values())


def subscribe_in_child(runner: SyncRunner, channel_id: str, title: str | None = None) -> None:
    """Subscribe the account to a channel in a child process of ``runner``. Raise a ``ChildError`` when it fails.

    ``title`` is the title of a new ``channel`` row, for a channel that the stored list does not have.
    """
    _change_in_child(runner, subscribe_command(channel_id, title), channel_id, subscribed=True)


def unsubscribe_in_child(runner: SyncRunner, channel_id: str) -> None:
    """Unsubscribe the account from a channel in a child process of ``runner``. Raise a ``ChildError`` when it fails."""
    _change_in_child(runner, unsubscribe_command(channel_id), channel_id, subscribed=False)


def _change_in_child(runner: SyncRunner, command: list[str], channel_id: str, *, subscribed: bool) -> None:
    data = read_json(run_child(runner, command, "subscription change", SUBSCRIPTION_TIMEOUT_SECONDS))
    # The flag must be the JSON boolean itself, because 1 == True in Python.
    expected = {"channel_id": channel_id, "subscribed": subscribed}
    if not isinstance(data, dict) or data != expected or data["subscribed"] is not subscribed:
        raise ChildFailed("The subscription change process ended without the confirmation of the change.")


def _found_channels(line: str) -> list[FoundChannel] | None:
    """Read the found channels from a line. Return None when the line is not a list of channels."""
    data = read_json(line)
    if not isinstance(data, list):
        return None
    channels: list[FoundChannel] = []
    for item in data:
        channel = _found_channel(item)
        if channel is None:
            return None
        channels.append(channel)
    return channels


def _found_channel(item: object) -> FoundChannel | None:
    names = [field.name for field in dataclasses.fields(FoundChannel)]
    if not isinstance(item, dict) or set(item) != set(names):
        return None
    count = item["subscriber_count"]
    if (
        not isinstance(item["channel_id"], str)
        or not item["channel_id"]
        or not isinstance(item["title"], str)
        or not isinstance(item["handle"], str | None)
        or not (count is None or type(count) is int)
        or not isinstance(item["thumbnail_url"], str | None)
    ):
        return None
    return FoundChannel(**item)


# The rules.


def add_channel_to_rule(
    sessions: sessionmaker[Session],
    rule_id: int,
    channel_id: str,
    *,
    title: str,
    handle: str | None = None,
    thumbnail_url: str | None = None,
    catch_up_since: datetime.date | None = None,
) -> AddOutcome:
    """Make the rule ``rule_id`` read the channel. This call blocks.

    A channel that the stored list does not have gets its ``channel`` row first, with ``is_subscribed`` false. The next
    refresh of the account lists corrects the flag when the account is subscribed. A stored row stays as it is.

    With ``catch_up_since``, the next run also adds the matching videos of the rule since that day. A rule that already
    has an earlier catch-up day keeps it.
    """
    with sessions.begin() as session:
        rule = session.get(Rule, rule_id)
        if rule is None:
            return AddOutcome.NO_RULE
        if session.get(RuleChannel, (rule_id, channel_id)) is not None:
            return AddOutcome.ALREADY_IN_RULE
        if catch_up_since is not None and (rule.catch_up_since is None or catch_up_since < rule.catch_up_since):
            rule.catch_up_since = catch_up_since
        if session.get(Channel, channel_id) is None:
            session.add(
                Channel(
                    channel_id=channel_id,
                    title=title.strip() or channel_id,
                    handle=handle,
                    thumbnail_url=thumbnail_url,
                    is_subscribed=False,
                )
            )
            # The rule_channel row needs the channel row first, because of its foreign key.
            session.flush()
        session.add(RuleChannel(rule_id=rule_id, channel_id=channel_id))
    return AddOutcome.ADDED
