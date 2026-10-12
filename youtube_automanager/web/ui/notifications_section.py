"""The notification targets block of the settings page: the table of the targets and the form that edits a target.

The web UI has no login, so the block never shows a stored target URL. A target shows only the scheme of its URL, and
the form keeps the stored URL while its URL field stays empty. No log line of the block holds a target URL.

A test notification can take up to a minute, so the block sends it in a thread. The block reads and writes the database
in a thread, never on the event loop. Each write holds the lock of the page, so one write runs at a time.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from nicegui import ui

from youtube_automanager.i18n import _
from youtube_automanager.notify import EVENT_LEVELS, Event, Level, Notifier
from youtube_automanager.web.services import Services
from youtube_automanager.web.ui.notification_targets import (
    APPRISE_URLS,
    EVENT_LABELS,
    LEVEL_LABELS,
    SaveOutcome,
    TargetRecord,
    delete_target,
    load_target,
    read_targets,
    save_target,
    set_enabled,
    set_event,
    set_min_level,
)

LOG = logging.getLogger(__name__)


class TargetEditor:
    """The form that creates a target, or changes the name and the URL of a target. ``done`` runs after a save."""

    def __init__(
        self,
        services: Services,
        writes: asyncio.Lock,
        target: TargetRecord | None,
        close: Callable[[], None],
        done: Callable[[], Awaitable[None]],
    ) -> None:
        self.services = services
        self.writes = writes
        self.target = target
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
        # A password field, so the token of a typed URL does not show on the screen.
        self.url_input = (
            ui.input(_("Apprise URL"), password=True, password_toggle_button=True)
            .props("outlined dense autocomplete=new-password")
            .classes("w-full")
            .mark("target-url")
        )
        if target:
            hint = _(
                "The stored URL stays when this field is empty. It starts with {scheme}. A URL in this field "
                "replaces it."
            ).format(scheme=target.url_scheme)
        else:
            hint = _("The URL names the service and its token, for example tgram://bottoken/ChatID for Telegram.")
        ui.label(hint).classes("text-grey-7").mark("target-url-hint")
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
        if self.target is None and not url:
            self.show_error(_("Enter the Apprise URL of the target."))
            return
        self.saving = True
        self.save_button.disable()
        try:
            if url and not await asyncio.to_thread(Notifier.is_valid_url, url):
                # The text never repeats the URL, because the URL holds a token.
                self.show_error(_("Apprise does not accept this URL. The link below lists the URL of each service."))
                return
            async with self.writes:
                outcome = await asyncio.to_thread(
                    save_target, self.services.sessions, self.target.id if self.target else None, name, url or None
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
    # The ids of the targets whose test notification is on its way.
    sending: set[int] = field(default_factory=set)
    # The "Send a test" button of each target in the table of this moment.
    test_buttons: dict[int, ui.button] = field(default_factory=dict)


def _header(text: str) -> None:
    with ui.element("th").classes("text-left"):
        ui.label(text)


async def notifications_section(services: Services, writes: asyncio.Lock) -> None:
    state = TargetsState(await asyncio.to_thread(read_targets, services.sessions))

    async def reload() -> None:
        # The read waits for the writes of the page, so it sees each change that came before it.
        async with writes:
            state.targets = await asyncio.to_thread(read_targets, services.sessions)
        await targets_view.refresh()

    async def change(write: Callable[..., bool], target_id: int, *values: object) -> None:
        """Write one change of the table at once. A target that went shows a warning, and the table reads again."""
        async with writes:
            done = await asyncio.to_thread(write, services.sessions, target_id, *values)
        if not done:
            ui.notify(_("The target does not exist any more."), type="warning")
            await reload()

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
        with ui.dialog() as confirm, ui.card().mark("target-delete-dialog"):
            ui.label(_('Delete the target "{name}"?').format(name=target.name)).mark("target-delete-question")
            with ui.row().classes("gap-2"):
                ui.button(_("Delete"), color="negative", on_click=lambda: confirm.submit(True)).mark(
                    "target-delete-confirm"
                )
                ui.button(_("Cancel"), on_click=lambda: confirm.submit(False)).props("flat").mark(
                    "target-delete-cancel"
                )
        confirmed = await confirm
        if not confirm.is_deleted:
            confirm.delete()
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
        # The form is ready before the dialog opens.
        await editor_view.refresh(TargetEditor(services, writes, target, close_editor, reload))
        editor_dialog.open()

    @ui.refreshable
    def editor_view(editor: TargetEditor | None) -> None:
        if editor is None:
            return
        with ui.card().classes("w-full max-w-lg gap-2").mark("target-editor"):
            editor.build()

    def target_row(target: TargetRecord) -> None:
        marker = f"target-{target.id}"
        with ui.element("tr").mark(marker):
            with ui.element("td"):
                ui.label(target.name).mark(f"{marker}-name")
            with ui.element("td"):
                ui.label(target.url_scheme).classes("font-mono").mark(f"{marker}-url")
            for event in Event:
                with ui.element("td").classes("text-center"):
                    ui.checkbox(
                        value=event in target.events,
                        on_change=lambda change_event, target_id=target.id, item=event: change(
                            set_event, target_id, item, bool(change_event.value)
                        ),
                    ).props("dense").mark(f"{marker}-event-{event.value}")
            with ui.element("td"):
                ui.select(
                    {level: _(label) for level, label in LEVEL_LABELS.items()},
                    value=target.min_level,
                    on_change=lambda change_event, target_id=target.id: change(
                        set_min_level, target_id, Level(change_event.value)
                    ),
                ).props("outlined dense options-dense").classes("w-32").mark(f"{marker}-level")
            with ui.element("td"):
                ui.switch(
                    value=target.enabled,
                    on_change=lambda change_event, target_id=target.id: change(
                        set_enabled, target_id, bool(change_event.value)
                    ),
                ).props("dense").mark(f"{marker}-enabled")
            with ui.element("td"), ui.row().classes("no-wrap items-center gap-1"):
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
        # A wide table scrolls inside its card, and the page keeps its width.
        with (
            ui.element("div").classes("w-full overflow-x-auto"),
            ui.element("q-markup-table").props("flat bordered dense").mark("targets-table"),
        ):
            with ui.element("thead"), ui.element("tr"):
                _header(_("Name"))
                _header(_("URL"))
                for event in Event:
                    # A Quasar table keeps each cell on one line. The name of an event may take two lines.
                    with ui.element("th").classes("text-center").style("white-space: normal; min-width: 6rem"):
                        ui.label(_(EVENT_LABELS[event]))
                        ui.label(_(LEVEL_LABELS[EVENT_LEVELS[event]])).classes("text-caption text-grey-7")
                _header(_("Lowest level"))
                _header(_("Enabled"))
                _header("")
            with ui.element("tbody"):
                for target in state.targets:
                    target_row(target)

    with ui.card().classes("w-full").mark("targets-card"):
        ui.label(_("Notifications")).classes("text-h6")
        ui.label(
            _(
                "A target gets an event when the target is on, the event is checked, and the level of the event is at "
                "least the lowest level of the target. The table shows only the start of each URL, because a URL holds "
                "the token of its service."
            )
        ).classes("text-grey-7")
        targets_view()
        ui.button(_("Add a target"), icon="add", on_click=lambda: open_editor(None)).mark("target-add")

    # A user can close the form only with its buttons, so a click outside it does not lose an edit.
    with ui.dialog().props("persistent").mark("target-dialog") as editor_dialog:
        editor_view(None)
