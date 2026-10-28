"""The editor of a rule, a sub page of the rules page: ``/rules/new`` creates a rule, and ``/rules/<id>`` changes or
deletes the rule with that id.

The editor previews what the next sync run does with a rule, and the apply dialog adds the matching videos of a saved
rule since a day to its playlist. Both read YouTube in a child process through ``rule_actions``, and the apply is a
run of the history. The page reads and writes the database in a thread, never on the event loop.

The address of the editor keeps the query of the rules page that opened it, so Save, Delete and Cancel go back to the
rules with the same filters, also after a reload of the editor.

Duplicate opens ``/rules/new?from=<id>``: a new rule with a copy of the saved rule <id> and the name "Copy of <name>".
The address holds only the id, so a reload shows the same copy, and the copy comes from the database, not from the form.
"""

import asyncio
import copy
import dataclasses
import json
import logging
import math
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from nicegui import context, ui

from youtube_automanager.db.models import RunStatus
from youtube_automanager.engine.rule_preview import APPLY_LIMIT, READ_LIMIT, DraftRule, Mark, Preview, PreviewVideo
from youtube_automanager.i18n import N_, _
from youtube_automanager.pacing import minimum_seconds
from youtube_automanager.rule_actions import AppliedRun, preview_in_child, read_applied
from youtube_automanager.rules_store import (
    MAX_DAYS_BACK,
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
from youtube_automanager.web.ui.account_lists import child_error_text
from youtube_automanager.web.ui.common import (
    busy,
    column,
    confirm,
    day_column,
    focus_when_drawn,
    icon_button,
    named,
    notify_in,
    set_loading,
    whole_number,
)
from youtube_automanager.web.ui.dates import DEFAULT_DAYS_BACK, default_first_day, today_button, today_in
from youtube_automanager.web.ui.layout import LeaveCheck, address_query, with_query
from youtube_automanager.web.ui.live_runs import Subscriptions
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
    duration_fields,
    duration_value,
    from_tree,
    json_text,
    new_leaf,
    value_kind,
)

LOG = logging.getLogger(__name__)

RULES_PATH = "/rules"
NEW_RULE_PATH = "/rules/new"
# The key of ``app.storage.client`` that tells the rules page where the focus goes after the editor: the id of the
# rule whose Edit button gets it, or None for the button New rule.
RETURN_FOCUS = "rules-return-focus"
# The query parameter of ``/rules/new`` with the id of the rule that the new rule copies.
COPY_PARAMETER = "from"
# The attribute of the select that searches and adds the channels of a rule. Its input takes the focus after the remove
# of the last channel of the list. Quasar puts the attribute of a select with an input on the input itself.
CHANNEL_SEARCH_ATTRIBUTE = "data-channel-search"
CHANNEL_SEARCH_SELECTOR = f"input[{CHANNEL_SEARCH_ATTRIBUTE}]"
# The attribute of the remove button of a channel in the list under the select. Its value is the id of the channel.
CHANNEL_REMOVE_ATTRIBUTE = "data-channel-remove"
# The browser code that empties the search text of the select and closes its menu after the user chose a channel.
# Quasar keeps the text and the menu of a select with ``multiple``, so the user had to delete the text before the next
# search, and the open menu covered the list under the field. The next typed letter opens the menu again, and the focus
# stays in the search. The second argument ``true`` skips the filter of Quasar, which would open the menu again.
# ``{id}`` is the id of the select.
CLEAR_SEARCH_HANDLER = "() => {{ runMethod({id}, 'updateInputValue', ['', true]); runMethod({id}, 'hidePopup', []); }}"

# The table of a preview shows this many videos on one page.
PREVIEW_PAGE_SIZE = 20
# Each add sends two requests with the cookies of the account, and each request takes a token of the budget.
REQUESTS_PER_ADD = 2

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


def channel_label(channel: ChannelChoice) -> str:
    label = f"{channel.title} ({channel.handle})" if channel.handle else channel.title
    if not channel.is_subscribed:
        # Translators: a channel that left the subscriptions. A rule can still read it by its id.
        label = _("{channel}, not subscribed").format(channel=label)
    return label


def channel_remove_selector(channel_id: str) -> str:
    """Return the CSS selector of the remove button of the channel ``channel_id`` in the list of the rule editor."""
    return f"[{CHANNEL_REMOVE_ATTRIBUTE}={json.dumps(channel_id)}]"


