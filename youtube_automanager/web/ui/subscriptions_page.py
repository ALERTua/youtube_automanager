"""The subscriptions page: the stored channels with filters, a search of channels on YouTube, and the channel actions.

The table shows the stored copy of the subscriptions and each channel that a rule reads, so a page load sends no
request to YouTube. The title of a channel, in the table or in the search results, opens the channel on YouTube in a
new tab. The last cell of each row holds the actions of the channel: subscribe or unsubscribe, and add the channel to a
rule. The result of an action shows above the table of the stored channels.

The column "Last video" of the stored table shows the newest upload date that ``seen_video`` holds for the channel, so
only a channel that a rule reads has one. A channel without a date shows a dash and sorts as the oldest.

Each row of both tables has a checkbox, and a click on the row outside the link and the actions selects the channel
too. One selection covers both tables, by the channel id. The page keeps it until the page loads again, so it stays when
the filters or the search change. The checkbox of a header selects each row of its table, also the rows on the other
pages, and a second click clears them. When the selection is not empty, a bar above the tables shows its size, the
number of selected stored channels that the filters hide, the bulk Subscribe and Unsubscribe, the bulk "Add to rule"
and "Clear selection".

The bulk Subscribe and Unsubscribe change each selected channel that the action fits, by its stored state, in one child
process. Unsubscribe asks first, with the shortest time that the request budget allows. While the batch runs, the bar
shows how many channels have the new stored state, and Stop kills the child. After the batch, the channels that changed
leave the selection, and a list under the stored table names each other channel.

The search, the subscribe and the unsubscribe run in child processes of the runner, and the page waits for them in a
thread, so the web process never imports yt-dlp. While a run, a session check or another child holds the sync lock,
they start nothing, and the page says so. The page reads and writes the database in a thread, never on the event loop.
"""

import asyncio
import logging
import math
from collections import defaultdict
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import quote

from nicegui import context, ui
from nicegui.events import GenericEventArguments
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.channel_actions import (
    AddOutcome,
    BatchChannel,
    ChannelForRule,
    FoundChannel,
    OutcomeKind,
    add_channel_to_rule,
    add_channels_to_rule,
    change_subscriptions_in_child,
    find_channels_in_child,
    subscribe_in_child,
    unsubscribe_in_child,
)
from youtube_automanager.db.models import Channel, Rule, RuleChannel, SeenVideo
from youtube_automanager.i18n import N_, _
from youtube_automanager.pacing import minimum_seconds
from youtube_automanager.short_child import ChildError, ChildFailed
from youtube_automanager.web.services import Services
from youtube_automanager.web.ui.account_lists import account_lists, child_error_text, child_pause, show_pause_end
from youtube_automanager.web.ui.common import (
    FOCUS_KEY_ATTRIBUTE,
    ROW_BUTTON_COLUMN,
    ROW_SCOPE_ATTRIBUTE,
    ResultLabel,
    actions_column,
    add_keyboard_sort,
    busy,
    column,
    day_column,
    notify_in,
    read_timezone,
    template_text,
)
from youtube_automanager.web.ui.dates import DEFAULT_DAYS_BACK, default_first_day
from youtube_automanager.web.ui.layout import register_page

LOG = logging.getLogger(__name__)

PAGE_SIZE = 50
CHANNEL_URL = "https://www.youtube.com/channel/"
# The value of a filter that lets each channel through.
ANY = "any"
YES = "yes"
NO = "no"
# The source of ``seen_video`` for the subscriptions feed, as ``FEED_SOURCE`` of ``engine.sync``. That module imports
# yt-dlp, which the process of the service never imports. The feed is not a channel, so it gives no date to a channel.
FEED_SOURCE = "feed"

YES_NO = {True: N_("Yes"), False: N_("No")}
SUBSCRIBED_FILTER = {ANY: N_("All"), YES: N_("Subscribed"), NO: N_("Not subscribed")}
RULES_FILTER = {ANY: N_("All"), YES: N_("With rules"), NO: N_("Without rules")}

# The scopes of the two tables, so the head script brings the focus back to the button of the right table.
FOUND_SCOPE = "found"
CHANNELS_SCOPE = "channels"

# The events of the actions of a row. Each one sends the id of the channel, and the add to a rule also the id of the
# rule.
SUBSCRIBE_EVENT = "subscribeChannel"
UNSUBSCRIBE_EVENT = "unsubscribeChannel"
ADD_TO_RULE_EVENT = "addChannelToRule"
CHANNEL_JS_HANDLER = "(id) => emit(id)"
RULE_JS_HANDLER = "(id, rule) => emit(id, rule)"

# The column of the checkbox of each row, and the events of the checkboxes. A click on a row sends only its id.
SELECT_COLUMN = "selected"
TOGGLE_EVENT = "toggleChannel"
TOGGLE_ALL_EVENT = "toggleAllChannels"
ROW_CLICK_JS_HANDLER = "(event, row) => emit(row.id)"
# A selected row has the class "selected" of Quasar, as in a table with its own selection.
SELECTED_ROW_CLASS = "(row) => (row.selected ? 'selected' : '')"
# The bar of the selection stays at the top of the window, below the header, while the page scrolls. The fixed header
# of the layout is 74 px high, measured in Firefox.
SELECTION_BAR_STYLE = "position: sticky; top: 74px; z-index: 10"
# How often the page reads the stored states of the channels of a running batch for its counter. The child gives no
# progress before it ends, but it stores each confirmed change at once.
BATCH_POLL_SECONDS = 2.0
# A batch sends a session check and a read of the page config before its changes, one request each.
BATCH_EXTRA_REQUESTS = 2

type OnRowEvent = Callable[[GenericEventArguments], Any]

# The title cell shows the avatar of the channel in front of the title, and the title is the link to the channel on
# YouTube. The click on the link does not reach the row.
TITLE_CELL = """
<q-td :props="props">
  <div class="row items-center no-wrap q-gutter-sm">
    <q-avatar v-if="props.row.thumbnail" size="28px">
      <img :src="props.row.thumbnail" referrerpolicy="no-referrer">
    </q-avatar>
    <a :href="props.row.url" target="_blank" rel="noopener noreferrer" @click.stop>{{ props.value }}\
<q-icon name="open_in_new" size="xs" class="q-ml-xs" /></a>
  </div>
</q-td>
"""


@dataclass(frozen=True)
class ChannelRecord:
    channel_id: str
    title: str
    handle: str | None
    thumbnail_url: str | None
    is_subscribed: bool
    # The newest upload date among the videos of the channel in ``seen_video``. None when the service saw no such video.
    last_video: date | None = None


