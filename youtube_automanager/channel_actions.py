"""The channel actions of the web pages: a search of channels on YouTube, a subscription change, and a channel for a rule.

The search and the subscription change need yt-dlp, so each runs in a short child process that follows the protocol of
``short_child``:

- ``python -m youtube_automanager find-channels --query=<text>`` finds channels anonymously. It needs no cookies and no
  database, but it holds the sync lock like each short child.
- ``subscribe --channel-id=<id> [--title=<title>]`` and ``unsubscribe --channel-id=<id>`` change a subscription of the
  account. The child works with the account as a refresh does. After YouTube confirms the change, it writes
  ``channel.is_subscribed``, and a subscribe adds the ``channel`` row when the row is missing.
- ``change-subscriptions`` changes the subscription of many channels in one child. It reads the batch as one JSON line
  on its standard input, and it writes each confirmed change as the single change does.

``add_channel_to_rule`` and ``add_channels_to_rule`` send no request to YouTube, so they run in the web process. A rule
knows a channel only by its id, and a ``channel`` row never goes away, because a rule can point at it. ``mark_date`` and
``last_checked_at`` of a channel belong to the sync engine, and this module never writes them.
"""

import dataclasses
import datetime
import enum
import json
import logging
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import Channel, Rule, RuleChannel
from youtube_automanager.pacing import minimum_seconds
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

LOG = logging.getLogger(__name__)

# The search gives at most this many channels.
SEARCH_LIMIT = 20
# A search sends one or two anonymous requests. The time includes the start of the child.
SEARCH_TIMEOUT_SECONDS = 90
# A subscription change sends a session check, a read of the page config and the change, and each takes a token of
# the request budget. Each timeout must stay shorter than ``SCHEDULED_WAIT_S`` of the runner, so a channel action
# never makes a scheduled run give up.
SUBSCRIPTION_TIMEOUT_SECONDS = 120
# A change of a batch that fails at a connection, a timeout or a server error goes again after each of these waits.
RETRY_WAITS_SECONDS = (10, 30)


@dataclass(frozen=True)
class FoundChannel:
    """A channel that the search found. ``handle`` and ``subscriber_count`` are None when YouTube does not show them."""

    channel_id: str
    title: str
    handle: str | None
    subscriber_count: int | None
    thumbnail_url: str | None


@dataclass(frozen=True)
class BatchChannel:
    """A channel of a subscription batch. ``title`` is the title of a new ``channel`` row of a subscribe."""

    channel_id: str
    title: str | None = None


class OutcomeKind(enum.StrEnum):
    # YouTube confirmed the target state, and the child stored it.
    CONFIRMED = "confirmed"
    # YouTube did not confirm the change, so the batch skipped the channel.
    REJECTED = "rejected"
    # The batch stopped at this channel.
    FAILED = "failed"


class StopReason(enum.StrEnum):
    PAUSED = "paused"
    LOGGED_OUT = "logged_out"
    # Each try of the change failed at a connection, a timeout or a server error.
    NETWORK = "network"
    # Another error of YouTube or of yt-dlp.
    ERROR = "error"


@dataclass(frozen=True)
class ChannelOutcome:
    """The outcome of one channel of a batch. ``cause`` names the cause of a rejected or failed change."""

    channel_id: str
    outcome: OutcomeKind
    cause: str | None


@dataclass(frozen=True)
class BatchStop:
    reason: StopReason
    cause: str


@dataclass(frozen=True)
class SubscriptionBatchResult:
    """The result of a batch that ended: the outcome of each channel, in the order of the batch."""

    subscribe: bool
    outcomes: tuple[ChannelOutcome, ...]


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


def change_subscriptions_command() -> list[str]:
    # The batch goes to the standard input, because a long list does not fit a command line.
    return [sys.executable, "-m", "youtube_automanager", "change-subscriptions"]


def batch_request(subscribe: bool, channels: Sequence[BatchChannel]) -> str:
    """Return the batch as the JSON line that ``change-subscriptions`` reads. The line holds only ASCII characters."""
    return json.dumps({"subscribe": subscribe, "channels": [dataclasses.asdict(channel) for channel in channels]})


