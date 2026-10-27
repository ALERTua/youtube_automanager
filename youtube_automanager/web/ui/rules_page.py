"""The rules page: the table of the rules with its filters. The editor of a rule is a sub page, in ``rule_editor``.

A rule adds the matching videos of its channels to its target playlist, and a rule without channels reads the
subscriptions feed of the account. The page takes the channels and the playlists only from the stored copy of the
account, which the refresh of the account data writes, so a page load makes no request to YouTube. The page reads and
writes the database in a thread, never on the event loop. Each time shows in the time zone of the schedule.

The address keeps the filters in its query: ``q`` is the text of the search, ``playlist`` the target playlist and
``state`` the state of the rule. A change of a filter writes the address in place and does not build the page again,
so a reload and a shared link show the same rules.
"""

import asyncio
from collections.abc import Container
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from nicegui import context, ui

from youtube_automanager.i18n import N_, _
from youtube_automanager.rules_store import ChannelChoice, RuleRecord, Stored, read_stored
from youtube_automanager.web.services import Services
from youtube_automanager.web.ui.account_lists import account_lists
from youtube_automanager.web.ui.common import (
    actions_column,
    add_row_button,
    column,
    focus_when_drawn,
    format_time,
    row_button_selector,
)
from youtube_automanager.web.ui.layout import (
    AddressFollower,
    address_query,
    register_page,
    replace_address,
    with_query,
)
from youtube_automanager.web.ui.rule_editor import (
    NEW_RULE_PATH,
    RETURN_FOCUS,
    RULES_PATH,
    ApplyBanner,
    edit_rule_page,
    new_rule_page,
)
from youtube_automanager.web.ui.rule_form import describe

# The scope of the row buttons of the table, so a focus move after the editor finds no button of another page.
RULES_SCOPE = "rules"

# The value of a filter that lets each rule through.
ANY = "any"
ENABLED = "enabled"
DISABLED = "disabled"
STATES = {ENABLED: N_("Enabled"), DISABLED: N_("Disabled")}

# One line with the start of the text, and the whole text as the tooltip of the cell.
LONG_CELL = """
<q-td :props="props">
  <div class="ellipsis" style="max-width: 20rem" :title="props.value">{{ props.value }}</div>
</q-td>
"""


@dataclass(frozen=True)
class RuleFilter:
    # A part of the rule name, or of the title or the handle of a rule channel. The case does not count.
    text: str = ""
    playlist_id: str | None = None
    enabled: bool | None = None

    def matches(self, rule: RuleRecord, channels: dict[str, ChannelChoice]) -> bool:
        if self.enabled is not None and rule.enabled != self.enabled:
            return False
        if self.playlist_id is not None and rule.playlist_id != self.playlist_id:
            return False
        needle = self.text.strip().casefold()
        if not needle:
            return True
        texts = [rule.name]
        for channel_id in rule.channel_ids:
            channel = channels.get(channel_id)
            if channel is not None:
                texts.extend(text for text in (channel.title, channel.handle) if text)
        return any(needle in text.casefold() for text in texts)


def yes_or_no(value: bool) -> str:
    return _("Yes") if value else _("No")


def rule_row(rule: RuleRecord, stored: Stored, zone: ZoneInfo) -> dict:
    titles = stored.channel_titles
    channels = [titles.get(channel_id, channel_id) for channel_id in rule.channel_ids]
    return {
        "id": rule.id,
        "name": rule.name,
        "enabled": yes_or_no(rule.enabled),
        "channels": ", ".join(channels) if channels else _("Subscriptions feed"),
        "playlist": stored.playlist_title(rule.playlist_id),
        "no_shorts": yes_or_no(rule.no_shorts),
        "condition": describe(rule.condition, titles),
        "updated": format_time(rule.updated_at, zone),
    }


def rule_columns() -> list[dict]:
    return [
        column("name", _("Name")),
        column("enabled", _("Enabled")),
        column("channels", _("Channels")),
        column("playlist", _("Playlist")),
        column("no_shorts", _("No Shorts")),
        column("condition", _("Condition")),
        column("updated", _("Changed")),
        actions_column(),
    ]


@dataclass(frozen=True)
class FilterValues:
    """The values of the three filter fields, as the query of the address holds them."""

    text: str = ""
    playlist: str = ANY
    state: str = ANY

    def query(self) -> dict[str, str | None]:
        """Return the query parameters of the address. A filter that lets each rule through has no parameter."""
        return {
            "q": self.text or None,
            "playlist": None if self.playlist == ANY else self.playlist,
            "state": None if self.state == ANY else self.state,
        }

    def rule_filter(self) -> RuleFilter:
        return RuleFilter(
            text=self.text,
            playlist_id=None if self.playlist == ANY else self.playlist,
            enabled=None if self.state == ANY else self.state == ENABLED,
        )


def filter_values(query: dict[str, str], playlists: Container[str]) -> FilterValues:
    """Return the filters of the query of an address. A value that no option of its field has lets each rule through,
    for example the playlist of a link after the delete of its last rule."""
    playlist = query.get("playlist", ANY)
    state = query.get("state", ANY)
    return FilterValues(
        text=query.get("q", ""),
        playlist=playlist if playlist in playlists else ANY,
        state=state if state in STATES else ANY,
    )


@dataclass
class PageState:
    stored: Stored
    filters: FilterValues = field(default_factory=FilterValues)
    # The number of the newest read. A read that finishes after a newer one started drops its result.
    reads: int = 0
    # True while the page puts the filters of a new address into the fields, so their change writes no address.
    following: bool = False


def _playlist_filter_options(stored: Stored) -> dict[str, str]:
    targets = {rule.playlist_id for rule in stored.rules}
    playlists = {playlist_id: stored.playlist_title(playlist_id) for playlist_id in targets}
    return {ANY: _("All playlists"), **dict(sorted(playlists.items(), key=lambda item: item[1].casefold()))}


