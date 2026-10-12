"""The rules page: the table of the rules with its filters, and the editor that creates, changes and deletes a rule.

A rule adds the matching videos of its channels to its target playlist, and a rule without channels reads the
subscriptions feed of the account. The page takes the channels and the playlists only from the stored copy of the
account, which the refresh of the account data writes, so it makes no request to YouTube. The page reads and writes
the database in a thread, never on the event loop. Each time shows in the time zone of the schedule.
"""

import asyncio
import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from nicegui import ui
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from youtube_automanager.db.models import Channel, Playlist, Rule, RuleChannel, Schedule, utc_now
from youtube_automanager.i18n import N_, _
from youtube_automanager.runner import RunAlreadyActive, SyncRunner
from youtube_automanager.web.services import Services
from youtube_automanager.web.ui.dashboard import format_time
from youtube_automanager.web.ui.layout import register_page
from youtube_automanager.web.ui.rule_form import (
    FIELDS,
    GROUP_KINDS,
    LIVE_STATUSES,
    OPERATORS,
    SHORT_CHOICES,
    Group,
    GroupKind,
    Leaf,
    ValueKind,
    change_field,
    change_operator,
    condition_from_json,
    condition_of,
    describe,
    from_tree,
    json_text,
    new_leaf,
    value_kind,
)

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
            )
            for rule in session.scalars(select(Rule).order_by(Rule.position, Rule.id))
        ]
        playlists = sorted(session.scalars(select(Playlist)), key=lambda row: (row.title.casefold(), row.playlist_id))
        timezone = session.scalar(select(Schedule.timezone).where(Schedule.id == 1)) or default_timezone
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


def _column(name: str, label: str) -> dict:
    return {"name": name, "label": label, "field": name, "align": "left"}


def rule_columns() -> list[dict]:
    return [
        _column("name", _("Name")),
        _column("enabled", _("Enabled")),
        _column("channels", _("Channels")),
        _column("playlist", _("Playlist")),
        _column("no_shorts", _("No Shorts")),
        _column("condition", _("Condition")),
        _column("updated", _("Changed")),
    ]


def channel_label(channel: ChannelChoice) -> str:
    label = f"{channel.title} ({channel.handle})" if channel.handle else channel.title
    if not channel.is_subscribed:
        # Translators: a channel that left the subscriptions. A rule can still read it by its id.
        label = _("{channel}, not subscribed").format(channel=label)
    return label


def channel_options(stored: Stored, extra: list[str]) -> dict[str, str]:
    """The stored channels for a select, with each id of ``extra`` that the copy does not have."""
    options = {channel_id: channel_label(channel) for channel_id, channel in stored.channels.items()}
    for channel_id in extra:
        options.setdefault(channel_id, channel_id)
    return options


def _marker(name: str, path: tuple[int, ...]) -> str:
    """The marker of a part of the condition editor. The path holds the index of each item from the root."""
    return "-".join([name, *map(str, path)])