def parse_batch_request(text: str) -> tuple[bool, list[BatchChannel]]:
    """Read a batch that ``batch_request`` wrote. Raise ``ValueError`` for a batch that is not valid.

    Each channel comes once, in the order of the batch.
    """
    from youtube_automanager.youtube._ytdlp import CHANNEL_ID

    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("the subscription batch is not JSON") from None
    if not isinstance(data, dict) or set(data) != {"subscribe", "channels"}:
        raise ValueError("the subscription batch needs exactly the keys subscribe and channels")
    subscribe, items = data["subscribe"], data["channels"]
    # The flag must be the JSON boolean itself, because 1 == True in Python.
    if type(subscribe) is not bool:
        raise ValueError("subscribe of the subscription batch is not true or false")
    if not isinstance(items, list) or not items:
        raise ValueError("the subscription batch has no list of channels")
    channels = []
    for item in items:
        if not isinstance(item, dict) or not set(item) <= {"channel_id", "title"}:
            raise ValueError(f"not a channel of the subscription batch: {item!r}")
        channel_id, title = item.get("channel_id"), item.get("title")
        # The pages give only stored or found ids, so another text is a bug. It fails before any request.
        if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
            raise ValueError(f"not a YouTube channel id: {channel_id!r}")
        if title is not None and type(title) is not str:
            raise ValueError(f"not a channel title: {title!r}")
        channels.append(BatchChannel(channel_id, title))
    return subscribe, _unique_channels(channels)


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


def change_subscriptions_once(
    settings: Settings, request_text: str, *, sleep: Callable[[float], None] = time.sleep
) -> int:
    """The body of a batch of subscription changes in the child process. Return the exit code of the child.

    ``request_text`` is the batch as one JSON line, as ``batch_request`` gives it. A batch that is not valid fails with
    ``ValueError`` before any request. The child checks the session and reads the page config of the home page once,
    and gives that config to each change. After YouTube confirms the change of a channel, the child writes
    ``channel.is_subscribed`` in its own transaction, as ``change_subscription_once`` does. A confirmed state is a
    success, also when the account was in that state already.

    The child handles each cause of a failed change by its type:

    - YouTube did not confirm the change (``WriteRejectedError``): the child skips the channel and goes on.
    - A failed connection, a timeout or a server error (``NetworkOrServerError``): the child waits each time of
      ``RETRY_WAITS_SECONDS`` and sends the change again. After the last retry fails, the batch stops with
      ``EXIT_YOUTUBE_ERROR``. A retry asks for the same target state, so a change that YouTube made already does no
      harm.
    - A pause of the requests: the batch stops with ``EXIT_PAUSED``.
    - A logged-out answer: the session dies, and the batch stops with ``EXIT_LOGGED_OUT``.
    - Any other ``AccountError``: the batch stops with ``EXIT_YOUTUBE_ERROR``.

    A batch that ends writes its result as one JSON line at the end of stderr: the outcome of each channel, and
    ``stopped`` null. A batch that stops writes the same JSON line with the outcomes so far and the reason of the stop,
    and then the last line of the short child protocol: the cause, or the pause. The changes that YouTube confirmed
    before the stop stay stored. ``sleep`` waits before a retry.
    """
    # The account module imports yt-dlp, so only the child loads it.
    from youtube_automanager.youtube.account import AccountError

    subscribe, channels = parse_batch_request(request_text)

    def work(sessions: sessionmaker[Session], client: ClientLike) -> dict:
        outcomes: list[ChannelOutcome] = []
        current = None
        try:
            ytcfg = client.home_config()
            for channel in channels:
                current = channel.channel_id
                outcome = _change_with_retries(client, channel.channel_id, subscribe, ytcfg, sleep)
                if outcome.outcome is OutcomeKind.CONFIRMED:
                    _store_subscription(sessions, channel.channel_id, subscribe, channel.title)
                outcomes.append(outcome)
        except AccountError as error:
            stop = BatchStop(_stop_reason(error), " ".join(str(error).split()))
            if current is not None:
                outcomes.append(ChannelOutcome(current, OutcomeKind.FAILED, stop.cause))
            LOG.error(
                "The subscription batch stopped after %d of %d channel(s), because of %s: %s",
                len(outcomes),
                len(channels),
                stop.reason,
                stop.cause,
            )
            print(json.dumps(_batch_result(subscribe, outcomes, stop)), file=sys.stderr, flush=True)
            raise
        confirmed = sum(outcome.outcome is OutcomeKind.CONFIRMED for outcome in outcomes)
        LOG.info("The subscription batch ended: %d of %d channel(s) confirmed", confirmed, len(channels))
        return _batch_result(subscribe, outcomes, None)

    return work_with_account(settings, f"The subscription batch of {len(channels)} channel(s)", work)


