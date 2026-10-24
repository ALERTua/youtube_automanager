"""The notification targets block of the settings page: a card for each target and the form that edits a target.

Each card shows only the scheme of its URL, because a URL holds the token of its service. The form that changes a
target shows its stored URL, because the user needs to see the URL to change it. The web UI has no password by
default, and ``UI_PASSWORD`` in ``.env`` adds one. The password goes over plain HTTP, so the deployer must reach the
service through a VPN or a reverse proxy with HTTPS. No log line of the block holds a target URL.

The events, the lowest level and the switch of each card are part of the draft of the page, and the floating Save
of the page writes only the choices that changed. The form adds or changes a target at once, and a delete acts at once
too. After each of them and after a save, the block reads the targets again. It keeps each choice of the draft that
differs from the choices that the page read or saved before, and each other choice takes the new value from the
database. The draft of a target that went goes with it.

A test notification can take up to a minute, so the block sends it in a thread. The block reads and writes the database
in a thread, never on the event loop. Each write holds the lock of the page, so one write runs at a time.
"""

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from nicegui import ui

from youtube_automanager.i18n import _
from youtube_automanager.notify import EVENT_LEVELS, Event, Level, Notifier
from youtube_automanager.web.services import Services
from youtube_automanager.web.ui.common import confirm
from youtube_automanager.web.ui.notification_targets import (
    APPRISE_URLS,
    EVENT_LABELS,
    LEVEL_LABELS,
    SaveOutcome,
    TargetChoices,
    TargetRecord,
    delete_target,
    keep_edits,
    load_target,
    read_targets,
    save_choices,
    save_target,
)
from youtube_automanager.web.ui.settings_draft import DraftPart, SettingsDraft, Write

LOG = logging.getLogger(__name__)


class TargetEditor:
    """The form that creates a target, or changes the name and the URL of a target. ``done`` runs after a save.

    ``url`` is the stored URL of the target, or None for a new target.
    """

    def __init__(
        self,
        services: Services,
        writes: asyncio.Lock,
        target: TargetRecord | None,
        url: str | None,
        close: Callable[[], None],
        done: Callable[[], Awaitable[None]],
    ) -> None:
        self.services = services
        self.writes = writes
        self.target = target
        self.url = url
        self.close = close
        self.done = done
        self.saving = False

    def build(self) -> None:
        target = self.target
        ui.label(_("Change the target") if target else _("New target")).classes("text-h6").mark("target-editor-title")
        self.name_input = (
            ui.input(_("Name"), value=target.name if target else "")
            .props("outlined dense")
            .classes("w-full")
            .mark("target-name")
        )
        if target:
            # The stored URL shows in plain text, so the user can see it and change it.
            self.url_input = (
                ui.input(_("Apprise URL"), value=self.url or "")
                .props("outlined dense autocomplete=off")
                .classes("w-full")
                .mark("target-url")
            )
        else:
            # A password field, so the token of a typed URL does not show on the screen.
            self.url_input = (
                ui.input(_("Apprise URL"), password=True, password_toggle_button=True)
                .props("outlined dense autocomplete=new-password")
                .classes("w-full")
                .mark("target-url")
            )
        ui.label(
            _("The URL names the service and its token, for example tgram://bottoken/ChatID for Telegram.")
        ).classes("text-grey-7").mark("target-url-hint")
        ui.link(_("The services of Apprise and their URLs"), APPRISE_URLS, new_tab=True).mark("target-url-link")
        self.error_label = ui.label().classes("text-negative").mark("target-error")
        self.error_label.set_visibility(False)
        with ui.row().classes("gap-2"):
            self.save_button = ui.button(_("Save"), icon="save", on_click=self.save).mark("target-save")
            ui.button(_("Cancel"), on_click=self.close).props("flat").mark("target-cancel")

    def show_error(self, text: str | None) -> None:
        self.error_label.set_text(text or "")
        self.error_label.set_visibility(bool(text))

    async def save(self) -> None:
        if self.saving:
            return
        name = (self.name_input.value or "").strip()
        url = (self.url_input.value or "").strip()
        if not name:
            self.show_error(_("Enter a name."))
            return
        if not url:
            self.show_error(_("Enter the Apprise URL of the target."))
            return
        # The stored URL stays as it is when the field still holds it, so a change of the URL on another page stays.
        new_url = None if url == self.url else url
        self.saving = True
        self.save_button.disable()
        try:
            if new_url and not await asyncio.to_thread(Notifier.is_valid_url, new_url):
                # The text never repeats the URL, because the URL holds a token.
                self.show_error(_("Apprise does not accept this URL. The link below lists the URL of each service."))
                return
            async with self.writes:
                outcome = await asyncio.to_thread(
                    save_target, self.services.sessions, self.target.id if self.target else None, name, new_url
                )
        finally:
            self.saving = False
            self.save_button.enable()
        if outcome is SaveOutcome.NAME_TAKEN:
            self.show_error(_("Another target has this name."))
            return
        if outcome is SaveOutcome.GONE:
            ui.notify(_("The target does not exist any more."), type="warning")
        else:
            LOG.info("Saved the notification target %r", name)
            ui.notify(_("The target {name} is saved.").format(name=name), type="positive")
        self.close()
        await self.done()