@dataclass(frozen=True)
class RuleRecord:
    id: int
    name: str
    enabled: bool
    channel_ids: frozenset[str]


@dataclass(frozen=True)
class StoredLists:
    """The stored channels, sorted by their title, and the rules, in their order."""

    channels: list[ChannelRecord]
    rules: list[RuleRecord]

    def channel(self, channel_id: str) -> ChannelRecord | None:
        return next((channel for channel in self.channels if channel.channel_id == channel_id), None)

    def rules_of(self, channel_id: str) -> list[RuleRecord]:
        return [rule for rule in self.rules if channel_id in rule.channel_ids]


@dataclass(frozen=True)
class ChannelFilter:
    # A part of the title or of the handle, in any case. The "@" of a handle is optional.
    text: str = ""
    subscribed: bool | None = None
    with_rules: bool | None = None
    # Only these channels, or each channel when None.
    only: frozenset[str] | None = None


@dataclass(frozen=True)
class ShownChannel:
    """The channel of a row action. ``stored`` is its stored row, and ``found`` is its search result."""

    channel_id: str
    stored: ChannelRecord | None
    found: FoundChannel | None

    @property
    def title(self) -> str:
        if self.stored is not None:
            return self.stored.title
        # A row is a stored channel or a found channel, so one of the two is here.
        return "" if self.found is None else self.found.title

    @property
    def handle(self) -> str | None:
        if self.stored is not None and self.stored.handle:
            return self.stored.handle
        return None if self.found is None else self.found.handle

    @property
    def thumbnail_url(self) -> str | None:
        if self.stored is not None and self.stored.thumbnail_url:
            return self.stored.thumbnail_url
        return None if self.found is None else self.found.thumbnail_url


@dataclass(eq=False)
class Batch:
    """A subscribe or an unsubscribe of many channels that the page runs in one child process.

    The batch is also the owner of its child, so Stop kills only the child of this batch. ``done`` is the number of its
    channels whose stored state was the target at the last read.
    """

    subscribe: bool
    channels: list[ShownChannel]
    done: int = 0
    # True after Stop killed the child.
    killed: bool = False

    @property
    def channel_ids(self) -> list[str]:
        return [channel.channel_id for channel in self.channels]


@dataclass(frozen=True)
class UnchangedChannel:
    """A channel of the last batch that did not change. ``cause`` is the answer of YouTube, or None when the page does
    not know it."""

    title: str
    cause: str | None = None


@dataclass
class PageState:
    lists: StoredLists
    # The channels of the last search, in the order of YouTube. None before the first search.
    found: list[FoundChannel] | None = None
    search_error: str | None = None
    # The channel whose subscription change of this page runs. The sync lock lets one child run at a time, so the
    # page starts no second change while one runs.
    working_id: str | None = None
    # The number of the newest read. A read that finishes after a newer one started drops its result.
    lists_read: int = 0
    # The selected channels of both tables, by their id, in the order of the selection. Each one keeps the channel of
    # its row, so a found channel that is not stored can go into a rule after a new search.
    selected: dict[str, ShownChannel] = field(default_factory=dict)
    # The batch of this page that runs. The page starts no other subscription change while it runs.
    batch: Batch | None = None
    # The channels of the last batch that did not change, in the order of the batch.
    unchanged: list[UnchangedChannel] = field(default_factory=list)

    @property
    def changing(self) -> bool:
        """True while a subscription change of this page runs, for one channel or for a batch."""
        return self.working_id is not None or self.batch is not None


def read_lists(sessions: sessionmaker[Session]) -> StoredLists:
    """Read the stored channels, with the date of their newest seen video, and the rules with their channels.

    One grouped query gives the dates of all channels. The service asks YouTube for nothing, so a channel that no rule
    reads has no date. This call blocks.
    """
    with sessions() as session:
        newest = {
            channel_id: upload_date
            for channel_id, upload_date in session.execute(
                select(SeenVideo.channel_id, func.max(SeenVideo.upload_date))
                .where(SeenVideo.channel_id != FEED_SOURCE)
                .group_by(SeenVideo.channel_id)
            )
            if upload_date is not None
        }
        channels = [
            ChannelRecord(
                row.channel_id,
                row.title,
                row.handle,
                row.thumbnail_url,
                row.is_subscribed,
                newest.get(row.channel_id),
            )
            for row in session.scalars(select(Channel))
        ]
        links: dict[int, set[str]] = defaultdict(set)
        for rule_id, channel_id in session.execute(select(RuleChannel.rule_id, RuleChannel.channel_id)):
            links[rule_id].add(channel_id)
        rules = [
            RuleRecord(rule.id, rule.name, rule.enabled, frozenset(links[rule.id]))
            for rule in session.scalars(select(Rule).order_by(Rule.position, Rule.id))
        ]
    channels.sort(key=lambda channel: (channel.title.casefold(), channel.channel_id))
    return StoredLists(channels, rules)


def add_to_rule(
    sessions: sessionmaker[Session], default_timezone: str, rule_id: int, channel: ShownChannel
) -> AddOutcome:
    """Make the rule read the channel, and let the next run also add the videos of the last DEFAULT_DAYS_BACK days.

    The day is a day in the time zone of the schedule. This call blocks.
    """
    timezone = read_timezone(sessions, default_timezone)
    return add_channel_to_rule(
        sessions,
        rule_id,
        channel.channel_id,
        title=channel.title,
        handle=channel.handle,
        thumbnail_url=channel.thumbnail_url,
        catch_up_since=default_first_day(timezone),
    )


def add_many_to_rule(
    sessions: sessionmaker[Session], default_timezone: str, rule_id: int, channels: Sequence[ShownChannel]
) -> int | None:
    """Make the rule read each of ``channels`` in one transaction, as ``add_to_rule`` does for one channel.

    Return the number of channels that the rule did not read before, or None when the rule does not exist. This call
    blocks.
    """
    timezone = read_timezone(sessions, default_timezone)
    return add_channels_to_rule(
        sessions,
        rule_id,
        [
            ChannelForRule(channel.channel_id, channel.title, channel.handle, channel.thumbnail_url)
            for channel in channels
        ],
        catch_up_since=default_first_day(timezone),
    )


def read_states(sessions: sessionmaker[Session], channel_ids: Collection[str]) -> dict[str, bool]:
    """Read the stored subscription state of each of ``channel_ids`` that has a ``channel`` row. This call blocks."""
    query = select(Channel.channel_id, Channel.is_subscribed).where(Channel.channel_id.in_(list(channel_ids)))
    with sessions() as session:
        return {channel_id: subscribed for channel_id, subscribed in session.execute(query)}