def _change_with_retries(
    client: ClientLike, channel_id: str, subscribe: bool, ytcfg: dict, sleep: Callable[[float], None]
) -> ChannelOutcome:
    """Change the subscription of one channel. Return its outcome, or raise the error that stops the batch."""
    from youtube_automanager.youtube.account import NetworkOrServerError, WriteRejectedError

    change = client.subscribe if subscribe else client.unsubscribe
    waits = iter(RETRY_WAITS_SECONDS)
    while True:
        try:
            change(channel_id, ytcfg=ytcfg)
        except WriteRejectedError as error:
            cause = " ".join(str(error).split())
            LOG.warning("The subscription batch skips channel %s: %s", channel_id, cause)
            return ChannelOutcome(channel_id, OutcomeKind.REJECTED, cause)
        except NetworkOrServerError as error:
            wait = next(waits, None)
            if wait is None:
                raise
            LOG.warning("The change of channel %s failed, so it goes again in %s s: %s", channel_id, wait, error)
            sleep(wait)
            continue
        return ChannelOutcome(channel_id, OutcomeKind.CONFIRMED, None)


def _stop_reason(error: Exception) -> StopReason:
    from youtube_automanager.youtube.account import AccountError, NetworkOrServerError, NotLoggedInError

    if isinstance(error, NotLoggedInError):
        return StopReason.LOGGED_OUT
    if isinstance(error, AccountError) and error.pause is not None:
        return StopReason.PAUSED
    if isinstance(error, NetworkOrServerError):
        return StopReason.NETWORK
    return StopReason.ERROR


def _batch_result(subscribe: bool, outcomes: Sequence[ChannelOutcome], stop: BatchStop | None) -> dict:
    return {
        "subscribe": subscribe,
        "outcomes": [dataclasses.asdict(outcome) for outcome in outcomes],
        "stopped": None if stop is None else dataclasses.asdict(stop),
    }


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


def batch_timeout_seconds(channels: int) -> float:
    """Return the time limit of a batch of ``channels`` channels.

    The batch sends a session check, a read of the page config and one change for each channel, and the request budget
    lets them go no faster than ``minimum_seconds``. Each channel can also wait each time of ``RETRY_WAITS_SECONDS``.
    The base covers the start of the child and the answers.
    """
    return SUBSCRIPTION_TIMEOUT_SECONDS + minimum_seconds(channels + 2) + channels * sum(RETRY_WAITS_SECONDS)


def change_subscriptions_in_child(
    runner: SyncRunner, subscribe: bool, channels: Sequence[BatchChannel], *, owner: object | None = None
) -> SubscriptionBatchResult:
    """Subscribe or unsubscribe many channels in one child process of ``runner``, and wait for it.

    Each channel goes once, in the order of ``channels``. Return the outcome of each channel. An empty list starts no
    child. Raise a ``ChildError`` when the batch stops: ``ChildPaused`` at a pause of the requests, ``ChildLoggedOut``
    when the session died, and ``ChildYouTubeError`` after the last retry of a failed connection, or at another error.
    With ``owner``, ``runner.kill_short_child(owner)`` kills the child, and the call raises ``ChildFailed``. The changes
    that YouTube confirmed before the stop are stored, and the log of the service gets the outcomes so far.
    """
    unique = _unique_channels(channels)
    if not unique:
        return SubscriptionBatchResult(subscribe, ())
    line = run_child(
        runner,
        change_subscriptions_command(),
        "subscription batch",
        batch_timeout_seconds(len(unique)),
        batch_request(subscribe, unique),
        owner=owner,
    )
    result = _batch_result_from_line(line, subscribe, [channel.channel_id for channel in unique])
    if result is None:
        raise ChildFailed("The subscription batch process ended without the outcome of each channel.")
    return result