class RuleEditor:
    """The form that creates or changes one rule. ``done`` runs after a save or a delete."""

    def __init__(
        self,
        services: Services,
        stored: Stored,
        rule: RuleRecord | None,
        close: Callable[[], None],
        done: Callable[[], Awaitable[None]],
    ) -> None:
        self.services = services
        self.stored = stored
        self.rule = rule
        self.close = close
        self.done = done
        # None when the visual editor cannot show the condition. The form then shows the condition as JSON text.
        self.root: Group | None = from_tree(rule.condition) if rule else Group()
        self.saving = False

    def build(self) -> None:
        rule = self.rule
        ui.label(_("Change the rule") if rule else _("New rule")).classes("text-h6").mark("editor-title")
        self.name_input = (
            ui.input(_("Name"), value=rule.name if rule else "").props("outlined dense").classes("w-full")
        ).mark("rule-name")
        self._sources()
        ui.label(_("Condition")).classes("text-subtitle1")
        if rule is not None and self.root is None:
            ui.label(_("The visual editor cannot show this condition. Edit it as JsonLogic.")).classes("text-grey-7")
            self.json_input = (
                ui.textarea(value=json_text(rule.condition))
                .props("outlined autogrow")
                .classes("w-full font-mono")
                .mark("rule-json")
            )
        elif self.root is not None:
            self._condition_editor(self.root)
        self.no_shorts_switch = ui.switch(_("No Shorts"), value=rule.no_shorts if rule else False).mark(
            "rule-no-shorts"
        )
        self._actions()

    def _sources(self) -> None:
        """Add the channels that the rule reads and its target playlist."""
        rule = self.rule
        channel_ids = list(rule.channel_ids) if rule else []
        self.channel_select = (
            ui.select(
                channel_options(self.stored, channel_ids),
                label=_("Channels"),
                value=channel_ids,
                multiple=True,
                with_input=True,
            )
            .props("outlined dense use-chips")
            .classes("w-full")
            .mark("rule-channels")
        )
        ui.label(
            _("Without channels, the rule reads the subscriptions feed. The feed needs the YouTube account.")
        ).classes("text-grey-7").mark("rule-feed-hint").bind_visibility_from(
            self.channel_select, "value", lambda value: not value
        )

        own = {
            playlist_id: playlist.title for playlist_id, playlist in self.stored.playlists.items() if playlist.is_own
        }
        if rule and rule.playlist_id not in own:
            own[rule.playlist_id] = self.stored.playlist_title(rule.playlist_id)
        # The button that refreshes the stored lists of the account goes next to this select.
        self.playlist_select = (
            ui.select(own, label=_("Target playlist"), value=rule.playlist_id if rule else None, with_input=True)
            .props("outlined dense")
            .classes("w-full")
            .mark("rule-playlist")
        )
        if not own:
            ui.label(
                _(
                    "No playlist is stored yet. The lists of the channels and the playlists come from the refresh of "
                    "the account data."
                )
            ).classes("text-warning").mark("rule-no-playlists")

    def _actions(self) -> None:
        """Add the switch that turns the rule on and off, the hint about new videos, the error and the buttons."""
        rule = self.rule
        self.enabled_switch = ui.switch(_("Enabled"), value=rule.enabled if rule else True).mark("rule-enabled")
        ui.label(
            _(
                "The rule acts only on new videos. It does not go back to the videos that are already on its "
                "channels, also after a change or when you turn it on again."
            )
        ).classes("text-grey-7").mark("rule-new-videos-hint")
        self.error_label = ui.label().classes("text-negative whitespace-pre-wrap").mark("editor-error")
        self.error_label.set_visibility(False)
        with ui.row().classes("w-full items-center gap-2"):
            self.save_button = ui.button(_("Save"), icon="save", on_click=self.save).mark("editor-save")
            ui.button(_("Cancel"), on_click=self.close).props("flat").mark("editor-cancel")
            if rule:
                ui.space()
                ui.button(_("Delete"), icon="delete", color="negative", on_click=self.ask_delete).props("flat").mark(
                    "editor-delete"
                )

    # The condition editor.

    def _condition_editor(self, root: Group) -> None:
        @ui.refreshable
        def condition_view() -> None:
            self._group_view(root, None, ())

        self.condition_view = condition_view
        condition_view()

    def _changed(self) -> None:
        self.condition_view.refresh()

    def _add(self, group: Group, item: Group | Leaf) -> None:
        group.items.append(item)
        self._changed()

    def _remove(self, parent: Group, index: int) -> None:
        del parent.items[index]
        self._changed()

    def _group_view(self, group: Group, parent: Group | None, path: tuple[int, ...]) -> None:
        with ui.card().props("flat bordered").classes("w-full gap-2 p-2").mark(_marker("group", path)):
            with ui.row().classes("w-full items-center gap-2"):
                ui.select(
                    {kind: _(label) for kind, label in GROUP_KINDS.items()},
                    value=group.kind,
                    on_change=lambda event: setattr(group, "kind", GroupKind(event.value)),
                ).props("outlined dense").classes("w-64").mark(_marker("group-kind", path))
                ui.space()
                if parent is not None:
                    ui.button(icon="delete", on_click=lambda: self._remove(parent, path[-1])).props(
                        "flat round dense"
                    ).tooltip(_("Remove the group")).mark(_marker("remove", path))
            if not group.items and parent is None:
                ui.label(_("Without conditions, the rule takes each video of its channels.")).classes(
                    "text-grey-7"
                ).mark("no-conditions")
            for index, item in enumerate(group.items):
                if isinstance(item, Group):
                    self._group_view(item, group, (*path, index))
                else:
                    self._leaf_view(item, group, (*path, index))
            with ui.row().classes("gap-2"):
                ui.button(_("Add a condition"), icon="add", on_click=lambda: self._add(group, new_leaf())).props(
                    "flat dense"
                ).mark(_marker("add-condition", path))
                ui.button(
                    _("Add a group"),
                    icon="playlist_add",
                    on_click=lambda: self._add(group, Group(GroupKind.ANY, [new_leaf()])),
                ).props("flat dense").mark(_marker("add-group", path))

    def _leaf_view(self, leaf: Leaf, parent: Group, path: tuple[int, ...]) -> None:
        def set_field(event) -> None:
            change_field(leaf, event.value)
            self._changed()

        def set_operator(event) -> None:
            change_operator(leaf, event.value)
            self._changed()

        with ui.row().classes("w-full items-center gap-2").mark(_marker("leaf", path)):
            ui.select({name: _(label) for name, label in FIELDS.items()}, value=leaf.field, on_change=set_field).props(
                "outlined dense"
            ).classes("w-48").mark(_marker("field", path))
            if len(OPERATORS[leaf.field]) > 1:
                ui.select(
                    {operator: _(label) for operator, label in OPERATORS[leaf.field].items()},
                    value=leaf.operator,
                    on_change=set_operator,
                ).props("outlined dense").classes("w-48").mark(_marker("operator", path))
            self._value_input(leaf, path)
            if leaf.operator == "regex":
                ui.checkbox(
                    _("Case-sensitive"),
                    value=leaf.case_sensitive,
                    on_change=lambda event: setattr(leaf, "case_sensitive", bool(event.value)),
                ).mark(_marker("case", path))
            ui.button(icon="close", on_click=lambda: self._remove(parent, path[-1])).props("flat round dense").classes(
                "ml-auto"
            ).tooltip(_("Remove the condition")).mark(_marker("remove", path))

    def _value_input(self, leaf: Leaf, path: tuple[int, ...]) -> None:
        def set_value(value: Any) -> None:
            leaf.value = value

        marker = _marker("value", path)
        match value_kind(leaf.field, leaf.operator):
            case ValueKind.TEXT:
                ui.input(
                    _("Pattern") if leaf.operator == "regex" else _("Text"),
                    value=leaf.value or "",
                    on_change=lambda event: set_value(event.value or ""),
                ).props("outlined dense").classes("flex-1 min-w-48").mark(marker)
            case ValueKind.CHANNEL:
                ui.select(
                    channel_options(self.stored, [leaf.value] if leaf.value else []),
                    value=leaf.value,
                    with_input=True,
                    on_change=lambda event: set_value(event.value),
                ).props("outlined dense").classes("flex-1 min-w-48").mark(marker)
            case ValueKind.CHANNELS:
                ui.select(
                    channel_options(self.stored, list(leaf.value or [])),
                    value=list(leaf.value or []),
                    multiple=True,
                    with_input=True,
                    on_change=lambda event: set_value(list(event.value or [])),
                ).props("outlined dense use-chips").classes("flex-1 min-w-48").mark(marker)
            case ValueKind.SECONDS:

                def set_seconds(value: float | None) -> None:
                    # ``ui.number`` gives a float. The rule compares whole seconds.
                    set_value(int(value) if value is not None and float(value).is_integer() else value)

                ui.number(
                    _("Seconds"),
                    value=leaf.value,
                    min=0,
                    step=1,
                    precision=0,
                    on_change=lambda event: set_seconds(event.value),
                ).props("outlined dense").classes("w-36").mark(marker)
            case ValueKind.DATE:
                ui.input(_("Date"), value=leaf.value, on_change=lambda event: set_value(event.value or None)).props(
                    "type=date outlined dense"
                ).classes("w-48").mark(marker)
            case ValueKind.LIVE_STATUS:
                ui.select(
                    {status: _(label) for status, label in LIVE_STATUSES.items()},
                    value=leaf.value,
                    on_change=lambda event: set_value(event.value),
                ).props("outlined dense").classes("w-64").mark(marker)
            case ValueKind.SHORT:
                ui.select(
                    {choice: _(label) for choice, label in SHORT_CHOICES.items()},
                    value=leaf.value,
                    on_change=lambda event: set_value(event.value),
                ).props("outlined dense").classes("w-48").mark(marker)

    # Save and delete.

    def read_draft(self) -> RuleDraft:
        """Return the rule of the form. Raise ValueError with a message for the user when the form cannot be saved."""
        name = (self.name_input.value or "").strip()
        if not name:
            raise ValueError(_("Enter a name."))
        playlist_id = self.playlist_select.value
        if not playlist_id:
            raise ValueError(_("Choose a target playlist."))
        if self.root is None:
            condition = condition_from_json(self.json_input.value)
        else:
            condition = condition_of(self.root)
        return RuleDraft(
            name=name,
            enabled=bool(self.enabled_switch.value),
            channel_ids=tuple(self.channel_select.value or ()),
            playlist_id=playlist_id,
            no_shorts=bool(self.no_shorts_switch.value),
            condition=condition,
        )

    def show_error(self, text: str | None) -> None:
        self.error_label.set_text(text or "")
        self.error_label.set_visibility(bool(text))

    async def save(self) -> None:
        if self.saving:
            return
        try:
            draft = self.read_draft()
        except ValueError as error:
            self.show_error(str(error))
            return
        self.show_error(None)
        self.saving = True
        self.save_button.disable()
        try:
            rule_id = await asyncio.to_thread(
                save_rule, self.services.sessions, self.rule.id if self.rule else None, draft
            )
        finally:
            self.saving = False
            self.save_button.enable()
        if rule_id is None:
            ui.notify(_("The rule does not exist any more, so the page did not save it."), type="warning")
        else:
            ui.notify(_("The rule is saved."), type="positive")
        self.close()
        await self.done()

    async def ask_delete(self) -> None:
        rule = self.rule
        if rule is None:
            return
        with ui.dialog() as confirm, ui.card().mark("delete-dialog"):
            ui.label(_('Delete the rule "{name}"?').format(name=rule.name)).mark("delete-question")
            ui.label(_("The history keeps the actions of the rule.")).classes("text-grey-7")
            with ui.row().classes("gap-2"):
                ui.button(_("Delete"), color="negative", on_click=lambda: confirm.submit(True)).mark("delete-confirm")
                ui.button(_("Cancel"), on_click=lambda: confirm.submit(False)).props("flat").mark("delete-cancel")
        confirmed = await confirm
        if not confirm.is_deleted:
            confirm.delete()
        if not confirmed:
            return
        try:
            await asyncio.to_thread(delete_rule, self.services.sessions, self.services.runner, rule.id)
        except RunAlreadyActive:
            ui.notify(
                _("A sync run or another task with YouTube is active now. Delete the rule when it ends."),
                type="warning",
            )
            return
        ui.notify(_("The rule is deleted."), type="positive")
        self.close()
        await self.done()


