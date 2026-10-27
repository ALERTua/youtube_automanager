"""The rules page: the table of the rules with its filters. The editor of a rule is a sub page, in ``rule_editor``.

A rule adds the matching videos of its channels to its target playlist, and a rule without channels reads the
subscriptions feed of the account. The page takes the channels and the playlists only from the stored copy of the
account, which the refresh of the account data writes, so a page load makes no request to YouTube. The page reads and
writes the database in a thread, never on the event loop. Each time shows in the time zone of the schedule.

The address keeps the filters in its query: ``q`` is the text of the search, ``playlist`` the target playlist and
``state`` the state of the rule. A change of a filter writes the address in place and does not build the page again,
so a reload and a shared link show the same rules.

The switch Enabled of a row turns its rule on and off at once. The row keeps its place until the next read of the
table, also when the filter of the state would hide it now. A click on a column header, or Enter and Space on it, sorts
the rows in the browser, and the page keeps that order when it builds the table again, for example after a change of a
filter.

The column Added counts the videos that each rule added in the last 7 and in the last 30 days, from the history. The
page reads the counts with the rules, and again after the end of each run.
"""

import asyncio
from collections.abc import Container
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from nicegui import context, ui
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import utc_now
from youtube_automanager.i18n import N_, _
from youtube_automanager.rules_store import (
    AddedCounts,
    ChannelChoice,
    RuleRecord,
    Stored,
    read_added_counts,
    read_stored,
    set_enabled,
)
from youtube_automanager.web.services import Services
from youtube_automanager.web.ui.account_lists import account_lists
from youtube_automanager.web.ui.common import (
    FOCUS_KEY_ATTRIBUTE,
    actions_column,
    add_keyboard_sort,
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
from youtube_automanager.web.ui.live_runs import RunFollower
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

# The column Added shows "<7 days> / <30 days>", and it sorts by the count of the 30 days.
ADDED_SORT = "(a, b, rowA, rowB) => rowA.added_month - rowB.added_month"

# The event of the switch Enabled of a row. It sends the id of the rule and the new value.
TOGGLE_EVENT = "toggleEnabled"
TOGGLE_JS_HANDLER = "(id, value) => emit(id, value)"
# The switch Enabled of a row. Its click does not reach the row, whose click opens the editor. Each update of the table
# draws the switch again, and its key of ``focus_key`` brings the keyboard focus back to the switch of the same rule.
# The name of the switch comes from the row as a value, so no text of a rule runs in the template.
ENABLED_CELL = f"""
<q-td :props="props" @click.stop>
  <q-toggle dense :model-value="props.row.enabled" :aria-label="props.row.enabled_label" \
:{FOCUS_KEY_ATTRIBUTE}="'rule-enabled-' + props.row.id" \
@update:model-value="(value) => $parent.$emit('{TOGGLE_EVENT}', props.row.id, value)" />
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


def rule_row(rule: RuleRecord, stored: Stored, zone: ZoneInfo, added: AddedCounts) -> dict:
    titles = stored.channel_titles
    channels = [titles.get(channel_id, channel_id) for channel_id in rule.channel_ids]
    return {
        "id": rule.id,
        "name": rule.name,
        "enabled": rule.enabled,
        # Translators: the name of the switch that turns the rule {name} on and off, for a screen reader.
        "enabled_label": _("Enabled: {name}").format(name=rule.name),
        "channels": ", ".join(channels) if channels else _("Subscriptions feed"),
        "playlist": stored.playlist_title(rule.playlist_id),
        "no_shorts": yes_or_no(rule.no_shorts),
        "condition": describe(rule.condition, titles),
        "added": f"{added.week} / {added.month}",
        "added_month": added.month,
        "updated": format_time(rule.updated_at, zone),
    }


def rule_columns() -> list[dict]:
    return [
        column("name", _("Name"), sortable=True),
        column("enabled", _("Enabled"), sortable=True),
        column("channels", _("Channels"), sortable=True),
        column("playlist", _("Playlist"), sortable=True),
        column("no_shorts", _("No Shorts"), sortable=True),
        column("condition", _("Condition"), sortable=True),
        # Translators: the column of the videos that the rule added in the last 7 days and in the last 30 days.
        {**column("added", _("Added 7 d / 30 d"), sortable=True), ":sort": ADDED_SORT},
        column("updated", _("Changed"), sortable=True),
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


def read_rules(sessions: sessionmaker[Session], default_timezone: str) -> tuple[Stored, dict[int, AddedCounts]]:
    """Read the rules with the stored copy of the account, and the videos that each rule added. This call blocks."""
    return read_stored(sessions, default_timezone), read_added_counts(sessions, utc_now())


@dataclass
class PageState:
    stored: Stored
    # The videos that each rule added, by the id of the rule. A rule without an add in 30 days has no entry.
    added: dict[int, AddedCounts]
    filters: FilterValues = field(default_factory=FilterValues)
    # The number of the newest read. A read that finishes after a newer one started drops its result.
    reads: int = 0
    # True while the page puts the filters of a new address into the fields, so their change writes no address.
    following: bool = False
    # The table that shows now, or None when the page shows a text instead.
    table: ui.table | None = None
    # The sort of the table that the user chose, so a new table of the same page keeps it.
    pagination: dict | None = None


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

    async def run_finished(*_args) -> None:
        # The counts of the column Added change only with a run.
        await reload(in_place=True)

    runs = RunFollower(services.run_events, finished=run_finished)
    state = PageState(*await asyncio.to_thread(read_rules, services.sessions, services.settings.tz))
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

    async def reload(*, in_place: bool = False) -> None:
        """Read the rules again and show them.

        With ``in_place``, the rows of the table change in place: each row stays, also a row that the filters would
        hide now, and only a rule that is gone leaves the table. The next change of a filter shows the rules of the
        filters again.
        """
        state.reads += 1
        read = state.reads
        stored, added = await asyncio.to_thread(read_rules, services.sessions, services.settings.tz)
        if read != state.reads:
            return
        state.stored = stored
        state.added = added
        options = _playlist_filter_options(stored)
        # A playlist that no rule targets any more leaves the filter, and its change writes the address.
        playlist_select.set_options(options, value=playlist_select.value if playlist_select.value in options else ANY)
        table = state.table
        if in_place and table is not None and not table.is_deleted:
            zone = ZoneInfo(stored.timezone)
            rows = [
                rule_row(rule, stored, zone, added.get(rule.id, AddedCounts()))
                for row in table.rows
                if (rule := stored.rule(row["id"])) is not None
            ]
            if rows:
                # A table update draws each row again, so the page sends the rows only when they change.
                if rows != table.rows:
                    table.update_rows(rows)
                return
        rules_view.refresh()

    def open_editor(path: str) -> None:
        # The editor keeps the filters in its address, and goes back to the rules with them.
        ui.navigate.to(with_query(path, state.filters.query()))

    def edit_rule(event) -> None:
        open_editor(f"{RULES_PATH}/{int(event.args)}")

    async def toggle_rule(event) -> None:
        """Turn a rule on or off from the switch of its row, and show the stored state in the row."""
        rule_id, enabled = event.args
        found = await asyncio.to_thread(set_enabled, services.sessions, int(rule_id), bool(enabled))
        if not found:
            ui.notify(_("The rule does not exist any more, so the page did not save it."), type="warning")
        await reload(in_place=True)

    def keep_sort(event) -> None:
        state.pagination = event.value

    @ui.refreshable
    def rules_view() -> None:
        stored = state.stored
        state.table = None
        if not stored.rules:
            ui.label(_("No rule yet. The button New rule creates one.")).mark("no-rules")
            return
        shown = shown_rules()
        if not shown:
            ui.label(_("No rule matches the filters.")).mark("no-match")
            return
        zone = ZoneInfo(stored.timezone)
        table = (
            ui.table(
                columns=rule_columns(),
                rows=[rule_row(rule, stored, zone, state.added.get(rule.id, AddedCounts())) for rule in shown],
                row_key="id",
            )
            .props("flat bordered dense")
            .classes("w-full cursor-pointer")
            .mark("rules-table")
        )
        state.table = table
        if state.pagination is not None:
            table.pagination = state.pagination
        table.on_pagination_change(keep_sort)
        add_keyboard_sort(table, RULES_SCOPE)
        table.add_slot("body-cell-enabled", ENABLED_CELL)
        table.on(TOGGLE_EVENT, toggle_rule, js_handler=TOGGLE_JS_HANDLER)
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
    if runs.start():
        # A run ended during the first read.
        await reload(in_place=True)