def _unique_channels(channels: Sequence[BatchChannel]) -> list[BatchChannel]:
    unique: dict[str, BatchChannel] = {}
    for channel in channels:
        unique.setdefault(channel.channel_id, channel)
    return list(unique.values())


def _batch_result_from_line(line: str, subscribe: bool, channel_ids: list[str]) -> SubscriptionBatchResult | None:
    """Read the result of a batch that ended. Return None unless it gives one outcome for each channel, in order."""
    data = read_json(line)
    if not isinstance(data, dict) or set(data) != {"subscribe", "outcomes", "stopped"}:
        return None
    # The flag must be the JSON boolean itself, because 1 == True in Python.
    if data["subscribe"] is not subscribe or data["stopped"] is not None or not isinstance(data["outcomes"], list):
        return None
    outcomes = []
    for item in data["outcomes"]:
        if not isinstance(item, dict) or set(item) != {"channel_id", "outcome", "cause"}:
            return None
        kind, cause = item["outcome"], item["cause"]
        if kind not in (OutcomeKind.CONFIRMED, OutcomeKind.REJECTED) or not isinstance(cause, str | None):
            return None
        outcomes.append(ChannelOutcome(item["channel_id"], OutcomeKind(kind), cause))
    if [outcome.channel_id for outcome in outcomes] != channel_ids:
        return None
    return SubscriptionBatchResult(subscribe, tuple(outcomes))


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


@dataclass(frozen=True)
class ChannelForRule:
    """A channel for a rule. The title, the handle and the avatar go into a new ``channel`` row only."""

    channel_id: str
    title: str
    handle: str | None = None
    thumbnail_url: str | None = None


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
    """Make the rule ``rule_id`` read the channel, as ``add_channels_to_rule`` does. This call blocks."""
    added = add_channels_to_rule(
        sessions, rule_id, [ChannelForRule(channel_id, title, handle, thumbnail_url)], catch_up_since=catch_up_since
    )
    if added is None:
        return AddOutcome.NO_RULE
    return AddOutcome.ADDED if added else AddOutcome.ALREADY_IN_RULE


def add_channels_to_rule(
    sessions: sessionmaker[Session],
    rule_id: int,
    channels: Sequence[ChannelForRule],
    *,
    catch_up_since: datetime.date | None = None,
) -> int | None:
    """Make the rule ``rule_id`` read each of ``channels`` in one transaction. This call blocks.

    Return the number of channels that the rule did not read before, or None when the rule does not exist. A channel
    that the rule reads already changes nothing.

    A channel that the stored list does not have gets its ``channel`` row first, with ``is_subscribed`` false. The next
    refresh of the account lists corrects the flag when the account is subscribed. A stored row stays as it is.

    With ``catch_up_since``, the next run also adds the matching videos of the rule since that day. A rule that already
    has an earlier catch-up day keeps it, and a rule that gets no new channel keeps its day too.
    """
    with sessions.begin() as session:
        rule = session.get(Rule, rule_id)
        if rule is None:
            return None
        read = set(session.scalars(select(RuleChannel.channel_id).where(RuleChannel.rule_id == rule_id)))
        # A channel that comes twice counts once, with its first data.
        new: dict[str, ChannelForRule] = {}
        for channel in channels:
            if channel.channel_id not in read:
                new.setdefault(channel.channel_id, channel)
        if not new:
            return 0
        if catch_up_since is not None and (rule.catch_up_since is None or catch_up_since < rule.catch_up_since):
            rule.catch_up_since = catch_up_since
        stored = set(session.scalars(select(Channel.channel_id).where(Channel.channel_id.in_(new))))
        session.add_all(
            Channel(
                channel_id=channel.channel_id,
                title=channel.title.strip() or channel.channel_id,
                handle=channel.handle,
                thumbnail_url=channel.thumbnail_url,
                is_subscribed=False,
            )
            for channel in new.values()
            if channel.channel_id not in stored
        )
        # The rule_channel rows need the channel rows first, because of their foreign key.
        session.flush()
        session.add_all(RuleChannel(rule_id=rule_id, channel_id=channel_id) for channel_id in new)
    return len(new)
