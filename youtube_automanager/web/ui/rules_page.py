"""The rules page: the table of the rules with its filters, and the editor that creates, changes and deletes a rule.

A rule adds the matching videos of its channels to its target playlist, and a rule without channels reads the
subscriptions feed of the account. The page takes the channels and the playlists only from the stored copy of the
account, which the refresh of the account data writes, so a page load makes no request to YouTube. The page reads and
writes the database in a thread, never on the event loop. Each time shows in the time zone of the schedule.

The editor previews what the next sync run does with a rule, and the apply dialog adds the matching videos of a saved
rule since a day to its playlist. Both read YouTube in a child process through ``rule_actions``, and the apply is a
run of the history.
"""

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from nicegui import ui

from youtube_automanager.db.models import RunStatus
from youtube_automanager.engine.rule_preview import APPLY_LIMIT, READ_LIMIT, DraftRule, Mark, Preview, PreviewVideo
from youtube_automanager.i18n import N_, _
from youtube_automanager.rule_actions import AppliedRun, preview_in_child, read_applied
from youtube_automanager.rules_store import (
    ChannelChoice,
    RuleDraft,
    RuleRecord,
    Stored,
    delete_rule,
    read_stored,
    save_rule,
    validate_name,
    validate_playlist,
)
from youtube_automanager.run_events import ActionItem
from youtube_automanager.runner import RunAlreadyActive
from youtube_automanager.short_child import ChildError
from youtube_automanager.web.services import Services
from youtube_automanager.web.ui.account_lists import account_lists, child_error_text
from youtube_automanager.web.ui.common import busy, column, confirm, format_time
from youtube_automanager.web.ui.dates import DEFAULT_DAYS_BACK, default_first_day, today_button, today_in
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
    duration_fields,
    duration_value,
    from_tree,
    json_text,
    new_leaf,
    value_kind,
)

LOG = logging.getLogger(__name__)

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

# The first day of an apply and of the older videos of a rule is at most this many days back.
MAX_DAYS_BACK = 365
# The table of a preview shows this many videos on one page.
PREVIEW_PAGE_SIZE = 20
# Each add sends two requests with the cookies of the account, 5 s or more apart.
SECONDS_PER_ADD = 10

# The label and the color of each result of a preview, in the order of the counts.
MARKS = {
    Mark.WILL_ADD: (N_("Will be added"), "positive"),
    Mark.ALREADY_ADDED: (N_("Already in the playlist"), "grey"),
    Mark.WAITS_FOR_FIELD: (N_("Waits: no value yet for a field of the rule"), "warning"),
    Mark.WAITS_FOR_LIVE: (N_("Waits: the video is upcoming or live"), "warning"),
    Mark.WAITS_FOR_ACCOUNT: (N_("Waits for the YouTube account"), "warning"),
    Mark.MATCHES: (N_("Matches"), "positive"),
    Mark.MATCHES_NOW: (N_("Matches now"), "positive"),
    Mark.NO_LONGER_MATCHES: (N_("No longer matches"), "negative"),
    Mark.UNDECIDED: (N_("Undecided"), "warning"),
}