def changed_channels(states: dict[str, bool], channel_ids: Collection[str], subscribe: bool) -> set[str]:
    """Return each of ``channel_ids`` whose stored state in ``states`` is the target. A channel without a row is not
    subscribed."""
    return {channel_id for channel_id in channel_ids if states.get(channel_id, False) is subscribe}


def fitting_channels(lists: StoredLists, channel_ids: Collection[str], subscribe: bool) -> list[str]:
    """Return each of ``channel_ids`` whose stored state is not the target, in their order.

    A subscribe fits each channel that is not subscribed, also a found channel that is not stored, and an unsubscribe
    fits each subscribed channel.
    """
    subscribed = {channel.channel_id for channel in lists.channels if channel.is_subscribed}
    return [channel_id for channel_id in channel_ids if (channel_id in subscribed) is not subscribe]


def duration_text(seconds: float) -> str:
    """Return ``seconds``, rounded up to a whole second, as minutes and seconds."""
    minutes, rest = divmod(math.ceil(seconds), 60)
    if not minutes:
        # Translators: a time in seconds.
        return _("{seconds} s").format(seconds=rest)
    if not rest:
        # Translators: a time in minutes.
        return _("{minutes} min").format(minutes=minutes)
    # Translators: a time in minutes and seconds.
    return _("{minutes} min {seconds} s").format(minutes=minutes, seconds=rest)


def batch_time_text(count: int) -> str:
    """Return the shortest time of a batch of ``count`` channels that the request budget allows."""
    duration = duration_text(minimum_seconds(count + BATCH_EXTRA_REQUESTS))
    # Translators: {duration} is a time, for example "2 min 12 s".
    return _(
        "This takes at least {duration}, because the service sends the requests with cookies at a limited rate."
    ).format(duration=duration)


def progress_text(subscribe: bool, done: int, total: int) -> str:
    """Return the counter of a running batch: the channels with the new stored state, of each channel of the batch."""
    if subscribe:
        return _("Subscribing: {done} of {total}").format(done=done, total=total)
    return _("Unsubscribing: {done} of {total}").format(done=done, total=total)


def batch_result_text(subscribe: bool, done: int, total: int) -> str:
    if subscribe:
        return _("Subscribed to channels: {done} of {total}.").format(done=done, total=total)
    return _("Unsubscribed from channels: {done} of {total}.").format(done=done, total=total)


def unchanged_text(channel: UnchangedChannel) -> str:
    if channel.cause is None:
        return channel.title
    # Translators: {cause} is the English answer of YouTube.
    return _("{title}: YouTube did not confirm the change: {cause}").format(title=channel.title, cause=channel.cause)


def filter_channels(lists: StoredLists, channel_filter: ChannelFilter) -> list[ChannelRecord]:
    """Return the stored channels that match the filter, in the order of the list."""
    needle = channel_filter.text.strip().casefold().removeprefix("@")
    with_rules = {channel_id for rule in lists.rules for channel_id in rule.channel_ids}
    matches = []
    for channel in lists.channels:
        if channel_filter.only is not None and channel.channel_id not in channel_filter.only:
            continue
        handle = (channel.handle or "").casefold().removeprefix("@")
        if needle and needle not in channel.title.casefold() and needle not in handle:
            continue
        if channel_filter.subscribed is not None and channel.is_subscribed is not channel_filter.subscribed:
            continue
        if (
            channel_filter.with_rules is not None
            and (channel.channel_id in with_rules) is not channel_filter.with_rules
        ):
            continue
        matches.append(channel)
    return matches


def rule_label(rule: RuleRecord) -> str:
    if rule.enabled:
        return rule.name
    # Translators: the name of a rule that is turned off.
    return _("{name} (off)").format(name=rule.name)


def channel_url(channel_id: str) -> str:
    return CHANNEL_URL + quote(channel_id, safe="")


def format_count(count: int | None) -> str:
    return "" if count is None else f"{count:,}"


def action_fields(
    channel_id: str, title: str, is_subscribed: bool, lists: StoredLists, working_id: str | None, locked: bool = False
) -> dict:
    """Return the fields of a row that the cell of the actions reads.

    The texts with the title of the channel or the name of a rule come as values of the row, so no such text runs in
    the template. ``working_id`` is the channel whose subscription change runs, or None. ``locked`` turns the
    subscription buttons off also without such a channel, for example while a batch runs.
    """
    return {
        "is_subscribed": is_subscribed,
        "unsubscribe_question": _("Unsubscribe the YouTube account from {title}?").format(title=title),
        "unsubscribe_note": (
            _("The rules that name this channel still read its videos.") if lists.rules_of(channel_id) else ""
        ),
        "choices": [
            {
                "id": rule.id,
                "label": rule_label(rule),
                "reads": channel_id in rule.channel_ids,
                # A rule without channels reads the subscriptions feed, and the first channel ends that.
                "feed": not rule.channel_ids,
            }
            for rule in lists.rules
        ],
        "working": channel_id == working_id,
        "locked": locked or working_id is not None,
    }


def channel_row(
    channel: ChannelRecord, lists: StoredLists, working_id: str | None, selected: bool = False, locked: bool = False
) -> dict:
    return {
        "id": channel.channel_id,
        "selected": selected,
        "title": channel.title,
        "url": channel_url(channel.channel_id),
        "thumbnail": channel.thumbnail_url or "",
        "handle": channel.handle or "",
        "subscribed": _(YES_NO[channel.is_subscribed]),
        "rules": ", ".join(rule_label(rule) for rule in lists.rules_of(channel.channel_id)),
        # The day as ``YYYY-MM-DD``, or an empty text. The browser shows the day in its own form, see ``day_column``.
        "last_video": "" if channel.last_video is None else channel.last_video.isoformat(),
        **action_fields(channel.channel_id, channel.title, channel.is_subscribed, lists, working_id, locked),
    }


def found_row(
    found: FoundChannel, lists: StoredLists, working_id: str | None, selected: bool = False, locked: bool = False
) -> dict:
    stored = lists.channel(found.channel_id)
    is_subscribed = stored is not None and stored.is_subscribed
    return {
        "id": found.channel_id,
        "selected": selected,
        "title": found.title,
        "url": channel_url(found.channel_id),
        "thumbnail": found.thumbnail_url or "",
        "handle": found.handle or "",
        "subscribers": format_count(found.subscriber_count),
        "subscribed": _(YES_NO[is_subscribed]),
        **action_fields(found.channel_id, found.title, is_subscribed, lists, working_id, locked),
    }


