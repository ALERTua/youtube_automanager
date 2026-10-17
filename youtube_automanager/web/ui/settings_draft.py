"""The draft of the settings page, with one Save and one Discard for each setting that the page edits.

A control of a setting changes only the draft. The two buttons float at the bottom right of the page while a part of
the draft differs from its stored values. Save first checks the form of each changed part. It writes nothing when a
form has an error, or when the user cancels a question of a part. Then it writes each changed part, and only the
changed parts, while the page holds its lock of the writes. Discard puts each control back to its stored value.

The stored values of a part are the values that the page read or saved last. So a save overwrites a change of the same
setting that another page made in between. The yt-dlp settings and the choices of a notification target write only the
fields that the page changed, and after the save they show the values of the database, also the values of another
page.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from nicegui import ui

from youtube_automanager.i18n import _


class FormError(ValueError):
    """A form that cannot be saved. The text tells the user why."""


@dataclass(frozen=True)
class Write:
    """The write of the changes of one part of the draft."""

    # Writes the changes, while the page holds its lock of the writes.
    run: Callable[[], Awaitable[None]]
    # Asks the user before the page writes anything. It returns False when the user cancels.
    confirm: Callable[[], Awaitable[bool]] | None = None
    # Runs after the writes of each part, without the lock, for example to read a view again.
    after: Callable[[], Awaitable[None]] | None = None


def _no_error(text: str | None) -> None:
    """A part without a form has no error to show."""


@dataclass(frozen=True)
class DraftPart:
    """One part of the draft, for example the rotation switch or the table of the notification targets."""

    # Returns True when a control of the part differs from its stored value.
    changed: Callable[[], bool]
    # Reads the controls and returns the write of their changes. It raises FormError when the form cannot be saved.
    prepare: Callable[[], Write]
    # Puts each control back to its stored value.
    discard: Callable[[], None]
    # Shows the text of a form error, or hides it when the text is None.
    show_error: Callable[[str | None], None] = _no_error


class SettingsDraft:
    """The parts of the draft of one settings page, and their buttons. ``writes`` is the lock of the writes of the page.

    Each block of the page adds its part, and calls ``update`` after each change of a control. The page calls
    ``build`` after the last block.
    """

    def __init__(self, writes: asyncio.Lock) -> None:
        self.writes = writes
        self._parts: list[DraftPart] = []
        self._saving = False
        self._buttons: ui.page_sticky | None = None

    def add(self, part: DraftPart) -> None:
        self._parts.append(part)

    def build(self) -> None:
        # The space at the end lets the page scroll its last controls out from under the buttons.
        ui.element("div").classes("h-16")
        with (
            ui.page_sticky("bottom-right", 24, 24).mark("settings-draft") as self._buttons,
            ui.card().props("bordered").classes("p-2 shadow-lg"),
            ui.row().classes("gap-2 no-wrap"),
        ):
            self._save_button = ui.button(_("Save"), icon="save", on_click=self.save).mark("settings-save")
            self._discard_button = (
                ui.button(_("Discard"), icon="undo", on_click=self.discard).props("outline").mark("settings-discard")
            )
        self.update()

    def update(self) -> None:
        """Show the buttons while a part differs from its stored values. A part without a change shows no error."""
        changed = False
        for part in self._parts:
            if part.changed():
                changed = True
            else:
                part.show_error(None)
        if self._buttons is not None:
            self._buttons.set_visibility(changed or self._saving)

    def _show_saving(self, saving: bool) -> None:
        self._saving = saving
        self._save_button.set_enabled(not saving)
        self._discard_button.set_enabled(not saving)
        if saving:
            self._save_button.props("loading")
        else:
            self._save_button.props(remove="loading")
        self.update()

    async def save(self) -> None:
        if self._saving:
            return
        writes: list[Write] = []
        valid = True
        for part in self._parts:
            if not part.changed():
                part.show_error(None)
                continue
            try:
                writes.append(part.prepare())
            except FormError as error:
                part.show_error(str(error))
                valid = False
            else:
                part.show_error(None)
        if not valid or not writes:
            return
        self._show_saving(True)
        try:
            for write in writes:
                if write.confirm is not None and not await write.confirm():
                    return
            async with self.writes:
                for write in writes:
                    await write.run()
            for write in writes:
                if write.after is not None:
                    await write.after()
        finally:
            self._show_saving(False)
        ui.notify(_("The settings are saved."), type="positive")

    def discard(self) -> None:
        if self._saving:
            return
        for part in self._parts:
            part.discard()
            part.show_error(None)
        self.update()