@register_page(
    RULES_PATH,
    label=N_("Rules"),
    icon="rule",
    # "/rules/new" comes first: "/rules/{rule_id}" matches it too.
    routes=[(NEW_RULE_PATH, new_rule_page), (f"{RULES_PATH}/{{rule_id}}", edit_rule_page)],
)
async def rules_page(services: Services) -> None:
    client = context.client
    state = PageState(await asyncio.to_thread(read_stored, services.sessions, services.settings.tz))
    state.filters = filter_values(address_query(client), _playlist_filter_options(state.stored))

    def shown_rules() -> list[RuleRecord]:
        rule_filter = state.filters.rule_filter()
        return [rule for rule in state.stored.rules if rule_filter.matches(rule, state.stored.channels)]

    def filters_changed() -> None:
        """Show the rules of the new filters, and write the filters into the address in place."""
        if state.following:
            return
        values = FilterValues(text_input.value or "", playlist_select.value or ANY, state_select.value or ANY)
        if values == state.filters:
            return
        state.filters = values
        replace_address(client, with_query(RULES_PATH, values.query()))
        rules_view.refresh()

    def follow_address(query: dict[str, str]) -> None:
        """Show the filters of a new address of this page, for example after the Back button or a click in the
        drawer."""
        values = filter_values(query, playlist_select.options)
        state.filters = values
        state.following = True
        try:
            text_input.set_value(values.text)
            playlist_select.set_value(values.playlist)
            state_select.set_value(values.state)
        finally:
            state.following = False
        rules_view.refresh()

    async def reload() -> None:
        state.reads += 1
        read = state.reads
        stored = await asyncio.to_thread(read_stored, services.sessions, services.settings.tz)
        if read != state.reads:
            return
        state.stored = stored
        options = _playlist_filter_options(stored)
        # A playlist that no rule targets any more leaves the filter, and its change writes the address.
        playlist_select.set_options(options, value=playlist_select.value if playlist_select.value in options else ANY)
        rules_view.refresh()

    def open_editor(path: str) -> None:
        # The editor keeps the filters in its address, and goes back to the rules with them.
        ui.navigate.to(with_query(path, state.filters.query()))

    def edit_rule(event) -> None:
        open_editor(f"{RULES_PATH}/{int(event.args)}")

    @ui.refreshable
    def rules_view() -> None:
        stored = state.stored
        if not stored.rules:
            ui.label(_("No rule yet. The button New rule creates one.")).mark("no-rules")
            return
        shown = shown_rules()
        if not shown:
            ui.label(_("No rule matches the filters.")).mark("no-match")
            return
        zone = ZoneInfo(stored.timezone)
        table = (
            ui.table(columns=rule_columns(), rows=[rule_row(rule, stored, zone) for rule in shown], row_key="id")
            .props("flat bordered dense")
            .classes("w-full cursor-pointer")
            .mark("rules-table")
        )
        table.add_slot("body-cell-channels", LONG_CELL)
        table.add_slot("body-cell-condition", LONG_CELL)
        # The rule id is the only part of the click that the page needs.
        table.on("rowClick", edit_rule, js_handler="(event, row) => emit(row.id)")
        add_row_button(table, edit_rule, icon="edit", label=_("Edit"), scope=RULES_SCOPE)

    def focus_after_editor() -> None:
        """Move the focus to where the editor was opened, after a save, a delete or a cancel in the editor.

        The focus goes to the Edit button of the rule of the editor. It goes to New rule when the rule was new, when it
        is gone, for example after a delete, or when the filters hide it. The selector of the button holds the scope of
        this table, and New rule has the id of its element, so a focus move that ends after a navigation to another
        page finds nothing there.
        """
        if RETURN_FOCUS not in client.storage:
            return
        rule_id = client.storage.pop(RETURN_FOCUS)
        new_rule = f"#{new_rule_button.html_id}"
        if rule_id is not None and any(rule.id == rule_id for rule in shown_rules()):
            focus_when_drawn(client, row_button_selector(rule_id, RULES_SCOPE), fallback=new_rule)
        else:
            focus_when_drawn(client, new_rule)

    ui.label(_("Rules")).classes("text-h5")

    def rule_name(rule_id: int) -> str:
        rule = state.stored.rule(rule_id)
        return rule.name if rule is not None else str(rule_id)

    await ApplyBanner(services, rule_name).load()
    # The editor reads the stored lists itself, so a refresh needs only a new read of the table.
    await account_lists(services, on_refreshed=reload)
    with ui.row().classes("w-full items-center gap-4"):
        text_input = (
            ui.input(_("Search"), value=state.filters.text, on_change=filters_changed)
            .props("outlined dense clearable")
            .classes("w-64")
            .mark("filter-text")
        )
        playlist_select = (
            ui.select(
                _playlist_filter_options(state.stored),
                label=_("Playlist"),
                value=state.filters.playlist,
                on_change=filters_changed,
            )
            .props("outlined dense")
            .classes("w-56")
            .mark("filter-playlist")
        )
        state_select = (
            ui.select(
                {ANY: _("All"), **{value: _(label) for value, label in STATES.items()}},
                label=_("State"),
                value=state.filters.state,
                on_change=filters_changed,
            )
            .props("outlined dense")
            .classes("w-40")
            .mark("filter-state")
        )
        ui.space()
        new_rule_button = ui.button(_("New rule"), icon="add", on_click=lambda: open_editor(NEW_RULE_PATH)).mark(
            "new-rule"
        )
    rules_view()
    AddressFollower(RULES_PATH, follow_address)
    focus_after_editor()