def actions_cell(scope: str) -> str:
    """Return the Vue template of the cell of the actions of a row, in the table with ``scope``.

    While the subscription change of the row runs, the cell shows a spinner and a note. Else it shows Subscribe or
    Unsubscribe, by the stored state, and "Add to rule". Subscribe starts at once. Unsubscribe and "Add to rule" open a
    menu next to the button: the question before the unsubscribe, or the rules. A rule that reads the channel already
    cannot be chosen. While a change of any channel runs, the buttons Subscribe and Unsubscribe are off.

    Each action is a button that the Tab key reaches, and Escape closes a menu. The click on the cell does not reach the
    row. The subscription button carries the id of the row, so the head script gives it the focus again after the
    redraw of the row. The button "Add to rule" has a key of ``focus_key`` for the same reason.
    """
    texts = {
        name: template_text(text)
        for name, text in {
            "working": _("The service works with YouTube. This takes up to a minute."),
            "subscribe": _("Subscribe"),
            "unsubscribe": _("Unsubscribe"),
            "cancel": _("Cancel"),
            "add": _("Add to rule"),
            "add_title": _("Add the channel to a rule"),
            "no_rule": _("No rule exists yet. Create a rule first."),
            "reads": _("already reads"),
            "feed": _(
                "This rule has no channels now, so it reads the subscriptions feed. After this change, it reads only "
                "its channels, and not the subscriptions feed."
            ),
            "catch_up": _(
                "The next run adds the videos of this channel from the last {days} days that the rule matches. After "
                "that, the rule adds each new video of the channel. To add older videos, open the rule and change Also "
                "add videos from."
            ).format(days=DEFAULT_DAYS_BACK),
        }.items()
    }
    return f"""
<q-td :props="props" auto-width @click.stop>
  <div v-if="props.row.working" class="row items-center no-wrap q-gutter-sm">
    <q-spinner size="sm" />
    <span>{texts["working"]}</span>
  </div>
  <div v-else class="row items-center justify-end no-wrap q-gutter-sm">
    <q-btn v-if="props.row.is_subscribed" outline dense no-caps color="negative" icon="remove_circle" \
label="{texts["unsubscribe"]}" :disable="props.row.locked" :data-row-button="props.row.id">
      <q-menu anchor="bottom right" self="top right">
        <q-card flat class="q-pa-md column q-gutter-sm" style="max-width: 22rem">
          <div class="text-subtitle1" style="white-space: normal">{{{{ props.row.unsubscribe_question }}}}</div>
          <div v-if="props.row.unsubscribe_note" style="white-space: normal">{{{{ props.row.unsubscribe_note }}}}</div>
          <div class="row q-gutter-sm">
            <q-btn color="negative" icon="remove_circle" label="{texts["unsubscribe"]}" v-close-popup \
@click="() => $parent.$emit('{UNSUBSCRIBE_EVENT}', props.row.id)" />
            <q-btn flat label="{texts["cancel"]}" v-close-popup />
          </div>
        </q-card>
      </q-menu>
    </q-btn>
    <q-btn v-else outline dense no-caps icon="add_circle" label="{texts["subscribe"]}" :disable="props.row.locked" \
:data-row-button="props.row.id" @click="() => $parent.$emit('{SUBSCRIBE_EVENT}', props.row.id)" />
    <q-btn outline dense no-caps icon="playlist_add" icon-right="arrow_drop_down" label="{texts["add"]}" \
:{FOCUS_KEY_ATTRIBUTE}="'{scope}-add-' + props.row.id">
      <q-menu anchor="bottom right" self="top right">
        <q-list style="max-width: 24rem">
          <q-item-label header>{texts["add_title"]}</q-item-label>
          <q-item v-if="!props.row.choices.length"><q-item-section>{texts["no_rule"]}</q-item-section></q-item>
          <q-item v-for="choice in props.row.choices" :key="choice.id" clickable v-close-popup \
:disable="choice.reads" @click="() => $parent.$emit('{ADD_TO_RULE_EVENT}', props.row.id, choice.id)">
            <q-item-section>
              <q-item-label>{{{{ choice.label }}}}</q-item-label>
              <q-item-label v-if="choice.feed" caption class="text-warning" style="white-space: normal">\
{texts["feed"]}</q-item-label>
            </q-item-section>
            <q-item-section v-if="choice.reads" side>{texts["reads"]}</q-item-section>
          </q-item>
          <q-item-label v-if="props.row.choices.length" caption class="q-px-md q-pb-sm" style="white-space: normal">\
{texts["catch_up"]}</q-item-label>
        </q-list>
      </q-menu>
    </q-btn>
  </div>
</q-td>
"""


def add_actions(
    table: ui.table, scope: str, on_subscribe: OnRowEvent, on_unsubscribe: OnRowEvent, on_add: OnRowEvent
) -> None:
    """Put the actions of ``actions_cell`` into the last column of ``table``, and connect their events."""
    table.props[ROW_SCOPE_ATTRIBUTE] = scope
    table.add_slot(f"body-cell-{ROW_BUTTON_COLUMN}", actions_cell(scope))
    table.on(SUBSCRIBE_EVENT, on_subscribe, js_handler=CHANNEL_JS_HANDLER)
    table.on(UNSUBSCRIBE_EVENT, on_unsubscribe, js_handler=CHANNEL_JS_HANDLER)
    table.on(ADD_TO_RULE_EVENT, on_add, js_handler=RULE_JS_HANDLER)


def select_cell(scope: str) -> str:
    """Return the Vue template of the checkbox of a row, in the table with ``scope``.

    The title of the channel is the name of the checkbox for a screen reader. The checkbox stops its click, so the click
    does not reach the row, and a click on the row outside it selects the row once. The checkbox has a key of
    ``focus_key``, because each change of the selection draws the rows again.
    """
    return f"""
<q-td :props="props" auto-width>
  <q-checkbox dense :model-value="props.row.selected" :aria-label="props.row.title" \
:{FOCUS_KEY_ATTRIBUTE}="'{scope}-select-' + props.row.id" \
@update:model-value="() => $parent.$emit('{TOGGLE_EVENT}', props.row.id)" />
</q-td>
"""