@dataclass
class TargetsState:
    targets: list[TargetRecord]
    # The choices of each target as the controls of its card show them, by the id of the target.
    edited: dict[int, TargetChoices]
    # The ids of the targets whose test notification is on its way.
    sending: set[int] = field(default_factory=set)
    # The "Send a test" button of each card of this moment.
    test_buttons: dict[int, ui.button] = field(default_factory=dict)

    def changes(self) -> list[tuple[TargetRecord, TargetChoices]]:
        """Return each target whose choices in its card differ from its stored choices, with those choices."""
        return [(target, self.edited[target.id]) for target in self.targets if self.edited[target.id] != target.choices]


def _event_label(event: Event) -> str:
    """Return the label of the checkbox of ``event``: the name of the event and the level that it has."""
    # Translators: The label of the checkbox of an event, for example "Run summary · Success".
    return _("{event} · {level}").format(event=_(EVENT_LABELS[event]), level=_(LEVEL_LABELS[EVENT_LEVELS[event]]))


async def notifications_section(services: Services, draft: SettingsDraft) -> None:
    writes = draft.writes
    targets = await asyncio.to_thread(read_targets, services.sessions)
    state = TargetsState(targets, {target.id: target.choices for target in targets})

    async def reload() -> None:
        # The read waits for the writes of the page, so it sees each change that came before it.
        async with writes:
            targets = await asyncio.to_thread(read_targets, services.sessions)
            before = {target.id: target.choices for target in state.targets}
            state.edited = {
                target.id: keep_edits(before[target.id], state.edited[target.id], target.choices)
                if target.id in before and target.id in state.edited
                else target.choices
                for target in targets
            }
            state.targets = targets
        await targets_view.refresh()
        draft.update()

    def choose(target_id: int, **choices: object) -> None:
        """Change the choices of a target in the draft only."""
        edited = state.edited.get(target_id)
        if edited is None:
            # The card of a target that went, before the block shows without it.
            return
        state.edited[target_id] = dataclasses.replace(edited, **choices)
        draft.update()

    def choose_event(target_id: int, event: Event, on: bool) -> None:
        edited = state.edited.get(target_id)
        if edited is not None:
            choose(target_id, events=(edited.events | {event}) if on else (edited.events - {event}))

    def prepare() -> Write:
        changes = state.changes()
        gone: list[TargetRecord] = []

        async def run() -> None:
            for target, edited in changes:
                if not await asyncio.to_thread(save_choices, services.sessions, target.id, target.choices, edited):
                    gone.append(target)
                    continue
                saved = dataclasses.replace(
                    target, events=edited.events, min_level=edited.min_level, enabled=edited.enabled
                )
                state.targets = [saved if other.id == target.id else other for other in state.targets]
                LOG.info("Saved the events, the lowest level and the switch of the notification target %r", target.name)

        async def after() -> None:
            if gone:
                ui.notify(_("The target does not exist any more."), type="warning")
            # A choice that this page did not change can hold the value of another page, and its card shows it.
            await reload()

        return Write(run, after=after)

    def discard() -> None:
        state.edited = {target.id: target.choices for target in state.targets}
        targets_view.refresh()

    def show_sending(target_id: int) -> None:
        button = state.test_buttons.get(target_id)
        if button is None or button.is_deleted:
            return
        if target_id in state.sending:
            button.props("loading")
            button.disable()
        else:
            button.props(remove="loading")
            button.enable()

    async def send_test(target_id: int) -> None:
        if target_id in state.sending:
            return
        state.sending.add(target_id)
        show_sending(target_id)
        try:
            target = await asyncio.to_thread(load_target, services.sessions, target_id)
            # Apprise waits up to a minute for a service, so the send runs in a thread.
            sent = target is not None and await asyncio.to_thread(Notifier.send_test, target)
        finally:
            state.sending.discard(target_id)
            show_sending(target_id)
        if target is None:
            ui.notify(_("The target does not exist any more."), type="warning")
            await reload()
        elif sent:
            ui.notify(_("The test notification went to {name}.").format(name=target.name), type="positive")
        else:
            ui.notify(
                _("The test notification to {name} failed. The log of the service names the cause.").format(
                    name=target.name
                ),
                type="negative",
            )

    async def ask_delete(target: TargetRecord) -> None:
        confirmed = await confirm(
            _('Delete the target "{name}"?').format(name=target.name),
            _("Delete"),
            marker="target-delete",
            color="negative",
        )
        if not confirmed:
            return
        async with writes:
            await asyncio.to_thread(delete_target, services.sessions, target.id)
        LOG.info("Deleted the notification target %r", target.name)
        ui.notify(_("The target {name} is deleted.").format(name=target.name), type="positive")
        await reload()

    def close_editor() -> None:
        editor_dialog.close()
        editor_view.refresh(None)

    async def open_editor(target: TargetRecord | None) -> None:
        url = None
        if target is not None:
            stored = await asyncio.to_thread(load_target, services.sessions, target.id)
            if stored is None:
                ui.notify(_("The target does not exist any more."), type="warning")
                await reload()
                return
            # Another page can have changed the name.
            target, url = dataclasses.replace(target, name=stored.name), stored.url
        # The form is ready before the dialog opens.
        await editor_view.refresh(TargetEditor(services, writes, target, url, close_editor, reload))
        editor_dialog.open()

    @ui.refreshable
    def editor_view(editor: TargetEditor | None) -> None:
        if editor is None:
            return
        with ui.card().classes("w-full max-w-lg gap-2").mark("target-editor"):
            editor.build()

    def target_card(target: TargetRecord) -> None:
        marker = f"target-{target.id}"
        choices = state.edited[target.id]
        with ui.card().props("flat bordered").classes("w-full gap-2").mark(marker):
            # Each row wraps on a narrow screen, so no control goes past the right edge.
            with ui.row().classes("w-full items-center gap-x-4 gap-y-2"):
                ui.label(target.name).classes("text-subtitle1 text-weight-medium").mark(f"{marker}-name")
                ui.label(target.url_scheme).classes("font-mono text-grey-7").mark(f"{marker}-url")
                ui.space()
                ui.select(
                    {level: _(label) for level, label in LEVEL_LABELS.items()},
                    label=_("Lowest level"),
                    value=choices.min_level,
                    on_change=lambda change_event, target_id=target.id: choose(
                        target_id, min_level=Level(change_event.value)
                    ),
                ).props("outlined dense options-dense").classes("w-36").mark(f"{marker}-level")
                ui.switch(
                    _("Enabled"),
                    value=choices.enabled,
                    on_change=lambda change_event, target_id=target.id: choose(
                        target_id, enabled=bool(change_event.value)
                    ),
                ).props("dense").mark(f"{marker}-enabled")
                buttons_row(target, marker)
            with ui.row().classes("w-full gap-x-6 gap-y-1").mark(f"{marker}-events"):
                for event in Event:
                    ui.checkbox(
                        _event_label(event),
                        value=event in choices.events,
                        on_change=lambda change_event, target_id=target.id, item=event: choose_event(
                            target_id, item, bool(change_event.value)
                        ),
                    ).props("dense").mark(f"{marker}-event-{event.value}")

    def buttons_row(target: TargetRecord, marker: str) -> None:
        with ui.row().classes("no-wrap items-center gap-1"):
            state.test_buttons[target.id] = (
                ui.button(_("Send a test"), icon="send", on_click=lambda target_id=target.id: send_test(target_id))
                .props("flat dense no-caps no-wrap")
                .mark(f"{marker}-test")
            )
            show_sending(target.id)
            ui.button(icon="edit", on_click=lambda record=target: open_editor(record)).props(
                "flat round dense"
            ).tooltip(_("Change the name or the URL")).mark(f"{marker}-edit")
            ui.button(icon="delete", on_click=lambda record=target: ask_delete(record)).props(
                "flat round dense color=negative"
            ).tooltip(_("Delete the target")).mark(f"{marker}-delete")

    @ui.refreshable
    def targets_view() -> None:
        state.test_buttons.clear()
        if not state.targets:
            ui.label(_("No notification target yet. The button Add a target creates one.")).mark("no-targets")
            return
        with ui.column().classes("w-full gap-2").mark("targets-list"):
            for target in state.targets:
                target_card(target)

    with ui.card().classes("w-full").mark("targets-card"):
        ui.label(_("Notifications")).classes("text-h6")
        ui.label(
            _(
                "A target gets an event when the target is on, the event is checked, and the level of the event is at "
                "least the lowest level of the target. Each target shows only the start of its URL, because a URL "
                "holds the token of its service."
            )
        ).classes("text-grey-7")
        targets_view()
        ui.button(_("Add a target"), icon="add", on_click=lambda: open_editor(None)).mark("target-add")

    # A user can close the form only with its buttons, so a click outside it does not lose an edit.
    with ui.dialog().props("persistent").mark("target-dialog") as editor_dialog:
        editor_view(None)
    draft.add(DraftPart(changed=lambda: bool(state.changes()), prepare=prepare, discard=discard))