@dataclass
class PageState:
    stored: Stored
    # The number of the newest read. A read that finishes after a newer one started drops its result.
    reads: int = 0


def _playlist_filter_options(stored: Stored) -> dict[str, str]:
    targets = {rule.playlist_id for rule in stored.rules}
    playlists = {playlist_id: stored.playlist_title(playlist_id) for playlist_id in targets}
    return {ANY: _("All playlists"), **dict(sorted(playlists.items(), key=lambda item: item[1].casefold()))}


@register_page("/rules", label=N_("Rules"), icon="rule")
async def rules_page(services: Services) -> None:
    state = PageState(await asyncio.to_thread(read_stored, services.sessions, services.settings.tz))

    def current_filter() -> RuleFilter:
        playlist_id = playlist_select.value
        return RuleFilter(
            text=text_input.value or "",
            playlist_id=None if playlist_id in (None, ANY) else playlist_id,
            enabled=None if state_select.value == ANY else state_select.value == ENABLED,
        )

    async def reload() -> None:
        state.reads += 1
        read = state.reads
        stored = await asyncio.to_thread(read_stored, services.sessions, services.settings.tz)
        if read != state.reads:
            return
        state.stored = stored
        options = _playlist_filter_options(stored)
        playlist_select.set_options(options, value=playlist_select.value if playlist_select.value in options else ANY)
        rules_view.refresh()

    @ui.refreshable
    def rules_view() -> None:
        stored = state.stored
        if not stored.rules:
            ui.label(_("No rule yet. The button New rule creates one.")).mark("no-rules")
            return
        rule_filter = current_filter()
        shown = [rule for rule in stored.rules if rule_filter.matches(rule, stored.channels)]
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
        table.on("rowClick", lambda event: open_editor(int(event.args)), js_handler="(event, row) => emit(row.id)")

    def close_editor() -> None:
        editor_dialog.close()
        editor_view.refresh(None)

    @ui.refreshable
    def editor_view(editor: RuleEditor | None) -> None:
        if editor is None:
            return
        # Quasar limits the content of a dialog to 560px. The inline style gives the condition rows more room.
        with ui.card().classes("gap-2").style("width: min(64rem, 95vw); max-width: 95vw").mark("editor"):
            editor.build()

    async def open_editor(rule_id: int | None) -> None:
        # A fresh read gives the editor the newest rule and the newest copy of the account.
        await reload()
        rule = None
        if rule_id is not None:
            rule = state.stored.rule(rule_id)
            if rule is None:
                ui.notify(_("The rule does not exist any more."), type="warning")
                return
        editor_view.refresh(RuleEditor(services, state.stored, rule, close_editor, reload))
        editor_dialog.open()

    ui.label(_("Rules")).classes("text-h5")
    with ui.row().classes("w-full items-center gap-4"):
        text_input = (
            ui.input(_("Search"), on_change=lambda: rules_view.refresh())
            .props("outlined dense clearable")
            .classes("w-64")
            .mark("filter-text")
        )
        playlist_select = (
            ui.select(
                _playlist_filter_options(state.stored),
                label=_("Playlist"),
                value=ANY,
                on_change=lambda: rules_view.refresh(),
            )
            .props("outlined dense")
            .classes("w-56")
            .mark("filter-playlist")
        )
        state_select = (
            ui.select(
                {ANY: _("All"), **{value: _(label) for value, label in STATES.items()}},
                label=_("State"),
                value=ANY,
                on_change=lambda: rules_view.refresh(),
            )
            .props("outlined dense")
            .classes("w-40")
            .mark("filter-state")
        )
        ui.space()
        ui.button(_("New rule"), icon="add", on_click=lambda: open_editor(None)).mark("new-rule")
    rules_view()

    # A user can close the dialog only with its buttons, so an edit does not get lost by a click outside it.
    with ui.dialog().props("persistent").mark("editor-dialog") as editor_dialog:
        editor_view(None)