def sorted_channels(channel_ids: Sequence[str], options: dict[str, str]) -> list[tuple[str, str]]:
    """Return each id of ``channel_ids`` with its label of ``options``, in the order of the labels, case-insensitively.

    An id without a label shows as its id.
    """
    labelled = [(channel_id, options.get(channel_id, channel_id)) for channel_id in channel_ids]
    return sorted(labelled, key=lambda item: (item[1].casefold(), item[1], item[0]))


def copy_name(name: str) -> str:
    """Return the name of a copy of the rule with the name ``name``."""
    # Translators: the name of a new rule that the button Duplicate makes from the rule {name}.
    return _("Copy of {name}").format(name=name)


def apply_status(count: int) -> str:
    """Return the status text of an apply of ``count`` adds, with the shortest time that the request budget allows.

    An apply shorter than one minute gets no time, because the answers of YouTube make up most of it.
    """
    minutes = math.floor(minimum_seconds(count * REQUESTS_PER_ADD) / 60)
    if minutes < 1:
        return _("The apply runs. The History page shows the apply as a run.")
    return _("The apply runs and takes {minutes} min or more. The History page shows the apply as a run.").format(
        minutes=minutes
    )


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
        day_column("date", _("Date")),
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
    """The form that creates or changes one rule.

    ``leave`` goes back to the rules after a save, a delete or a cancel. ``open_apply`` opens the apply dialog of a
    saved rule, and it gets whether the form has changes that are not saved. ``changed`` gets the same after each change
    of a field. ``duplicate`` opens a new rule with a copy of the saved rule that it gets. ``template`` gives a new rule
    the fields of a rule, for example of the rule that it copies.
    """

    def __init__(
        self,
        services: Services,
        stored: Stored,
        rule: RuleRecord | None,
        leave: Callable[[], None],
        open_apply: Callable[[RuleRecord, bool], None] | None = None,
        changed: Callable[[bool], None] | None = None,
        *,
        duplicate: Callable[[RuleRecord], None] | None = None,
        template: RuleRecord | None = None,
    ) -> None:
        self.services = services
        self.stored = stored
        self.rule = rule
        # The rule whose fields the form opens with: the saved rule, or the template of a new rule.
        self.source = rule if rule is not None else template
        self.duplicate_to = duplicate
        self.leave = leave
        self.open_apply = open_apply
        self.changed = changed
        # The values of the fields when the form opened. A new rule has changes when its fields differ from them.
        self.initial: tuple | None = None
        # None when the visual editor cannot show the condition. The form then shows the condition as JSON text.
        self.root: Group | None = from_tree(self.source.condition) if self.source else Group()
        self.saving = False
        self.previewing = False
        # The number of the changes of the form. A preview that ends after a change shows no old result.
        self.changes = 0
        # True after the user cleared the field Also add videos from. A change of the rule then does not fill it again.
        self.catch_up_cleared = False

    def build(self) -> None:
        # Cancel stays on while a save or a delete runs, and a confirmed Cancel leaves the editor and deletes the button
        # of the click. So the result of the save or the delete goes through the client.
        self.client = context.client
        rule = self.source
        ui.label(_("Change the rule") if self.rule else _("New rule")).classes("text-h6").mark("editor-title")
        self.name_input = (
            ui.input(_("Name"), value=rule.name if rule else "", on_change=self._form_changed)
            .props("outlined dense")
            .classes("w-full")
        ).mark("rule-name")
        self._sources()
        ui.label(_("Condition")).classes("text-subtitle1")
        if rule is not None and self.root is None:
            ui.label(_("The visual editor cannot show this condition. Edit it as JsonLogic.")).classes("text-grey-7")
            self.json_input = named(
                ui.textarea(value=json_text(rule.condition), on_change=self._rule_changed)
                .props("outlined autogrow")
                .classes("w-full font-mono")
                .mark("rule-json"),
                _("Condition as JsonLogic"),
            )
        elif self.root is not None:
            self._condition_editor(self.root)
        self.no_shorts_switch = ui.switch(
            _("No Shorts"), value=rule.no_shorts if rule else False, on_change=self._rule_changed
        ).mark("rule-no-shorts")
        self._catch_up_part()
        self._preview_part()
        self._actions()
        # A new rule has no saved version, so a change is a difference from the form that the editor opened with.
        self.initial = self._form()

    def _form(self) -> tuple:
        """Return the values of the fields of the form, to compare them with the values of another moment."""
        condition = copy.deepcopy(self.root) if self.root is not None else self.json_input.value
        return (
            self.name_input.value,
            self.enabled_switch.value,
            list(self.channel_select.value or ()),
            self.playlist_select.value,
            self.no_shorts_switch.value,
            condition,
            self.catch_up_input.value,
        )

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
        self._form_changed()

    def _form_changed(self) -> None:
        """Tell ``changed`` whether the form has changes that are not saved, after a change of a field."""
        if self.changed is not None and self.initial is not None:
            self.changed(self.has_changes())

    def _sources(self) -> None:
        """Add the channels that the rule reads and its target playlist."""
        rule = self.source
        channel_ids = list(rule.channel_ids) if rule else []
        # The labels of the channels that the select offers, by their ids.
        self.channel_labels = channel_options(self.stored, channel_ids)
        # The select searches and adds the channels. It hides the chosen channels, and the list under it shows them.
        self.channel_select = (
            ui.select(
                self.channel_labels,
                label=_("Channels"),
                value=channel_ids,
                multiple=True,
                with_input=True,
                on_change=self._channels_changed,
            )
            .props(f"outlined dense hide-selected {CHANNEL_SEARCH_ATTRIBUTE}")
            .classes("w-full")
            .mark("rule-channels")
        )
        self.channel_select.on("add", js_handler=CLEAR_SEARCH_HANDLER.format(id=self.channel_select.id))
        self.channel_list = ui.column().classes("w-full gap-0").mark("rule-channel-list")
        self._draw_channels()
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

    def _channels_changed(self) -> None:
        """Take a change of the channels: draw their list again, then do what each change of the rule does."""
        self._draw_channels()
        self._rule_changed()

    def _draw_channels(self) -> None:
        """Draw the list of the chosen channels under the select, sorted by their labels, each with a remove button."""
        self.channel_list.clear()
        with self.channel_list:
            for channel_id, label in sorted_channels(self.channel_select.value or (), self.channel_labels):
                with ui.row().classes("w-full items-center gap-1 no-wrap").mark("rule-channel") as row:
                    row.props["data-channel"] = channel_id
                    ui.label(label).classes("ellipsis").mark("rule-channel-label")
                    # Translators: the name of the button that removes the channel {channel} from the rule.
                    text = _("Remove {channel}").format(channel=label)
                    button = icon_button("close", text, lambda channel_id=channel_id: self._remove_channel(channel_id))
                    button.props[CHANNEL_REMOVE_ATTRIBUTE] = channel_id
                    button.mark("rule-channel-remove")

    def _remove_channel(self, channel_id: str) -> None:
        """Remove the channel ``channel_id`` from the rule, and give the keyboard focus to the next remove button.

        The next button is the one of the next row, or of the row before when the channel was the last. Without
        channels, the search of the select gets the focus.
        """
        select = self.channel_select
        ids = [item for item, _label in sorted_channels(select.value or (), self.channel_labels)]
        if channel_id not in ids:
            return
        index = ids.index(channel_id)
        rest = ids[:index] + ids[index + 1 :]
        select.set_value([item for item in select.value if item != channel_id])
        if rest:
            target = channel_remove_selector(rest[min(index, len(rest) - 1)])
        else:
            target = CHANNEL_SEARCH_SELECTOR
        focus_when_drawn(select.client, target, fallback=CHANNEL_SEARCH_SELECTOR)

    def _actions(self) -> None:
        """Add the switch that turns the rule on and off, the hint about new videos, the error and the buttons."""
        rule = self.rule
        source = self.source
        self.enabled_switch = ui.switch(
            _("Enabled"), value=source.enabled if source else True, on_change=self._preview_old
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
            ui.button(_("Cancel"), on_click=self.cancel).props("flat").mark("editor-cancel")
            if rule:
                ui.space()
                ui.button(_("Duplicate"), icon="content_copy", on_click=self.duplicate).props("flat").mark(
                    "editor-duplicate"
                )
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
                named(
                    ui.select(
                        {kind: _(label) for kind, label in GROUP_KINDS.items()},
                        value=group.kind,
                        on_change=lambda event: self._set(group, "kind", GroupKind(event.value)),
                    )
                    .props("outlined dense")
                    .classes("w-64")
                    .mark(_marker("group-kind", path)),
                    _("Group type"),
                )
                ui.space()
                if parent is not None:
                    icon_button("delete", _("Remove the group"), lambda: self._remove(parent, path[-1])).mark(
                        _marker("remove", path)
                    )
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
            named(
                ui.select({name: _(label) for name, label in FIELDS.items()}, value=leaf.field, on_change=set_field)
                .props("outlined dense")
                .classes("w-48")
                .mark(_marker("field", path)),
                _("Field"),
            )
            if len(OPERATORS[leaf.field]) > 1:
                named(
                    ui.select(
                        {operator: _(label) for operator, label in OPERATORS[leaf.field].items()},
                        value=leaf.operator,
                        on_change=set_operator,
                    )
                    .props("outlined dense")
                    .classes("w-48")
                    .mark(_marker("operator", path)),
                    _("Operator"),
                )
            self._value_input(leaf, path)
            if leaf.operator == "regex":
                ui.checkbox(
                    _("Case-sensitive"),
                    value=leaf.case_sensitive,
                    on_change=lambda event: self._set(leaf, "case_sensitive", bool(event.value)),
                ).mark(_marker("case", path))
            icon_button("close", _("Remove the condition"), lambda: self._remove(parent, path[-1])).classes(
                "ml-auto"
            ).mark(_marker("remove", path))

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
                named(
                    ui.select(
                        channel_options(self.stored, [leaf.value] if leaf.value else []),
                        value=leaf.value,
                        with_input=True,
                        on_change=lambda event: set_value(event.value),
                    )
                    .props("outlined dense")
                    .classes("flex-1 min-w-48")
                    .mark(marker),
                    _("Channel"),
                )
            case ValueKind.CHANNELS:
                named(
                    ui.select(
                        channel_options(self.stored, list(leaf.value or [])),
                        value=list(leaf.value or []),
                        multiple=True,
                        with_input=True,
                        on_change=lambda event: set_value(list(event.value or [])),
                    )
                    .props("outlined dense use-chips")
                    .classes("flex-1 min-w-48")
                    .mark(marker),
                    _("Channels"),
                )
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
                named(
                    ui.select(
                        {status: _(label) for status, label in LIVE_STATUSES.items()},
                        value=leaf.value,
                        on_change=lambda event: set_value(event.value),
                    )
                    .props("outlined dense")
                    .classes("w-64")
                    .mark(marker),
                    _("Live status"),
                )
            case ValueKind.SHORT:
                named(
                    ui.select(
                        {choice: _(label) for choice, label in SHORT_CHOICES.items()},
                        value=leaf.value,
                        on_change=lambda event: set_value(event.value),
                    )
                    .props("outlined dense")
                    .classes("w-48")
                    .mark(marker),
                    _("Short"),
                )

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
        """Tell whether the form differs from the saved rule. A form that cannot be saved counts as changed.

        A new rule has changes when a field differs from the form that the editor opened with.
        """
        rule = self.rule
        if rule is None:
            return self.initial is not None and self._form() != self.initial
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
            notify_in(self.client, _("The rule does not exist any more, so the page did not save it."), type="warning")
        else:
            notify_in(self.client, _("The rule is saved."), type="positive")
        self._leave_once()

    def _leave_once(self) -> None:
        """Go back to the rules, unless the user left the editor while the database worked."""
        if not self.save_button.is_deleted:
            self.leave()

    async def may_leave(self) -> bool:
        """Return True when the user may leave the editor: the form has no changes, or the user discards them."""
        if not self.has_changes():
            return True
        return await confirm(
            _("Discard the changes?"),
            _("Discard"),
            marker="discard",
            color="negative",
            cancel_label=_("Keep editing"),
        )

    async def cancel(self) -> None:
        """Go back to the rules without a save. A form with changes asks first."""
        if await self.may_leave():
            self._leave_once()

    async def duplicate(self) -> None:
        """Open a new rule with a copy of the saved rule. A form with changes asks first, because the copy takes the
        saved rule and not the changes."""
        rule = self.rule
        if rule is None or self.duplicate_to is None:
            return
        if await self.may_leave() and not self.save_button.is_deleted:
            self.duplicate_to(rule)

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
            notify_in(
                self.client,
                _("A sync run or another task with YouTube is active now. Delete the rule when it ends."),
                type="warning",
            )
            return
        notify_in(self.client, _("The rule is deleted."), type="positive")
        self._leave_once()


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
        # The end of an apply comes through an event, and the tab can close while the dialog reads the end. So the end
        # goes through the client, which skips a closed tab.
        self.client = context.client
        with ui.dialog().mark("apply-dialog") as self.dialog:
            self.card = ui.card().classes("gap-2").style("width: min(56rem, 95vw); max-width: 95vw")
        # The subscriptions end with the content of the page: at a click in the drawer, and when the tab closes.
        subscriptions = Subscriptions()
        subscriptions.subscribe(services.run_events.progress, self._on_progress)
        subscriptions.subscribe(services.run_events.finished, self._on_finished)
        subscriptions.subscribe(services.applies.changed, self._on_changed)

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
            set_loading(button, working)
            button.set_enabled(not working)
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
        self._status(apply_status(count))
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
        notify_in(self.client, text, type=kind)


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
        subscriptions = Subscriptions()
        subscriptions.subscribe(services.run_events.progress, self._on_progress)
        subscriptions.subscribe(services.run_events.finished, self._on_finished)
        subscriptions.subscribe(services.applies.changed, self.show)

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


def rules_query(address: str) -> dict[str, str]:
    """Return the query of the rules page in ``address``, the address of an editor: its query without the id of the
    rule that a new rule copies."""
    return {name: value for name, value in parse_qsl(urlsplit(address).query) if name != COPY_PARAMETER}


def rules_address(address: str) -> str:
    """Return the address of the rules with the query of ``address``, the address of an editor.

    The rules page opens the editor with its own query, so the editor goes back to the rules with the same filters.
    """
    query = rules_query(address)
    return f"{RULES_PATH}?{urlencode(query)}" if query else RULES_PATH


def copy_address(rule_id: int, address: str) -> str:
    """Return the address of a new rule that copies the rule ``rule_id``, with the query of the rules in ``address``."""
    return with_query(NEW_RULE_PATH, {COPY_PARAMETER: str(rule_id), **rules_query(address)})


async def new_rule_page(services: Services) -> None:
    """The page of the address ``/rules/new``: the editor of a new rule.

    With the query parameter ``from``, the new rule starts with the fields of that saved rule and the name "Copy of
    <name>". A ``from`` that names no rule gives an empty new rule. The builder reads the query itself: a parameter of
    the builder would make ``ui.sub_pages`` build the page again at each change of the query.
    """
    await editor_page(services, None)


async def edit_rule_page(services: Services, rule_id: str) -> None:
    """The page of the address ``/rules/<id>``: the editor of the rule with that id.

    The id is the text of the address, so the page converts it, and an id that is not a whole number or not a stored
    rule gets a message and a link back to the rules.
    """
    await editor_page(services, rule_id)


async def editor_page(services: Services, rule_id: str | None) -> None:
    """Show the editor of the rule ``rule_id``, the text of the address, or of a new rule when it is None."""
    client = context.client
    back = rules_address(client.sub_pages_router.current_path)
    stored = await asyncio.to_thread(read_stored, services.sessions, services.settings.tz)
    rule = None
    template = None
    if rule_id is None:
        source_id = whole_number(address_query(client).get(COPY_PARAMETER))
        source = None if source_id is None else stored.rule(source_id)
        if source is not None:
            template = dataclasses.replace(source, name=copy_name(source.name))
    else:
        number = whole_number(rule_id)
        rule = None if number is None else stored.rule(number)
        if rule is None:
            ui.label(_("Change the rule")).classes("text-h5")
            ui.label(_("No rule has the id {id}. Another tab can have deleted it.").format(id=rule_id)).mark(
                "no-such-rule"
            )
            ui.link(_("Back to the rules"), back).mark("back-to-rules")
            return

    def leave() -> None:
        # The rules page moves the focus to the Edit button of this rule, or to New rule when the rule is gone.
        client.storage[RETURN_FOCUS] = rule.id if rule is not None else None
        with client:
            ui.navigate.to(back)

    def rule_name(other_id: int) -> str:
        other = stored.rule(other_id)
        return other.name if other is not None else str(other_id)

    def open_apply(saved: RuleRecord, unsaved: bool) -> None:
        apply_dialog.open(stored, saved, unsaved=unsaved)

    def duplicate(saved: RuleRecord) -> None:
        with client:
            ui.navigate.to(copy_address(saved.id, client.sub_pages_router.current_path))

    await ApplyBanner(services, rule_name).load()
    # A click in the drawer asks before it leaves a form with changes, and the browser warns before a reload or a close of
    # the tab. The Back button of the browser leaves without a question, see LeaveCheck.
    leave_check = LeaveCheck(lambda: editor.may_leave())
    editor = RuleEditor(
        services, stored, rule, leave, open_apply, leave_check.warn_on_unload, duplicate=duplicate, template=template
    )
    with ui.card().classes("w-full gap-2").mark("editor"):
        editor.build()
    apply_dialog = ApplyDialog(services)