VIDEO_TITLE_CELL = """
<q-td :props="props">
  <a :href="props.row.url" target="_blank" rel="noopener noreferrer">{{ props.value }}</a>
</q-td>
"""
MARK_CELL = """
<q-td :props="props">
  <q-badge :color="props.row.mark_color">{{ props.value }}</q-badge>
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


def parse_since(text: str | None, today: date) -> date:
    """Return the first day of ``text`` in the form 2026-10-05.

    Raise ValueError with a message for the user when the day is missing, in the future, or more than MAX_DAYS_BACK
    days before ``today``.
    """
    try:
        day = date.fromisoformat((text or "").strip())
    except ValueError:
        raise ValueError(_("Choose the first day of the videos.")) from None
    if day > today:
        raise ValueError(_("Choose a day that is not in the future."))
    if day < today - timedelta(days=MAX_DAYS_BACK):
        raise ValueError(_("Choose a day at most {days} days back.").format(days=MAX_DAYS_BACK))
    return day


def parse_catch_up(text: str | None, today: date) -> date | None:
    """Return the day of the field Also add videos from, or None for an empty field.

    Raise ValueError with a message for the user when the day is in the future or more than MAX_DAYS_BACK days back.
    """
    if not (text or "").strip():
        return None
    try:
        return parse_since(text, today)
    except ValueError as error:
        raise ValueError(_("Also add videos from: {reason}").format(reason=error)) from None


def day_input(today: date) -> ui.input:
    """Add the input of the first day of an apply and of its preview, with the default day."""
    earliest = today - timedelta(days=MAX_DAYS_BACK)
    return (
        ui.input(_("Videos from"), value=(today - timedelta(days=DEFAULT_DAYS_BACK)).isoformat())
        .props(f"type=date outlined dense min={earliest.isoformat()} max={today.isoformat()}")
        .classes("w-48")
    )


def preview_columns() -> list[dict]:
    return [
        column("date", _("Date")),
        column("channel", _("Channel")),
        column("title", _("Title")),
        column("mark", _("Result")),
    ]


def preview_row(video: PreviewVideo) -> dict:
    label, color = MARKS[video.mark]
    return {
        "id": video.video_id,
        "date": video.upload_date.isoformat() if video.upload_date else _("No date"),
        "channel": video.channel_title,
        "title": video.title,
        "url": video.url,
        "mark": _(label),
        "mark_color": color,
    }


def counts_text(preview: Preview) -> str:
    """Return the count of the videos of each result, for example "Will be added: 3, Undecided: 1"."""
    parts = []
    for mark, (label, _color) in MARKS.items():
        if preview.count(mark):
            # Translators: {result} is the result of a preview for a video, for example "Will be added".
            part = _("{result}: {count}").format(result=_(label), count=preview.count(mark))
            parts.append(part)
    return ", ".join(parts)


def next_run_notes(preview: Preview, draft: DraftRule, *, enabled: bool) -> list[tuple[str, str]]:
    """Return the notes of a preview of the next run, each with the end of its marker, in plain words."""
    notes = []
    if not enabled:
        notes.append(
            (_("The rule is off, so no run uses it. The preview shows what the next run does with the rule on."), "off")
        )
    if preview.mode_changed:
        if draft.catch_up_since is None:
            text = _(
                "A setting of the service changed how far a run reads the channels. So the next run adds no video "
                "that is already on the channels. After that run, the rule adds the videos that come later."
            )
        else:
            text = _(
                "A setting of the service changed how far a run reads the channels. So the next run adds only the "
                "videos from the day in Also add videos from. After that run, the rule adds the videos that come later."
            )
        notes.append((text, "mode-changed"))
    elif preview.baseline_sources and draft.catch_up_since is None:
        notes.append(
            (
                _(
                    "The next run does not add the videos that are already on these channels: {channels}. Set Also "
                    "add videos from to add them from that day."
                ).format(channels=", ".join(preview.baseline_sources)),
                "not-added",
            )
        )
    if preview.count(Mark.WAITS_FOR_ACCOUNT):
        notes.append(
            (
                _(
                    "The YouTube account is not available now, so the matching videos wait for it. Upload new YouTube "
                    "cookies on the Settings page."
                ),
                "account",
            )
        )
    if preview.catch_up_adds > APPLY_LIMIT:
        notes.append(
            (
                _(
                    "The next run adds the {limit} oldest of the {count} videos from the day in Also add videos from. "
                    "The runs after it add the rest."
                ).format(limit=APPLY_LIMIT, count=preview.catch_up_adds),
                "limit",
            )
        )
    return notes


def add_text(count: int) -> str:
    """Return the text of the button that starts an apply of ``count`` videos."""
    if count == 1:
        return _("Add 1 video")
    return _("Add {count} videos").format(count=count)


def applied_text(applied: AppliedRun) -> str:
    """Tell how an apply ended."""
    if applied.status is not RunStatus.SUCCEEDED:
        return _("The apply failed: {error} The History page shows it as run {run}.").format(
            error=applied.error_text or "", run=applied.run_id
        )
    if applied.error_count:
        return _(
            "The apply is done with errors. Videos added: {count}. Errors: {errors}. The History page shows them in "
            "run {run}."
        ).format(count=applied.added_count, errors=applied.error_count, run=applied.run_id)
    return _("The apply is done. Videos added: {count}. The History page shows it as run {run}.").format(
        count=applied.added_count, run=applied.run_id
    )


def _same_tree(first: Any, second: Any) -> bool:
    # Python == takes 1 for true, so the comparison uses the JSON text.
    return json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


class PreviewView:
    """The place of the result of a preview: a message while it runs, the cause of a failure, or the listed videos.

    ``marker`` starts the marker of each part, and ``empty_text`` tells that the preview lists no video.
    """

    def __init__(self, marker: str, empty_text: str) -> None:
        self.marker = marker
        self.empty_text = empty_text
        self.container = ui.column().classes("w-full gap-2").mark(marker)

    def clear(self) -> None:
        self.container.clear()

    def working(self) -> None:
        self.container.clear()
        with self.container, ui.row().classes("items-center gap-2"):
            ui.spinner(size="sm")
            ui.label(_("The service reads the videos on YouTube. This takes up to a few minutes.")).mark(
                f"{self.marker}-working"
            )

    def failed(self, text: str) -> None:
        self.container.clear()
        with self.container:
            ui.label(text).classes("text-negative whitespace-pre-wrap").mark(f"{self.marker}-failed")

    def note(self, text: str) -> None:
        """Show only a note, for example that the result is old."""
        self.container.clear()
        with self.container:
            ui.label(text).classes("text-warning").mark(f"{self.marker}-note")

    def show(self, preview: Preview, notes: Sequence[tuple[str, str]] = (), footer: str | None = None) -> None:
        """Show the result. Each of ``notes`` is a text and the end of its marker, and it comes before the list, for
        example that the rule is off. ``footer`` comes under the list."""
        self.container.clear()
        with self.container:
            for text, name in notes:
                ui.label(text).classes("text-warning").mark(f"{self.marker}-{name}")
            for error in preview.errors:
                ui.label(error).classes("text-negative").mark(f"{self.marker}-error")
            if preview.cut_sources:
                ui.label(
                    _(
                        "The read stops after {limit} videos of a channel, so the older videos of these channels are "
                        "not in the list: {sources}"
                    ).format(limit=READ_LIMIT, sources=", ".join(preview.cut_sources))
                ).classes("text-warning").mark(f"{self.marker}-cut")
            if not preview.videos:
                ui.label(self.empty_text).mark(f"{self.marker}-empty")
                if footer:
                    ui.label(footer).classes("text-grey-7").mark(f"{self.marker}-footer")
                return
            ui.label(counts_text(preview)).mark(f"{self.marker}-counts")
            table = (
                ui.table(
                    columns=preview_columns(),
                    rows=[preview_row(video) for video in preview.videos],
                    row_key="id",
                    pagination=PREVIEW_PAGE_SIZE,
                )
                .props("flat bordered dense")
                .classes("w-full")
                .mark(f"{self.marker}-table")
            )
            table.add_slot("body-cell-title", VIDEO_TITLE_CELL)
            table.add_slot("body-cell-mark", MARK_CELL)
            if preview.more:
                ui.label(
                    _("The table shows the newest {shown} of the {total} videos.").format(
                        shown=len(preview.videos), total=preview.total
                    )
                ).mark(f"{self.marker}-more")
            if footer:
                ui.label(footer).classes("text-grey-7").mark(f"{self.marker}-footer")


def _marker(name: str, path: tuple[int, ...]) -> str:
    """The marker of a part of the condition editor. The path holds the index of each item from the root."""
    return "-".join([name, *map(str, path)])


class RuleEditor:
    """The form that creates or changes one rule. ``done`` runs after a save or a delete.

    ``open_apply`` opens the apply dialog of a saved rule, and it gets whether the form has changes that are not saved.
    """

    def __init__(
        self,
        services: Services,
        stored: Stored,
        rule: RuleRecord | None,
        close: Callable[[], None],
        done: Callable[[], Awaitable[None]],
        open_apply: Callable[[RuleRecord, bool], None] | None = None,
    ) -> None:
        self.services = services
        self.stored = stored
        self.rule = rule
        self.close = close
        self.done = done
        self.open_apply = open_apply
        # None when the visual editor cannot show the condition. The form then shows the condition as JSON text.
        self.root: Group | None = from_tree(rule.condition) if rule else Group()
        self.saving = False
        self.previewing = False
        # The number of the changes of the form. A preview that ends after a change shows no old result.
        self.changes = 0
        # True after the user cleared the field Also add videos from. A change of the rule then does not fill it again.
        self.catch_up_cleared = False

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
                ui.textarea(value=json_text(rule.condition), on_change=self._rule_changed)
                .props("outlined autogrow")
                .classes("w-full font-mono")
                .mark("rule-json")
            )
        elif self.root is not None:
            self._condition_editor(self.root)
        self.no_shorts_switch = ui.switch(
            _("No Shorts"), value=rule.no_shorts if rule else False, on_change=self._rule_changed
        ).mark("rule-no-shorts")
        self._catch_up_part()
        self._preview_part()
        self._actions()

    def _catch_up_part(self) -> None:
        """Add the field Also add videos from with its Today button.

        A new rule starts with DEFAULT_DAYS_BACK days back, and a saved rule with its stored day, which the next run
        clears after its catch-up.
        """
        rule = self.rule
        timezone = self.stored.timezone
        today = today_in(timezone)
        if rule is None:
            value: date | None = default_first_day(timezone)
        else:
            value = rule.catch_up_since
        earliest = today - timedelta(days=MAX_DAYS_BACK)
        with ui.row().classes("items-center gap-2"):
            self.catch_up_input = (
                ui.input(
                    _("Also add videos from"),
                    value=None if value is None else value.isoformat(),
                    on_change=self._catch_up_changed,
                )
                .props(f"type=date outlined dense clearable min={earliest.isoformat()} max={today.isoformat()}")
                .classes("w-56")
                .mark("rule-catch-up")
            )
            today_button(self.catch_up_input, timezone).mark("rule-catch-up-today")
        ui.label(
            _(
                "The next run also adds the videos from this day that the rule matches, the oldest first. Leave the "
                "field empty to add only new videos."
            )
        ).classes("text-grey-7").mark("rule-catch-up-hint")

    def _rule_changed(self) -> None:
        """Take a change of the condition, the channels or No Shorts.

        Such a change can match older videos, so the field Also add videos from gets DEFAULT_DAYS_BACK days back,
        unless it already holds an earlier day, or the user cleared it in this editor.
        """
        first = default_first_day(self.stored.timezone)
        try:
            current = date.fromisoformat((self.catch_up_input.value or "").strip())
        except ValueError:
            current = None
        if (current is None and not self.catch_up_cleared) or (current is not None and first < current):
            self.catch_up_input.set_value(first.isoformat())
        self._preview_old()

    def _catch_up_changed(self) -> None:
        """Take a change of the field Also add videos from. An empty field after a change is a clear by the user.

        The fill of ``_rule_changed`` always gives a day, so it never counts as a clear. A day in the field ends the clear.
        """
        self.catch_up_cleared = not (self.catch_up_input.value or "").strip()
        self._preview_old()

    def _preview_old(self) -> None:
        """Remove the preview after a change of the form, because the preview no longer shows what a save stores."""
        self.changes += 1
        if not self.previewing:
            self.preview_view.clear()

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
                on_change=self._rule_changed,
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
        self.playlist_select = (
            ui.select(
                own,
                label=_("Target playlist"),
                value=rule.playlist_id if rule else None,
                with_input=True,
                on_change=self._preview_old,
            )
            .props("outlined dense")
            .classes("w-full")
            .mark("rule-playlist")
        )
        if not own:
            ui.label(
                _(
                    "No playlist is stored yet. Close the editor and refresh the lists with the button Refresh lists "
                    "at the top of the page."
                )
            ).classes("text-warning").mark("rule-no-playlists")

    def _actions(self) -> None:
        """Add the switch that turns the rule on and off, the hint about new videos, the error and the buttons."""
        rule = self.rule
        self.enabled_switch = ui.switch(
            _("Enabled"), value=rule.enabled if rule else True, on_change=self._preview_old
        ).mark("rule-enabled")
        ui.label(
            _(
                "Each run adds the new videos that the rule matches. The videos that are already on the channels come "
                "only from the day in Also add videos from. A rule that is off adds nothing and keeps that day for "
                "the first run after you turn it on."
            )
        ).classes("text-grey-7").mark("rule-new-videos-hint")
        self.error_label = ui.label().classes("text-negative whitespace-pre-wrap").mark("editor-error")
        self.error_label.set_visibility(False)
        with ui.row().classes("w-full items-center gap-2"):
            self.save_button = ui.button(_("Save"), icon="save", on_click=self.save).mark("editor-save")
            ui.button(_("Cancel"), on_click=self.close).props("flat").mark("editor-cancel")
            if rule:
                ui.space()
                ui.button(_("Apply to videos from a date"), icon="playlist_add_check", on_click=self.apply).props(
                    "flat"
                ).mark("editor-apply")
                ui.button(_("Delete"), icon="delete", color="negative", on_click=self.ask_delete).props("flat").mark(
                    "editor-delete"
                )

    def _preview_part(self) -> None:
        """Add the preview of the form: the hint, the button and the result."""
        hint = _(
            "The preview shows what the next run does with the rule as the form shows it now: the videos that it "
            "will add, and the matching videos that wait. The preview adds nothing."
        )
        empty = _("The next run adds no video for this rule.")
        with ui.card().props("flat bordered").classes("w-full gap-2 p-2").mark("preview-part"):
            ui.label(_("Preview of the next run")).classes("text-subtitle1")
            ui.label(hint).classes("text-grey-7").mark("preview-hint")
            self.preview_button = (
                ui.button(_("Preview"), icon="visibility", on_click=self.preview).props("outline").mark("preview-run")
            )
            self.preview_view = PreviewView("preview", empty)

    # The condition editor.

    def _condition_editor(self, root: Group) -> None:
        @ui.refreshable
        def condition_view() -> None:
            self._group_view(root, None, ())

        self.condition_view = condition_view
        condition_view()

    def _changed(self) -> None:
        self.condition_view.refresh()
        self._rule_changed()

    def _set(self, item: Group | Leaf, name: str, value: Any) -> None:
        """Change one part of the condition that needs no new view."""
        setattr(item, name, value)
        self._rule_changed()

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
                    on_change=lambda event: self._set(group, "kind", GroupKind(event.value)),
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
                    on_change=lambda event: self._set(leaf, "case_sensitive", bool(event.value)),
                ).mark(_marker("case", path))
            ui.button(icon="close", on_click=lambda: self._remove(parent, path[-1])).props("flat round dense").classes(
                "ml-auto"
            ).tooltip(_("Remove the condition")).mark(_marker("remove", path))

    def _value_input(self, leaf: Leaf, path: tuple[int, ...]) -> None:
        def set_value(value: Any) -> None:
            self._set(leaf, "value", value)

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
                minutes, seconds = duration_fields(leaf.value)

                def set_duration() -> None:
                    set_value(duration_value(minutes_input.value, seconds_input.value))

                minutes_input = (
                    ui.number(_("Minutes"), value=minutes, min=0, step=1, precision=0, on_change=set_duration)
                    .props("outlined dense")
                    .classes("w-28")
                    .mark(_marker("minutes", path))
                )
                seconds_input = (
                    ui.number(_("Seconds"), value=seconds, min=0, max=59, step=1, precision=0, on_change=set_duration)
                    .props("outlined dense")
                    .classes("w-28")
                    .mark(_marker("seconds", path))
                )
            case ValueKind.DATE:
                day = (
                    ui.input(_("Date"), value=leaf.value, on_change=lambda event: set_value(event.value or None))
                    .props("type=date outlined dense")
                    .classes("w-48")
                    .mark(marker)
                )
                today_button(day, self.stored.timezone).mark(_marker("today", path))
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

    def read_condition(self) -> Any:
        """Return the condition of the form. Raise ValueError with a message for the user when it is not valid."""
        if self.root is None:
            return condition_from_json(self.json_input.value)
        return condition_of(self.root)

    def read_draft(self) -> RuleDraft:
        """Return the rule of the form. Raise ValueError with a message for the user when the form cannot be saved."""
        # The checks of the name and the playlist come first, as in validate_draft. The condition and the day come from
        # the widgets, so their conversion errors come after.
        name = validate_name(self.name_input.value)
        playlist_id = validate_playlist(self.playlist_select.value)
        return RuleDraft(
            name=name,
            enabled=bool(self.enabled_switch.value),
            channel_ids=tuple(self.channel_select.value or ()),
            playlist_id=playlist_id,
            no_shorts=bool(self.no_shorts_switch.value),
            condition=self.read_condition(),
            catch_up_since=self.read_catch_up(),
        )

    def read_catch_up(self) -> date | None:
        """Return the day of the field Also add videos from. Raise ValueError with a message for the user."""
        return parse_catch_up(self.catch_up_input.value, today_in(self.stored.timezone))

    def has_changes(self) -> bool:
        """Tell whether the form differs from the saved rule. A form that cannot be saved counts as changed."""
        rule = self.rule
        if rule is None:
            return True
        try:
            draft = self.read_draft()
        except ValueError:
            return True
        return (
            (draft.name, draft.enabled, draft.playlist_id, draft.no_shorts, draft.catch_up_since)
            != (rule.name, rule.enabled, rule.playlist_id, rule.no_shorts, rule.catch_up_since)
            or set(draft.channel_ids) != set(rule.channel_ids)
            or not _same_tree(draft.condition, rule.condition)
        )

    def show_error(self, text: str | None) -> None:
        self.error_label.set_text(text or "")
        self.error_label.set_visibility(bool(text))

    # The preview and the apply.

    async def preview(self) -> None:
        """Preview what the next run does with the rule of the form, also for a saved rule. The preview adds nothing."""
        if self.previewing:
            return
        try:
            draft = DraftRule(
                self.read_condition(),
                tuple(self.channel_select.value or ()),
                bool(self.no_shorts_switch.value),
                self.playlist_select.value or None,
                self.read_catch_up(),
            )
        except ValueError as error:
            self.preview_view.failed(str(error))
            return
        changes = self.changes
        self.previewing = True
        with busy(self.preview_button):
            self.preview_view.working()
            try:
                result: Preview | str = await asyncio.to_thread(
                    preview_in_child, self.services.runner, None, draft=draft
                )
            except ChildError as error:
                LOG.warning("The preview of a rule failed: %s", error)
                result = _("The preview failed. {reason}").format(reason=child_error_text(error))
            finally:
                self.previewing = False
        # The user can close the editor while the child runs.
        if self.preview_button.is_deleted:
            return
        if changes != self.changes:
            self.preview_view.note(_("The form changed while the preview ran. Press Preview again."))
            return
        if isinstance(result, str):
            self.preview_view.failed(result)
            return
        self.preview_view.show(
            result,
            next_run_notes(result, draft, enabled=bool(self.enabled_switch.value)),
            _("The next run also checks each new video that comes before the run starts."),
        )

    def apply(self) -> None:
        if self.rule is not None and self.open_apply is not None:
            self.open_apply(self.rule, self.has_changes())

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
        confirmed = await confirm(
            _('Delete the rule "{name}"?').format(name=rule.name),
            _("Delete"),
            marker="delete",
            detail=_("The history keeps the actions of the rule."),
            color="negative",
        )
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


class ApplyDialog:
    """The dialog that previews a saved rule for the videos since a day, and adds the matching ones to its playlist.

    The apply reads and evaluates the videos again, because the preview can be old. It is a run of the history with
    the trigger ``rule_apply``, and it adds at most APPLY_LIMIT videos, the oldest first. The apply runs in a task of
    the service, so it goes on after the close of the dialog, of the editor and of the tab. The dialog follows its run
    through the events of the runs.
    """

    def __init__(self, services: Services) -> None:
        self.services = services
        self.rule: RuleRecord | None = None
        self.timezone = services.settings.tz
        # The newest preview, and its first day. An apply starts only after a preview of the shown day.
        self.preview: Preview | None = None
        self.preview_day: date | None = None
        self.working = False
        # The run of the newest apply of this dialog, the number of videos that it adds, and whether its end is shown.
        # A progress of the run can come after its end, so the flag keeps the end text.
        self.run_id: int | None = None
        self.count = 0
        self.ended = False
        with ui.dialog().mark("apply-dialog") as self.dialog:
            self.card = ui.card().classes("gap-2").style("width: min(56rem, 95vw); max-width: 95vw")
        # The page subscribes here, so NiceGUI ends each subscription when the client of the page is deleted. The type
        # checker ty reads the callback of `Event.subscribe` as `() -> Any` only, and refuses a handler with arguments.
        services.run_events.progress.subscribe(self._on_progress)  # ty: ignore[invalid-argument-type]
        services.run_events.finished.subscribe(self._on_finished)  # ty: ignore[invalid-argument-type]
        services.applies.changed.subscribe(self._on_changed)

    def open(self, stored: Stored, rule: RuleRecord, *, unsaved: bool) -> None:
        """Show the dialog for ``rule``. While an apply runs, the dialog shows that apply."""
        if not self.working:
            self._build(stored, rule, unsaved)
        self.dialog.open()

    def _build(self, stored: Stored, rule: RuleRecord, unsaved: bool) -> None:
        self.rule = rule
        self.timezone = stored.timezone
        self.preview, self.preview_day = None, None
        self.card.clear()
        with self.card:
            ui.label(_('Apply the rule "{name}" to videos from a date').format(name=rule.name)).classes("text-h6").mark(
                "apply-title"
            )
            if unsaved:
                ui.label(
                    _("The editor has changes that are not saved. The apply uses the saved version of the rule.")
                ).classes("text-warning").mark("apply-unsaved")
            ui.label(
                _(
                    "The apply adds each video of the rule sources since the day that the rule matches to the playlist "
                    "{playlist}, the oldest first, and at most {limit} videos at a time. It skips each video that the "
                    "service added to this playlist before, each upcoming or live video, and each video that the rule "
                    "cannot decide yet."
                ).format(playlist=stored.playlist_title(rule.playlist_id), limit=APPLY_LIMIT)
            ).classes("text-grey-7").mark("apply-hint")
            with ui.row().classes("items-center gap-2"):
                self.since_input = day_input(today_in(self.timezone)).mark("apply-since")
                self.since_input.on_value_change(self._day_changed)
                today_button(self.since_input, self.timezone).mark("apply-since-today")
                self.preview_button = (
                    ui.button(_("Preview"), icon="visibility", on_click=self.run_preview)
                    .props("outline")
                    .mark("apply-preview")
                )
            self.view = PreviewView("apply-result", _("The rule matches no video of its sources since this day."))
            self.limit_label = ui.label().classes("text-warning").mark("apply-limit")
            self.limit_label.set_visibility(False)
            self.status_label = ui.label().classes("whitespace-pre-wrap").mark("apply-status")
            self.status_label.set_visibility(False)
            with ui.row().classes("w-full items-center gap-2"):
                self.add_button = ui.button(icon="playlist_add", on_click=self.run_apply).mark("apply-run")
                self.add_button.set_visibility(False)
                ui.space()
                ui.button(_("Close"), on_click=self.dialog.close).props("flat").mark("apply-close")

    def _day_changed(self) -> None:
        """Forget the preview of another day, so no apply starts with the count of that day."""
        if self.working:
            return
        self.preview, self.preview_day = None, None
        self.view.clear()
        self.add_button.set_visibility(False)
        self.limit_label.set_visibility(False)

    def _set_working(self, working: bool) -> None:
        self.working = working
        for button in (self.preview_button, self.add_button):
            if working:
                button.props("loading")
                button.disable()
            else:
                button.props(remove="loading")
                button.enable()
        self.since_input.set_enabled(not working)

    def _status(self, text: str, *, error: bool = False) -> None:
        if error:
            self.status_label.classes(add="text-negative")
        else:
            self.status_label.classes(remove="text-negative")
        self.status_label.set_text(text)
        self.status_label.set_visibility(bool(text))

    async def run_preview(self) -> None:
        if self.working or self.rule is None:
            return
        self._day_changed()
        self._status("")
        try:
            since = parse_since(self.since_input.value, today_in(self.timezone))
        except ValueError as error:
            self.view.failed(str(error))
            return
        self._set_working(True)
        self.view.working()
        try:
            result = await asyncio.to_thread(preview_in_child, self.services.runner, since, rule_id=self.rule.id)
        except ChildError as error:
            LOG.warning("The preview of rule %s failed: %s", self.rule.id, error)
            self.view.failed(_("The preview failed. {reason}").format(reason=child_error_text(error)))
            return
        finally:
            self._set_working(False)
        self.view.show(result)
        self.preview, self.preview_day = result, since
        count = result.count(Mark.WILL_ADD)
        if count:
            self.add_button.set_text(add_text(min(count, APPLY_LIMIT)))
            self.add_button.set_visibility(True)
        if count > APPLY_LIMIT:
            self.limit_label.set_text(
                _("One apply adds the {limit} oldest of the {count} videos. Apply again to add the rest.").format(
                    limit=APPLY_LIMIT, count=count
                )
            )
            self.limit_label.set_visibility(True)

    async def run_apply(self) -> None:
        if self.working or self.rule is None or self.preview is None or self.preview_day is None:
            return
        rule, since = self.rule, self.preview_day
        count = min(self.preview.count(Mark.WILL_ADD), APPLY_LIMIT)
        self._set_working(True)
        self.add_button.set_visibility(False)
        self._status(
            _(
                "The apply runs. Each add takes {seconds} s or more, so the apply takes {minutes} min or more. The "
                "History page shows the apply as a run."
            ).format(seconds=SECONDS_PER_ADD, minutes=math.ceil(count * SECONDS_PER_ADD / 60))
        )
        try:
            run_id = await self.services.applies.start(rule.id, since)
        except ChildError as error:
            LOG.warning("The apply of rule %s did not start: %s", rule.id, error)
            self._set_working(False)
            self._status(_("The apply did not start. {reason}").format(reason=child_error_text(error)), error=True)
            self.add_button.set_visibility(True)
            return
        except BaseException:
            # NiceGUI tells the user about another error, and the dialog lets the user try again.
            self._set_working(False)
            self.add_button.set_visibility(True)
            raise
        self.run_id, self.count, self.ended = run_id, count, False
        # The preview is old now, so a new apply needs a new preview.
        self.preview, self.preview_day = None, None
        self.limit_label.set_visibility(False)

    def _on_progress(self, run_id: int, added: int, seen: int, actions: tuple[ActionItem, ...]) -> None:
        if run_id != self.run_id or self.ended:
            return
        self._status(
            _(
                "The apply runs. Videos added: {added} of {count}. The History page shows the apply as run {run}."
            ).format(added=added, count=self.count, run=run_id)
        )

    async def _on_finished(self, run_id: int, status: RunStatus) -> None:
        if run_id == self.run_id:
            await self._show_end(run_id)

    async def _on_changed(self) -> None:
        # An apply that ends at once can end before ``run_apply`` learns its run, and then only the end of its task
        # tells the dialog.
        if self.run_id is not None and not self.services.applies.is_active(self.run_id):
            await self._show_end(self.run_id)

    async def _show_end(self, run_id: int) -> None:
        """Show how the apply of ``run_id`` ended. The row of the run tells it, because the write of the end can fail."""
        if self.ended:
            return
        self.ended = True
        applied = await asyncio.to_thread(read_applied, self.services.sessions, run_id)
        self._set_working(False)
        if applied is None:
            text = _("The apply ended, but its run {run} is not in the database any more.").format(run=run_id)
            kind = "negative"
        else:
            text = applied_text(applied)
            if applied.status is not RunStatus.SUCCEEDED:
                kind = "negative"
            elif applied.error_count:
                kind = "warning"
            else:
                kind = "positive"
        self._status(text, error=kind == "negative")
        ui.notify(text, type=kind)


class ApplyBanner:
    """The line above the rules that tells about an apply that runs, also about one from another tab or an earlier load
    of the page. It follows the apply through the events of the runs."""

    def __init__(self, services: Services, rule_name: Callable[[int], str]) -> None:
        self.services = services
        self.rule_name = rule_name
        # The added count of each apply run, and the runs whose end came. A progress can come after the end, and then
        # ``show`` keeps the banner of that run hidden.
        self.added: dict[int, int] = {}
        self.ended: set[int] = set()
        with ui.row().classes("w-full items-center gap-2 text-primary").mark("apply-banner") as self.row:
            ui.spinner(size="sm")
            self.label = ui.label().mark("apply-banner-text")
        self.row.set_visibility(False)
        services.run_events.progress.subscribe(self._on_progress)  # ty: ignore[invalid-argument-type]
        services.run_events.finished.subscribe(self._on_finished)  # ty: ignore[invalid-argument-type]
        services.applies.changed.subscribe(self.show)

    async def load(self) -> None:
        """Read the added count of the active apply, so a new load of the page shows it before the next progress."""
        current = self.services.applies.current()
        if current is not None:
            applied = await asyncio.to_thread(read_applied, self.services.sessions, current.run_id)
            if applied is not None:
                # A progress during the read can have brought a newer count.
                self.added[current.run_id] = max(self.added.get(current.run_id, 0), applied.added_count)
        self.show()

    def show(self) -> None:
        current = self.services.applies.current()
        if current is None or current.run_id in self.ended:
            self.row.set_visibility(False)
            return
        self.label.set_text(
            _('An apply of the rule "{name}" runs. Videos added: {count}.').format(
                name=self.rule_name(current.rule_id), count=self.added.get(current.run_id, 0)
            )
        )
        self.row.set_visibility(True)

    def _on_progress(self, run_id: int, added: int, seen: int, actions: tuple[ActionItem, ...]) -> None:
        self.added[run_id] = added
        self.show()

    def _on_finished(self, run_id: int, status: RunStatus) -> None:
        self.ended.add(run_id)
        self.show()


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
        editor_view.refresh(RuleEditor(services, state.stored, rule, close_editor, reload, open_apply))
        editor_dialog.open()

    def open_apply(rule: RuleRecord, unsaved: bool) -> None:
        apply_dialog.open(state.stored, rule, unsaved=unsaved)

    ui.label(_("Rules")).classes("text-h5")

    def rule_name(rule_id: int) -> str:
        rule = state.stored.rule(rule_id)
        return rule.name if rule is not None else str(rule_id)

    await ApplyBanner(services, rule_name).load()
    # The editor reads the stored lists again at each open, so a refresh needs only a new read of the table.
    await account_lists(services, on_refreshed=reload)
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
    apply_dialog = ApplyDialog(services)