def select_header(scope: str) -> str:
    """Return the Vue template of the checkbox in the header of the table with ``scope``.

    The column of ``select_column`` gives its state: on, off, or partial (null). A click sends no rows, because the
    page selects each row of the table, also the rows on the other pages.
    """
    return f"""
<q-th :props="props" auto-width>
  <q-checkbox dense :model-value="props.col.select_state" :disable="!props.col.selectable" \
:aria-label="props.col.select_label" :{FOCUS_KEY_ATTRIBUTE}="'{scope}-select-all'" \
@update:model-value="() => $parent.$emit('{TOGGLE_ALL_EVENT}')" />
  <q-tooltip>{{{{ props.col.select_label }}}}</q-tooltip>
</q-th>
"""


def add_selection(table: ui.table, scope: str, on_toggle: OnRowEvent, on_toggle_all: Callable[[], Any]) -> None:
    """Put the checkboxes of ``select_cell`` and ``select_header`` into the first column of ``table``, and let a click
    on a row select it too."""
    table.add_slot(f"body-cell-{SELECT_COLUMN}", select_cell(scope))
    table.add_slot(f"header-cell-{SELECT_COLUMN}", select_header(scope))
    table.props[":table-row-class-fn"] = SELECTED_ROW_CLASS
    table.on(TOGGLE_EVENT, on_toggle, js_handler=CHANNEL_JS_HANDLER)
    table.on("rowClick", on_toggle, js_handler=ROW_CLICK_JS_HANDLER)
    table.on(TOGGLE_ALL_EVENT, on_toggle_all)


def select_column(label: str, channel_ids: Collection[str], selected: Collection[str]) -> dict:
    """Return the column of the checkboxes. ``channel_ids`` are the channels of each row of the table, on each page.

    The checkbox of the header is on when each of them is selected, off when none is, and partial between. ``label``
    names it for a screen reader and in its tooltip.
    """
    count = sum(channel_id in selected for channel_id in channel_ids)
    return {
        **column(SELECT_COLUMN, _("Select")),
        "select_state": None if 0 < count < len(channel_ids) else count > 0,
        "selectable": bool(channel_ids),
        "select_label": label,
    }


def last_video_cell() -> str:
    """Return the Vue template of the cell "Last video" of the stored table.

    A channel with a date shows the day that the column formats in the form of the browser. A channel without a date
    shows a dash, and its tooltip says why. The dash has the text as its name for a screen reader.
    """
    text = template_text(_("The service does not read this channel"))
    return f"""
<q-td :props="props">
  <span v-if="props.row.last_video">{{{{ props.value }}}}</span>
  <span v-else role="img" aria-label="{text}">—<q-tooltip>{text}</q-tooltip></span>
</q-td>
"""


def channel_columns(channel_ids: Collection[str] = (), selected: Collection[str] = ()) -> list[dict]:
    """Return the columns of the stored table. ``channel_ids`` are the channels that the filters show."""
    # Translators: the name of the checkbox in the header of the table. {count} is the number of the channels.
    label = _("Select each channel that the filters show, also on the other pages ({count})")
    return [
        select_column(label.format(count=len(channel_ids)), channel_ids, selected),
        column("title", _("Channel")),
        column("handle", _("Handle")),
        column("subscribed", _("Subscribed")),
        column("rules", _("Rules")),
        day_column("last_video", _("Last video"), sortable=True),
        actions_column(),
    ]


def found_columns(channel_ids: Collection[str] = (), selected: Collection[str] = ()) -> list[dict]:
    """Return the columns of the found table. ``channel_ids`` are the found channels."""
    # Translators: the name of the checkbox in the header of the table. {count} is the number of the channels.
    label = _("Select each found channel ({count})")
    return [
        select_column(label.format(count=len(channel_ids)), channel_ids, selected),
        column("title", _("Channel")),
        column("handle", _("Handle")),
        column("subscribers", _("Subscribers")),
        column("subscribed", _("Subscribed")),
        actions_column(),
    ]


def rule_menu(lists: StoredLists, channel_ids: Collection[str], on_choose: Callable[[int], Any]) -> None:
    """Draw the items of the menu "Add to rule" of the selection bar: one item for each rule.

    A rule that reads each of ``channel_ids`` already cannot be chosen and says so. A rule that reads some of them
    shows how many it would add. A rule without channels warns that it stops to read the subscriptions feed, and the
    note about the catch-up comes last. ``on_choose`` gets the id of the chosen rule.
    """
    with ui.list().style("max-width: 24rem"):
        ui.item_label(_("Add the selected channels to a rule")).props("header")
        if not lists.rules:
            with ui.item():
                ui.item_section(_("No rule exists yet. Create a rule first."))
            return
        for rule in lists.rules:
            missing = sum(channel_id not in rule.channel_ids for channel_id in channel_ids)
            with ui.item(on_click=lambda rule_id=rule.id: on_choose(rule_id)).mark(
                "bulk-rule", f"bulk-rule:{rule.id}"
            ) as item:
                item.set_enabled(missing > 0)
                with ui.item_section():
                    ui.item_label(rule_label(rule))
                    if not rule.channel_ids:
                        ui.item_label(
                            _(
                                "This rule has no channels now, so it reads the subscriptions feed. After this change, "
                                "it reads only its channels, and not the subscriptions feed."
                            )
                        ).props("caption").classes("text-warning").style("white-space: normal").mark(
                            f"bulk-rule-feed:{rule.id}"
                        )
                if not missing:
                    ui.item_section(_("already reads")).props("side").mark(f"bulk-rule-note:{rule.id}")
                elif missing < len(channel_ids):
                    # The number of the selected channels that the rule would add.
                    ui.item_section(f"+{missing}").props("side").mark(f"bulk-rule-note:{rule.id}")
        ui.item_label(
            _(
                "The next run adds the videos of these channels from the last {days} days that the rule matches. "
                "After that, the rule adds each new video of the channels. To add older videos, open the rule and "
                "change Also add videos from."
            ).format(days=DEFAULT_DAYS_BACK)
        ).props("caption").classes("q-px-md q-pb-sm").style("white-space: normal").mark("bulk-catch-up")


def _choice(value: str) -> bool | None:
    return None if value == ANY else value == YES


def _options(labels: dict[str, str]) -> dict[str, str]:
    return {value: _(label) for value, label in labels.items()}


