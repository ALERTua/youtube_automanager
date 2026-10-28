"""The subscriptions page: the stored channels with filters, a search of channels on YouTube, and the channel actions.

The table shows the stored copy of the subscriptions and each channel that a rule reads, so a page load sends no
request to YouTube. A click on a channel, in the table or in the search results, opens the dialog of the channel:
subscribe or unsubscribe, add the channel to a rule, or open the channel on YouTube.

The search, the subscribe and the unsubscribe run in child processes of the runner, and the page waits for them in a
thread, so the web process never imports yt-dlp. While a run, a session check or another child holds the sync lock,
they start nothing, and the page says so. The page reads and writes the database in a thread, never on the event loop.
"""

import asyncio
import enum
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import quote

from nicegui import context, ui
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.channel_actions import (
    AddOutcome,
    FoundChannel,
    add_channel_to_rule,
    find_channels_in_child,
    subscribe_in_child,
    unsubscribe_in_child,
)
from youtube_automanager.db.models import Channel, Rule, RuleChannel
from youtube_automanager.i18n import N_, _
from youtube_automanager.request_pause import RequestPause
from youtube_automanager.short_child import ChildError
from youtube_automanager.web.services import Services
from youtube_automanager.web.ui.account_lists import account_lists, child_error_text, child_pause, show_pause_end
from youtube_automanager.web.ui.common import (
    FOCUS_DELAY_MS,
    ROW_SCOPE_ATTRIBUTE,
    actions_column,
    add_row_button,
    busy,
    column,
    focus_when_drawn,
    notify_in,
    read_timezone,
    row_button_selector,
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

YES_NO = {True: N_("Yes"), False: N_("No")}
SUBSCRIBED_FILTER = {ANY: N_("All"), YES: N_("Subscribed"), NO: N_("Not subscribed")}
RULES_FILTER = {ANY: N_("All"), YES: N_("With rules"), NO: N_("Without rules")}

# The title cell shows the avatar of the channel in front of the title.
TITLE_CELL = """
<q-td :props="props">
  <div class="row items-center no-wrap q-gutter-sm">
    <q-avatar v-if="props.row.thumbnail" size="28px">
      <img :src="props.row.thumbnail" referrerpolicy="no-referrer">
    </q-avatar>
    <span>{{ props.value }}</span>
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


@dataclass(frozen=True)
class SelectedChannel:
    """The channel of the dialog. ``stored`` is its stored row, and ``found`` is its search result."""

    channel_id: str
    stored: ChannelRecord | None
    found: FoundChannel | None

    @property
    def title(self) -> str:
        if self.stored is not None:
            return self.stored.title
        # The dialog opens only for a stored channel or a found channel, so one of the two is here.
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

    @property
    def subscriber_count(self) -> int | None:
        return None if self.found is None else self.found.subscriber_count

    @property
    def is_subscribed(self) -> bool:
        return self.stored is not None and self.stored.is_subscribed


class DialogView(enum.Enum):
    DETAILS = "details"
    ADD_TO_RULE = "add_to_rule"
    CONFIRM_UNSUBSCRIBE = "confirm_unsubscribe"


@dataclass
class PageState:
    lists: StoredLists
    # The channels of the last search, in the order of YouTube. None before the first search.
    found: list[FoundChannel] | None = None
    search_error: str | None = None
    selected_id: str | None = None
    view: DialogView = DialogView.DETAILS
    # The rule that the person chose in the dialog.
    rule_id: int | None = None
    # A subscription change of this page runs.
    working: bool = False
    # The cause of the last failed action of the dialog, and the pause of the requests with cookies that caused it.
    error: str | None = None
    error_pause: RequestPause | None = None
    # The number of the newest read. A read that finishes after a newer one started drops its result.
    lists_read: int = 0
    # The channels of the last search that gave a result, by their id. A failed later search keeps them, so an open
    # dialog of a found channel still works.
    found_by_id: dict[str, FoundChannel] = field(default_factory=dict)
    # The scope of the table whose row opened the dialog, so the focus goes back to that row when the dialog closes.
    row_scope: str | None = None


def read_lists(sessions: sessionmaker[Session]) -> StoredLists:
    """Read the stored channels and the rules with their channels. This call blocks."""
    with sessions() as session:
        channels = [
            ChannelRecord(row.channel_id, row.title, row.handle, row.thumbnail_url, row.is_subscribed)
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
    sessions: sessionmaker[Session], default_timezone: str, rule_id: int, channel: SelectedChannel
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


def filter_channels(lists: StoredLists, channel_filter: ChannelFilter) -> list[ChannelRecord]:
    """Return the stored channels that match the filter, in the order of the list."""
    needle = channel_filter.text.strip().casefold().removeprefix("@")
    with_rules = {channel_id for rule in lists.rules for channel_id in rule.channel_ids}
    matches = []
    for channel in lists.channels:
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


def channel_row(channel: ChannelRecord, rules: list[RuleRecord]) -> dict:
    return {
        "id": channel.channel_id,
        "title": channel.title,
        "thumbnail": channel.thumbnail_url or "",
        "handle": channel.handle or "",
        "subscribed": _(YES_NO[channel.is_subscribed]),
        "rules": ", ".join(rule_label(rule) for rule in rules),
    }


def found_row(found: FoundChannel, stored: ChannelRecord | None) -> dict:
    return {
        "id": found.channel_id,
        "title": found.title,
        "thumbnail": found.thumbnail_url or "",
        "handle": found.handle or "",
        "subscribers": format_count(found.subscriber_count),
        "subscribed": _(YES_NO[stored is not None and stored.is_subscribed]),
    }


def channel_columns() -> list[dict]:
    return [
        column("title", _("Channel")),
        column("handle", _("Handle")),
        column("subscribed", _("Subscribed")),
        column("rules", _("Rules")),
        actions_column(),
    ]


def found_columns() -> list[dict]:
    return [
        column("title", _("Channel")),
        column("handle", _("Handle")),
        column("subscribers", _("Subscribers")),
        column("subscribed", _("Subscribed")),
        actions_column(),
    ]


def _choice(value: str) -> bool | None:
    return None if value == ANY else value == YES


def _options(labels: dict[str, str]) -> dict[str, str]:
    return {value: _(label) for value, label in labels.items()}


@register_page("/subscriptions", label=N_("Subscriptions"), icon="subscriptions")
async def subscriptions_page(services: Services) -> None:
    state = PageState(lists=await asyncio.to_thread(read_lists, services.sessions))
    client = context.client

    def selected() -> SelectedChannel | None:
        if state.selected_id is None:
            return None
        stored = state.lists.channel(state.selected_id)
        found = state.found_by_id.get(state.selected_id)
        if stored is None and found is None:
            return None
        return SelectedChannel(state.selected_id, stored, found)

    def read_filter() -> ChannelFilter:
        return ChannelFilter(
            text=text_input.value or "",
            subscribed=_choice(subscribed_select.value),
            with_rules=_choice(rules_select.value),
        )

    def show_channels() -> None:
        lists = state.lists
        channels = filter_channels(lists, read_filter())
        channels_table.rows = [channel_row(channel, lists.rules_of(channel.channel_id)) for channel in channels]
        summary.set_text(_("{shown} of {total} channels").format(shown=len(channels), total=len(lists.channels)))
        # A filter that matches nothing and a service without stored channels need different next steps.
        no_channels.set_text(
            _("No channel matches the filters.")
            if lists.channels
            else _("No channel is stored yet. Press Refresh lists.")
        )
        no_channels.set_visibility(not channels)

    async def reload() -> None:
        state.lists_read += 1
        read = state.lists_read
        lists = await asyncio.to_thread(read_lists, services.sessions)
        if read != state.lists_read:
            return
        state.lists = lists
        show_channels()
        results_view.refresh()
        dialog_content.refresh()

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
                state.found_by_id = {channel.channel_id: channel for channel in found}
        results_view.refresh()

    def open_channel(event) -> None:
        state.selected_id = str(event.args)
        state.row_scope = event.sender.props.get(ROW_SCOPE_ATTRIBUTE)
        state.view = DialogView.DETAILS
        state.rule_id = None
        state.error = None
        dialog_content.refresh()
        dialog.open()

    def show_view(view: DialogView) -> None:
        state.view = view
        state.rule_id = None
        state.error = None
        dialog_content.refresh()

    def choose_rule(event) -> None:
        state.rule_id = event.value
        dialog_content.refresh()

    async def change_subscription(subscribe: bool) -> None:
        channel = selected()
        if channel is None or state.working:
            return
        state.working = True
        state.error = None
        dialog_content.refresh()
        try:
            if subscribe:
                await asyncio.to_thread(subscribe_in_child, services.runner, channel.channel_id, channel.title)
            else:
                await asyncio.to_thread(unsubscribe_in_child, services.runner, channel.channel_id)
        except ChildError as error:
            LOG.warning("The subscription of channel %s did not change: %s", channel.channel_id, error)
            state.error = _("The subscription did not change. {reason}").format(reason=child_error_text(error))
            state.error_pause = child_pause(error)
        else:
            # The redraw of the dialog deleted the button of this handler, so the message goes through the client.
            if subscribe:
                notify_in(client, _("Subscribed to {title}.").format(title=channel.title), type="positive")
            else:
                notify_in(client, _("Unsubscribed from {title}.").format(title=channel.title), type="positive")
        finally:
            state.working = False
            state.view = DialogView.DETAILS
        await reload()

    async def save_to_rule() -> None:
        channel = selected()
        rule = next((rule for rule in state.lists.rules if rule.id == state.rule_id), None)
        if channel is None or rule is None:
            return
        outcome = await asyncio.to_thread(add_to_rule, services.sessions, services.settings.tz, rule.id, channel)
        # A click on Back while the save runs redraws the dialog and deletes the button of this handler.
        if outcome is AddOutcome.ADDED:
            notify_in(
                client, _("Rule {rule} now reads {title}.").format(rule=rule.name, title=channel.title), type="positive"
            )
        elif outcome is AddOutcome.ALREADY_IN_RULE:
            notify_in(client, _("Rule {rule} already reads {title}.").format(rule=rule.name, title=channel.title))
        else:
            notify_in(client, _("Rule {rule} does not exist any more.").format(rule=rule.name), type="warning")
        state.view = DialogView.DETAILS
        state.rule_id = None
        await reload()

    @ui.refreshable
    def results_view() -> None:
        if state.search_error is not None:
            ui.label(state.search_error).classes("text-negative").mark("search-error")
            return
        if state.found is None:
            return
        if not state.found:
            ui.label(_("YouTube found no channel for this text.")).mark("no-found")
            return
        rows = [found_row(channel, state.lists.channel(channel.channel_id)) for channel in state.found]
        table = (
            ui.table(columns=found_columns(), rows=rows, row_key="id")
            .props("flat bordered dense")
            .classes("w-full cursor-pointer")
            .mark("found-table")
        )
        table.add_slot("body-cell-title", TITLE_CELL)
        table.on("rowClick", open_channel, js_handler="(event, row) => emit(row.id)")
        add_row_button(table, open_channel, icon="edit", label=_("Edit"), scope="found")

    def _details_view(channel: SelectedChannel) -> None:
        rules = state.lists.rules_of(channel.channel_id)
        with ui.grid(columns="auto 1fr").classes("gap-x-4 gap-y-1 items-start"):
            ui.label(_("Subscribed")).classes("text-grey-7")
            ui.label(_(YES_NO[channel.is_subscribed])).mark("channel-subscribed")
            count = channel.subscriber_count
            if count is not None:
                ui.label(_("Subscribers")).classes("text-grey-7")
                ui.label(format_count(count)).mark("channel-subscribers")
            ui.label(_("Rules")).classes("text-grey-7")
            if rules:
                ui.label(", ".join(rule_label(rule) for rule in rules)).mark("channel-rules")
            else:
                ui.label(_("No rule reads this channel.")).mark("channel-rules")
        ui.link(_("Open on YouTube"), channel_url(channel.channel_id), new_tab=True).mark("channel-link")
        with ui.row().classes("gap-2"):
            if channel.is_subscribed:
                ui.button(
                    _("Unsubscribe"), icon="remove_circle", on_click=lambda: show_view(DialogView.CONFIRM_UNSUBSCRIBE)
                ).props("outline").mark("channel-unsubscribe").set_enabled(not state.working)
            else:
                ui.button(_("Subscribe"), icon="add_circle", on_click=lambda: change_subscription(True)).mark(
                    "channel-subscribe"
                ).set_enabled(not state.working)
            ui.button(_("Add to rule…"), icon="playlist_add", on_click=lambda: show_view(DialogView.ADD_TO_RULE)).mark(
                "channel-add-to-rule"
            )
            ui.button(_("Close"), on_click=dialog.close).props("flat").mark("channel-close")

    def _add_to_rule_view(channel: SelectedChannel) -> None:
        ui.label(_("Add the channel to a rule")).classes("text-subtitle1")
        rules = state.lists.rules
        choices = [rule for rule in rules if channel.channel_id not in rule.channel_ids]
        chosen = next((rule for rule in choices if rule.id == state.rule_id), None)
        if not rules:
            ui.label(_("No rule exists yet. Create a rule first.")).mark("no-rule-choice")
        elif not choices:
            ui.label(_("Each rule already reads this channel.")).mark("no-rule-choice")
        else:
            ui.select(
                {rule.id: rule_label(rule) for rule in choices},
                label=_("Rule"),
                value=None if chosen is None else chosen.id,
                on_change=choose_rule,
            ).props("outlined dense").classes("w-full").mark("rule-select")
            ui.label(
                _(
                    "The next run adds the videos of this channel from the last {days} days that the rule matches. "
                    "After that, the rule adds each new video of the channel. To add older videos, open the rule and "
                    "change Also add videos from."
                ).format(days=DEFAULT_DAYS_BACK)
            ).mark("fact-catch-up")
            if chosen is not None and not chosen.channel_ids:
                ui.label(
                    _(
                        "This rule has no channels now, so it reads the subscriptions feed. After this change, it reads "
                        "only its channels, and not the subscriptions feed."
                    )
                ).classes("text-warning").mark("fact-feed")
        with ui.row().classes("gap-2"):
            ui.button(_("Save"), icon="save", on_click=save_to_rule).mark("rule-save").set_enabled(chosen is not None)
            ui.button(_("Back"), on_click=lambda: show_view(DialogView.DETAILS)).props("flat").mark("rule-back")

    def _confirm_unsubscribe_view(channel: SelectedChannel) -> None:
        ui.label(_("Unsubscribe the YouTube account from this channel?")).classes("text-subtitle1").mark("confirm-text")
        if state.lists.rules_of(channel.channel_id):
            ui.label(_("The rules that name this channel still read its videos.")).mark("confirm-rules")
        with ui.row().classes("gap-2"):
            ui.button(_("Unsubscribe"), icon="remove_circle", on_click=lambda: change_subscription(False)).props(
                "color=negative"
            ).mark("confirm-unsubscribe").set_enabled(not state.working)
            ui.button(_("Back"), on_click=lambda: show_view(DialogView.DETAILS)).props("flat").mark("confirm-back")

    @ui.refreshable
    def dialog_content() -> None:
        channel = selected()
        if channel is None:
            return
        with ui.row().classes("items-center gap-4 no-wrap"):
            if channel.thumbnail_url:
                ui.image(channel.thumbnail_url).props("referrerpolicy=no-referrer").classes(
                    "w-16 h-16 rounded-full"
                ).mark("channel-thumbnail")
            with ui.column().classes("gap-0"):
                ui.label(channel.title).classes("text-h6").mark("channel-title")
                ui.label(channel.handle or _("No handle")).classes("text-grey-7").mark("channel-handle")
        if state.working:
            with ui.row().classes("items-center gap-2"):
                ui.spinner(size="sm")
                ui.label(_("The service works with YouTube. This takes up to a minute.")).mark("channel-working")
        if state.error is not None:
            label = ui.label(state.error).classes("text-negative").mark("channel-error")
            show_pause_end(label, state.error, state.error_pause)
        if state.view is DialogView.ADD_TO_RULE:
            _add_to_rule_view(channel)
        elif state.view is DialogView.CONFIRM_UNSUBSCRIBE:
            _confirm_unsubscribe_view(channel)
        else:
            _details_view(channel)

    ui.label(_("Subscriptions")).classes("text-h5")
    await account_lists(services, on_refreshed=reload)

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
                "Click a channel to see its actions."
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
        summary = ui.label().mark("channels-summary")
        no_channels = ui.label().mark("no-channels")
        channels_table = (
            ui.table(columns=channel_columns(), rows=[], row_key="id", pagination=PAGE_SIZE)
            .props("flat bordered dense")
            .classes("w-full cursor-pointer")
            .mark("channels-table")
        )
        channels_table.add_slot("body-cell-title", TITLE_CELL)
        channels_table.on("rowClick", open_channel, js_handler="(event, row) => emit(row.id)")
        add_row_button(channels_table, open_channel, icon="edit", label=_("Edit"), scope="channels")
    show_channels()

    def dialog_changed(event) -> None:
        # The row button that had the focus was drawn again while the dialog was open.
        if not event.value and state.selected_id is not None:
            # The row can be gone, for example after Unsubscribe with the filter Subscribed. Then the field of the
            # table gets the focus: the search field for a found channel, and the filter field for a stored one.
            field = search_input if state.row_scope == "found" else text_input
            focus_when_drawn(
                client,
                row_button_selector(state.selected_id, state.row_scope),
                fallback=f"#{field.html_id}",
                delay_ms=FOCUS_DELAY_MS,
            )

    with ui.dialog().mark("channel-dialog") as dialog, ui.card().classes("w-full max-w-lg"):
        dialog_content()
    dialog.on_value_change(dialog_changed)
