"""The helpers that two or more pages share: the table columns, the confirm dialog, the busy button, the result label,
the time zone of the schedule, the format of a time, and the labels of the run statuses, the session states and the run
triggers.
"""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui

from youtube_automanager.db.models import RunStatus, RunTrigger, SessionState
from youtube_automanager.i18n import N_, _

# The pages import the time zone readers from here, and the scheduler owns them.
from youtube_automanager.scheduler import read_timezone, stored_timezone  # noqa: F401

TIME_FORMAT = "%Y-%m-%d %H:%M %Z"

SESSION_STATES = {
    SessionState.UNKNOWN: (N_("Unknown"), "grey"),
    SessionState.OK: (N_("OK"), "positive"),
    SessionState.EXPIRING: (N_("Expiring"), "warning"),
    SessionState.DEAD: (N_("Dead"), "negative"),
}
RUN_STATUSES = {
    RunStatus.RUNNING: (N_("In progress"), "info"),
    RunStatus.SUCCEEDED: (N_("Succeeded"), "positive"),
    RunStatus.FAILED: (N_("Failed"), "negative"),
}
RUN_TRIGGERS = {
    RunTrigger.SCHEDULE: N_("Schedule"),
    RunTrigger.MANUAL: N_("Manual"),
    RunTrigger.RULE_APPLY: N_("Rule apply"),
}


def column(name: str, label: str, *, sortable: bool = False) -> dict:
    """Return the definition of a column of a ``ui.table``. The cell shows the field with the same name."""
    definition = {"name": name, "label": label, "field": name, "align": "left"}
    if sortable:
        definition["sortable"] = True
    return definition


async def confirm(
    question: str,
    action: str,
    *,
    marker: str,
    detail: str | None = None,
    color: str = "primary",
    classes: str = "",
) -> bool:
    """Ask ``question`` in a dialog and return True only when the user presses the ``action`` button.

    The dialog, the question and the two buttons get the markers ``{marker}-dialog``, ``{marker}-question``,
    ``{marker}-confirm`` and ``{marker}-cancel``. ``detail`` is a second line under the question, ``color`` is the color
    of the ``action`` button, and ``classes`` go to the card. A close of the dialog without a button returns False.
    """
    with ui.dialog() as dialog, ui.card().classes(classes).mark(f"{marker}-dialog"):
        ui.label(question).mark(f"{marker}-question")
        if detail is not None:
            ui.label(detail).classes("text-grey-7")
        with ui.row().classes("gap-2"):
            ui.button(action, color=color, on_click=lambda: dialog.submit(True)).mark(f"{marker}-confirm")
            ui.button(_("Cancel"), on_click=lambda: dialog.submit(False)).props("flat").mark(f"{marker}-cancel")
    confirmed = await dialog
    if not dialog.is_deleted:
        dialog.delete()
    return confirmed is True


@contextmanager
def busy(button: ui.button) -> Generator[None]:
    """Show the ``button`` as loading and disabled while the block runs. The button is back to normal after the block.

    The block can close the page, so a button that is deleted by then stays as it is.
    """
    button.props("loading")
    button.disable()
    try:
        yield
    finally:
        if not button.is_deleted:
            button.props(remove="loading")
            button.enable()


class ResultLabel(ui.label):
    """A label for the result of an action. A problem shows in the color of an error."""

    def ok(self, text: str) -> None:
        self.classes(remove="text-negative")
        self.set_text(text)

    def problem(self, text: str) -> None:
        self.classes(add="text-negative")
        self.set_text(text)


def format_time(value: datetime, zone: ZoneInfo) -> str:
    return value.astimezone(zone).strftime(TIME_FORMAT)