@register_page("/subscriptions", label=N_("Subscriptions"), icon="subscriptions")
async def subscriptions_page(services: Services) -> None:
    state = PageState(lists=await asyncio.to_thread(read_lists, services.sessions))
    # A redraw of the found channels deletes their table, whose events start the actions, so each action tells its
    # result through the client of the page.
    client = context.client
    # The table of the found channels, while the page shows one.
    found_table: ui.table | None = None

    def shown_channel(channel_id: str) -> ShownChannel | None:
        stored = state.lists.channel(channel_id)
        found = next((channel for channel in state.found or [] if channel.channel_id == channel_id), None)
        if stored is None and found is None:
            return None
        return ShownChannel(channel_id, stored, found)

    def read_filter() -> ChannelFilter:
        return ChannelFilter(
            text=text_input.value or "",
            subscribed=_choice(subscribed_select.value),
            with_rules=_choice(rules_select.value),
            only=frozenset(state.selected) if only_selected.value else None,
        )

    def show_channels() -> None:
        lists = state.lists
        channels = filter_channels(lists, read_filter())
        shown_ids = [channel.channel_id for channel in channels]
        channels_table.columns = channel_columns(shown_ids, state.selected)
        channels_table.rows = [
            channel_row(channel, lists, state.working_id, channel.channel_id in state.selected, state.changing)
            for channel in channels
        ]
        summary.set_text(_("{shown} of {total} channels").format(shown=len(channels), total=len(lists.channels)))
        # A filter that matches nothing and a service without stored channels need different next steps.
        no_channels.set_text(
            _("No channel matches the filters.")
            if lists.channels
            else _("No channel is stored yet. Press Refresh lists.")
        )
        no_channels.set_visibility(not channels)
        show_bar(set(shown_ids))

    def show_bar(shown_ids: set[str]) -> None:
        count = len(state.selected)
        stored_ids = {channel.channel_id for channel in state.lists.channels}
        hidden = sum(channel_id in stored_ids and channel_id not in shown_ids for channel_id in state.selected)
        selection_bar.set_visibility(count > 0 or state.batch is not None)
        selected_label.set_text(_("Selected: {count}").format(count=count))
        # Translators: {count} is the number of the selected stored channels that the filters do not show.
        hidden_label.set_text(_("({count} hidden by the filters)").format(count=hidden))
        hidden_label.set_visibility(hidden > 0)
        to_subscribe = len(fitting_channels(state.lists, state.selected, subscribe=True))
        to_unsubscribe = len(fitting_channels(state.lists, state.selected, subscribe=False))
        # Translators: {count} is the number of the selected channels that the account is not subscribed to.
        bulk_subscribe_button.set_text(_("Subscribe ({count})").format(count=to_subscribe))
        bulk_subscribe_button.set_enabled(to_subscribe > 0 and not state.changing)
        # Translators: {count} is the number of the selected channels that the account is subscribed to.
        bulk_unsubscribe_button.set_text(_("Unsubscribe ({count})").format(count=to_unsubscribe))
        bulk_unsubscribe_button.set_enabled(to_unsubscribe > 0 and not state.changing)
        show_progress()
        bulk_menu_view.refresh()
        bulk_unsubscribe_view.refresh()

    def show_progress() -> None:
        """Show the counter and Stop of the running batch instead of the actions of the bar, or the actions again."""
        batch = state.batch
        selection_actions.set_visibility(batch is None)
        progress_row.set_visibility(batch is not None)
        if batch is None:
            return
        progress_label.set_text(progress_text(batch.subscribe, batch.done, len(batch.channels)))
        stop_button.set_enabled(not batch.killed)

    def show_found() -> None:
        if found_table is None or state.found is None:
            return
        found_ids = [channel.channel_id for channel in state.found]
        found_table.columns = found_columns(found_ids, state.selected)
        found_table.rows = [
            found_row(channel, state.lists, state.working_id, channel.channel_id in state.selected, state.changing)
            for channel in state.found
        ]

    def show_selection() -> None:
        show_channels()
        show_found()

    def show_rows() -> None:
        show_channels()
        results_view.refresh()

    def toggle_channel(event: GenericEventArguments) -> None:
        channel_id = str(event.args)
        if channel_id in state.selected:
            del state.selected[channel_id]
        else:
            channel = shown_channel(channel_id)
            if channel is None:
                return
            state.selected[channel_id] = channel
        show_selection()

    def toggle_many(channels: list[ShownChannel]) -> None:
        """Select each of ``channels``. When each of them is selected already, clear them instead."""
        if channels and all(channel.channel_id in state.selected for channel in channels):
            for channel in channels:
                del state.selected[channel.channel_id]
        else:
            for channel in channels:
                state.selected.setdefault(channel.channel_id, channel)
        show_selection()

    def toggle_shown_channels() -> None:
        channels = filter_channels(state.lists, read_filter())
        toggle_many([ShownChannel(channel.channel_id, channel, None) for channel in channels])

    def toggle_found_channels() -> None:
        found = state.found or []
        toggle_many(
            [ShownChannel(channel.channel_id, state.lists.channel(channel.channel_id), channel) for channel in found]
        )

    def clear_selection() -> None:
        state.selected.clear()
        show_selection()

    async def reload() -> None:
        state.lists_read += 1
        read = state.lists_read
        lists = await asyncio.to_thread(read_lists, services.sessions)
        if read != state.lists_read:
            return
        state.lists = lists
        show_rows()

    async def search() -> None:
        query = (search_input.value or "").strip()
        if not query:
            state.found, state.search_error = None, _("Enter a name, an @handle, a channel ID or a channel URL.")
            results_view.refresh()
            return
        with busy(search_button):
            try:
                found = await asyncio.to_thread(find_channels_in_child, services.runner, query)
            except ChildError as error:
                LOG.warning("The channel search failed: %s", error)
                state.found = None
                state.search_error = _("The search failed. {reason}").format(reason=child_error_text(error))
            else:
                state.found, state.search_error = found, None
        results_view.refresh()

    async def change_subscription(channel_id: str, subscribe: bool) -> None:
        channel = shown_channel(channel_id)
        if channel is None or state.changing:
            return
        state.working_id = channel_id
        result_label.ok("")
        state.unchanged = []
        unchanged_view.refresh()
        show_rows()
        try:
            if subscribe:
                await asyncio.to_thread(subscribe_in_child, services.runner, channel_id, channel.title)
            else:
                await asyncio.to_thread(unsubscribe_in_child, services.runner, channel_id)
        except ChildError as error:
            LOG.warning("The subscription of channel %s did not change: %s", channel_id, error)
            text = _("The subscription did not change. {reason}").format(reason=child_error_text(error))
            result_label.problem(text)
            show_pause_end(result_label, text, child_pause(error))
        else:
            if subscribe:
                text = _("Subscribed to {title}.").format(title=channel.title)
            else:
                text = _("Unsubscribed from {title}.").format(title=channel.title)
            result_label.ok(text)
            notify_in(client, text, type="positive")
        finally:
            state.working_id = None
        await reload()

    async def change_selected(subscribe: bool) -> None:
        """Subscribe or unsubscribe each selected channel that the action fits, in one child process.

        While the child runs, the timer of the batch counts the channels with the new stored state. After the child,
        each channel that changed leaves the selection, and the list under the table names each other channel.
        """
        if state.changing:
            return
        # The stored row of a channel is newer than the row that the selection keeps.
        channels = [
            shown_channel(channel_id) or state.selected[channel_id]
            for channel_id in fitting_channels(state.lists, state.selected, subscribe)
        ]
        bulk_unsubscribe_menu.close()
        if not channels:
            return
        batch = Batch(subscribe, channels)
        state.batch = batch
        state.unchanged = []
        result_label.ok("")
        unchanged_view.refresh()
        show_rows()
        batch_timer.active = True
        ids = batch.channel_ids
        error: ChildError | None = None
        causes: dict[str, str | None] = {}
        try:
            result = await asyncio.to_thread(
                change_subscriptions_in_child,
                services.runner,
                subscribe,
                [BatchChannel(channel.channel_id, channel.title) for channel in channels],
                owner=batch,
            )
        except ChildError as caught:
            error = caught
            # A batch that stopped gives the page no outcomes, but each change that YouTube confirmed is stored.
            changed = changed_channels(await asyncio.to_thread(read_states, services.sessions, ids), ids, subscribe)
        else:
            changed = {outcome.channel_id for outcome in result.outcomes if outcome.outcome is OutcomeKind.CONFIRMED}
            causes = {outcome.channel_id: outcome.cause for outcome in result.outcomes}
        finally:
            batch_timer.active = False
            state.batch = None
        for channel_id in changed:
            state.selected.pop(channel_id, None)
        state.unchanged = [
            UnchangedChannel(channel.title, causes.get(channel.channel_id))
            for channel in channels
            if channel.channel_id not in changed
        ]
        counts = batch_result_text(subscribe, len(changed), len(ids))
        if error is None:
            if state.unchanged:
                result_label.problem(counts)
                notify_in(client, counts, type="warning")
            else:
                result_label.ok(counts)
                notify_in(client, counts, type="positive")
        elif batch.killed and isinstance(error, ChildFailed):
            text = _("The change of the channels stopped at your request. {result}").format(result=counts)
            result_label.ok(text)
            notify_in(client, text)
        else:
            LOG.warning("The subscription batch stopped: %s", error)
            text = f"{counts} {child_error_text(error)}"
            result_label.problem(text)
            show_pause_end(result_label, text, child_pause(error))
        unchanged_view.refresh()
        await reload()

    async def subscribe_selected() -> None:
        await change_selected(subscribe=True)

    async def unsubscribe_selected() -> None:
        await change_selected(subscribe=False)

    async def count_batch() -> None:
        """Count the channels of the running batch whose stored state is the target now."""
        batch = state.batch
        if batch is None:
            return
        states = await asyncio.to_thread(read_states, services.sessions, batch.channel_ids)
        if state.batch is not batch:
            return
        batch.done = len(changed_channels(states, batch.channel_ids, batch.subscribe))
        show_progress()

    def stop_batch() -> None:
        """Kill the child of the running batch at once. The changes that YouTube confirmed before stay stored."""
        batch = state.batch
        if batch is None or batch.killed:
            return
        batch.killed = services.runner.kill_short_child(batch)
        show_progress()

    async def subscribe_row(event: GenericEventArguments) -> None:
        await change_subscription(str(event.args), subscribe=True)

    async def unsubscribe_row(event: GenericEventArguments) -> None:
        await change_subscription(str(event.args), subscribe=False)

    async def add_row_to_rule(event: GenericEventArguments) -> None:
        channel_id, rule_id = event.args
        channel = shown_channel(str(channel_id))
        rule = next((rule for rule in state.lists.rules if rule.id == rule_id), None)
        if channel is None or rule is None:
            return
        outcome = await asyncio.to_thread(add_to_rule, services.sessions, services.settings.tz, rule.id, channel)
        if outcome is AddOutcome.ADDED:
            text = _("Rule {rule} now reads {title}.").format(rule=rule.name, title=channel.title)
            result_label.ok(text)
            notify_in(client, text, type="positive")
        elif outcome is AddOutcome.ALREADY_IN_RULE:
            text = _("Rule {rule} already reads {title}.").format(rule=rule.name, title=channel.title)
            result_label.ok(text)
            notify_in(client, text)
        else:
            text = _("Rule {rule} does not exist any more.").format(rule=rule.name)
            result_label.problem(text)
            notify_in(client, text, type="warning")
        await reload()

    async def add_selected_to_rule(rule_id: int) -> None:
        rule = next((rule for rule in state.lists.rules if rule.id == rule_id), None)
        # The stored row of a channel is newer than the row that the selection keeps.
        channels = [shown_channel(channel_id) or kept for channel_id, kept in state.selected.items()]
        if rule is None or not channels:
            return
        bulk_menu.close()
        added = await asyncio.to_thread(add_many_to_rule, services.sessions, services.settings.tz, rule.id, channels)
        if added is None:
            text = _("Rule {rule} does not exist any more.").format(rule=rule.name)
            result_label.problem(text)
            notify_in(client, text, type="warning")
        elif added:
            # Translators: {count} is the number of the channels that the rule did not read before.
            text = _("Rule {rule} now reads more channels: {count}.").format(rule=rule.name, count=added)
            result_label.ok(text)
            notify_in(client, text, type="positive")
        else:
            text = _("Rule {rule} already reads each selected channel.").format(rule=rule.name)
            result_label.ok(text)
            notify_in(client, text)
        await reload()

    @ui.refreshable
    def results_view() -> None:
        nonlocal found_table
        found_table = None
        if state.search_error is not None:
            ui.label(state.search_error).classes("text-negative").mark("search-error")
            return
        if state.found is None:
            return
        if not state.found:
            ui.label(_("YouTube found no channel for this text.")).mark("no-found")
            return
        found_table = (
            ui.table(columns=found_columns(), rows=[], row_key="id")
            .props("flat bordered dense")
            .classes("w-full")
            .mark("found-table")
        )
        found_table.add_slot("body-cell-title", TITLE_CELL)
        add_selection(found_table, FOUND_SCOPE, toggle_channel, toggle_found_channels)
        add_actions(found_table, FOUND_SCOPE, subscribe_row, unsubscribe_row, add_row_to_rule)
        show_found()

    @ui.refreshable
    def bulk_menu_view() -> None:
        rule_menu(state.lists, state.selected, add_selected_to_rule)

    @ui.refreshable
    def bulk_unsubscribe_view() -> None:
        """The question before the bulk Unsubscribe, with the note about the rules and the shortest time."""
        channel_ids = fitting_channels(state.lists, state.selected, subscribe=False)
        with ui.card().props("flat").classes("q-pa-md column q-gutter-sm").style("max-width: 24rem"):
            # Translators: {count} is the number of the channels.
            question = _("Unsubscribe the YouTube account from these channels: {count}?")
            ui.label(question.format(count=len(channel_ids))).classes("text-subtitle1").mark(
                "bulk-unsubscribe-question"
            )
            read = sum(bool(state.lists.rules_of(channel_id)) for channel_id in channel_ids)
            if read:
                # Translators: {count} is the number of the channels that a rule reads.
                note = _("Rules read some of these channels: {count}. The rules still read their videos.")
                ui.label(note.format(count=read)).mark("bulk-unsubscribe-note")
            ui.label(batch_time_text(len(channel_ids))).mark("bulk-unsubscribe-time")
            with ui.row().classes("q-gutter-sm"):
                ui.button(_("Unsubscribe"), icon="remove_circle", color="negative", on_click=unsubscribe_selected).mark(
                    "bulk-unsubscribe-confirm"
                )
                ui.button(_("Cancel"), on_click=bulk_unsubscribe_menu.close).props("flat").mark(
                    "bulk-unsubscribe-cancel"
                )

    @ui.refreshable
    def unchanged_view() -> None:
        """The list of the channels of the last batch that did not change."""
        if not state.unchanged:
            return
        with ui.column().classes("gap-1").mark("batch-unchanged"):
            ui.label(_("These channels did not change:")).classes("text-weight-bold").mark("batch-unchanged-title")
            for channel in state.unchanged:
                ui.label(unchanged_text(channel)).mark("batch-unchanged-channel")

    ui.label(_("Subscriptions")).classes("text-h5")
    await account_lists(services, on_refreshed=reload)

    # The bar of the selection, above both tables. While a batch runs, its counter and Stop replace the actions.
    with (
        ui.row()
        .classes("w-full items-center gap-4 q-px-md q-py-sm bg-primary text-white rounded-borders shadow-2")
        .style(SELECTION_BAR_STYLE)
        .mark("selection-bar") as selection_bar
    ):
        with ui.row().classes("items-center gap-4").mark("selection-actions") as selection_actions:
            selected_label = ui.label().classes("text-weight-bold").mark("selected-count")
            hidden_label = ui.label().mark("selected-hidden")
            bulk_subscribe_button = (
                ui.button(icon="add_circle", color="white", on_click=subscribe_selected)
                .props("outline no-caps")
                .mark("bulk-subscribe")
            )
            with (
                ui.button(icon="remove_circle", color="white")
                .props("outline no-caps")
                .mark("bulk-unsubscribe") as bulk_unsubscribe_button,
                ui.menu().mark("bulk-unsubscribe-menu") as bulk_unsubscribe_menu,
            ):
                bulk_unsubscribe_view()
            with (
                ui.button(_("Add to rule"), icon="playlist_add", color="white")
                .props("outline no-caps icon-right=arrow_drop_down")
                .mark("bulk-add"),
                ui.menu().mark("bulk-add-menu") as bulk_menu,
            ):
                bulk_menu_view()
            ui.button(_("Clear selection"), icon="close", color="white", on_click=clear_selection).props(
                "outline no-caps"
            ).mark("clear-selection")
        # The counter of a running batch. A progress bar of the request budget can join this row.
        with ui.row().classes("items-center gap-4").mark("batch-progress") as progress_row:
            ui.spinner(color="white")
            progress_label = ui.label().classes("text-weight-bold").mark("batch-progress-text")
            stop_button = (
                ui.button(_("Stop"), icon="stop", color="white", on_click=stop_batch)
                .props("outline no-caps")
                .mark("batch-stop")
            )
    batch_timer = ui.timer(BATCH_POLL_SECONDS, count_batch, active=False, immediate=False)

    with ui.card().classes("w-full"):
        ui.label(_("Find a channel on YouTube")).classes("text-h6")
        with ui.row().classes("w-full items-center gap-4"):
            search_input = (
                ui.input(_("Name, @handle, channel ID or channel URL"))
                .props("outlined dense clearable")
                .classes("flex-1 min-w-64")
                .mark("search-input")
            )
            search_button = ui.button(_("Search"), icon="search", on_click=search).mark("search")
        search_input.on("keydown.enter", search)
        results_view()

    with ui.card().classes("w-full"):
        ui.label(_("Stored channels")).classes("text-h6")
        ui.label(
            _(
                "The subscriptions of the account from the last refresh, and each channel that a rule reads. "
                "A click on a row selects the channel. A click on the title of a channel opens the channel on YouTube."
            )
        ).classes("text-grey-7")
        with ui.row().classes("w-full items-center gap-4"):
            text_input = (
                ui.input(_("Name or @handle"), on_change=show_channels)
                .props("outlined dense clearable")
                .classes("w-64")
                .mark("filter-text")
            )
            subscribed_select = (
                ui.select(_options(SUBSCRIBED_FILTER), label=_("Subscription"), value=ANY, on_change=show_channels)
                .props("outlined dense")
                .classes("w-48")
                .mark("filter-subscribed")
            )
            rules_select = (
                ui.select(_options(RULES_FILTER), label=_("Rules"), value=ANY, on_change=show_channels)
                .props("outlined dense")
                .classes("w-48")
                .mark("filter-rules")
            )
            only_selected = ui.switch(_("Only selected"), on_change=show_channels).mark("filter-selected")
        summary = ui.label().mark("channels-summary")
        no_channels = ui.label().mark("no-channels")
        # The result of the last action of a row, in both tables.
        result_label = ResultLabel().mark("channel-result")
        channels_table = (
            ui.table(columns=channel_columns(), rows=[], row_key="id", pagination=PAGE_SIZE)
            .props("flat bordered dense")
            .classes("w-full")
            .mark("channels-table")
        )
        channels_table.add_slot("body-cell-title", TITLE_CELL)
        channels_table.add_slot("body-cell-last_video", last_video_cell())
        add_keyboard_sort(channels_table, CHANNELS_SCOPE)
        add_selection(channels_table, CHANNELS_SCOPE, toggle_channel, toggle_shown_channels)
        add_actions(channels_table, CHANNELS_SCOPE, subscribe_row, unsubscribe_row, add_row_to_rule)
        unchanged_view()
    show_channels()
