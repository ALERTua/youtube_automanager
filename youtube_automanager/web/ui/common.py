"""The helpers that two or more pages share: the table columns, the button of a table row, the icon button, the confirm
dialog, the busy button, the result label, the time zone of the schedule, the format of a time, and the labels of the
run statuses, the session states and the run triggers.
"""

import html
import json
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from nicegui import ui
from nicegui.events import GenericEventArguments

from youtube_automanager.db.models import RunStatus, RunTrigger, SessionState
from youtube_automanager.i18n import N_, _

# The pages import the time zone readers from here, and the scheduler owns them.
from youtube_automanager.scheduler import read_timezone, stored_timezone  # noqa: F401

TIME_FORMAT = "%Y-%m-%d %H:%M %Z"
# The name of the table column that holds the button of each row.
ROW_BUTTON_COLUMN = "actions"
# The event that the cell of a row button emits, and the code that the browser runs to send only the id of the row.
ROW_BUTTON_EVENT = "rowButton"
# The attribute of a table that tells a page with more than one table of rows which table is which.
ROW_SCOPE_ATTRIBUTE = "data-row-scope"
ROW_BUTTON_JS_HANDLER = "(id) => emit(id)"
# The wait before the focus moves back to a button, so a dialog that is closing does not take the focus again.
FOCUS_DELAY_MS = 200

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


def actions_column() -> dict:
    """Return the last column of a table whose rows have a button. ``add_row_button`` fills its cells."""
    return column(ROW_BUTTON_COLUMN, _("Actions"))


def add_row_button(
    table: ui.table,
    on_press: Callable[[GenericEventArguments], Any],
    *,
    icon: str,
    label: str,
    scope: str | None = None,
) -> None:
    """Put a button into the column ``actions_column`` of each row of ``table``.

    A click on a row does what ``on_press`` does, but a row has no keyboard path. So the button does the same for a
    keyboard. ``on_press`` gets the id of the row as ``event.args``, as the handler of a ``rowClick`` that sends the id
    does. The button has ``label`` as its tooltip and its name for a screen reader. Its click does not reach the row.
    A page with two tables of rows gives each table its own ``scope``, so ``row_button_selector`` finds the right one.
    """
    if scope is not None:
        table.props[ROW_SCOPE_ATTRIBUTE] = scope
    table.add_slot(f"body-cell-{ROW_BUTTON_COLUMN}", row_button_template(icon, label))
    table.on(ROW_BUTTON_EVENT, on_press, js_handler=ROW_BUTTON_JS_HANDLER)


def row_button_template(icon: str, label: str) -> str:
    """Return the Vue template of the cell of ``add_row_button``.

    A template of Vue runs ``{{ ... }}`` as an expression, also in the text of a tooltip. So the label goes in with each
    ``{`` as a character reference, and a label cannot run code.
    """
    text = html.escape(label).replace("{", "&#123;")
    return f"""
<q-td :props="props" auto-width>
  <q-btn flat round dense icon="{icon}" aria-label="{text}" :data-row-button="props.row.id" \
@click.stop="() => $parent.$emit('{ROW_BUTTON_EVENT}', props.row.id)">
    <q-tooltip>{text}</q-tooltip>
  </q-btn>
</q-td>
"""


def row_button_selector(row_id: int | str, scope: str | None = None) -> str:
    """Return the CSS selector of the button of the row with ``row_id``, in the table with ``scope`` when it is given."""
    selector = f"[data-row-button={json.dumps(str(row_id))}]"
    return selector if scope is None else f"[{ROW_SCOPE_ATTRIBUTE}={json.dumps(scope)}] {selector}"


def focus_when_drawn(selector: str, *, fallback: str | None = None, delay_ms: int = 0) -> None:
    """Move the keyboard focus to the element that matches ``selector``, as soon as the browser has drawn it.

    A rebuild of a table or a card removes the element that had the focus, and the focus goes to the top of the page.
    The page calls this after such a rebuild. The browser tries for about a second. When no element matches
    ``selector``, the focus goes to the element that matches ``fallback``. ``delay_ms`` is a wait before the first try,
    for a dialog that is still closing.
    """
    ui.run_javascript(
        f"""
setTimeout(() => {{
  let tries = 20;
  const go = () => {{
    const element = document.querySelector({json.dumps(selector)});
    if (element) element.focus();
    else if (--tries > 0) setTimeout(go, 50);
    else if ({json.dumps(fallback)}) document.querySelector({json.dumps(fallback)})?.focus();
  }};
  go();
}}, {delay_ms});
"""
    )


def quasar_names_script() -> str:
    """Return the ``<script>`` that names the controls inside Quasar components, which no prop of NiceGUI can name.

    The table has a select for the rows of a page, and the file upload has buttons that show only an icon. The script
    sets ``aria-label`` on them as soon as the browser draws them. The select gets the text before it, which is a text
    of Quasar in the language of the page. A button of the upload gets the label that belongs to its icon.
    """
    buttons = {
        "add_box": _("Add a file"),
        "clear_all": _("Remove all files"),
        "cloud_upload": _("Upload the files"),
        "clear": _("Stop the upload"),
        "delete": _("Remove the file"),
    }
    return f"""<script>
(() => {{
  const names = {json.dumps(buttons)};
  const name = () => {{
    document.querySelectorAll('.q-table__bottom .q-table__select input').forEach((input) => {{
      if (input.getAttribute('aria-label')) return;
      const text = input.closest('.q-table__select')?.previousElementSibling?.textContent.trim().replace(/:$/, '');
      if (text) input.setAttribute('aria-label', text);
    }});
    document.querySelectorAll('.q-uploader button, .q-uploader a[role=button]').forEach((button) => {{
      const label = names[button.querySelector('.q-icon')?.textContent.trim()];
      if (label && !button.getAttribute('aria-label')) button.setAttribute('aria-label', label);
    }});
  }};
  new MutationObserver(name).observe(document.documentElement, {{childList: true, subtree: true}});
}})();
</script>"""


def named[T: ui.element](element: T, label: str) -> T:
    """Give ``element``, a field without a visible label, the ``label`` as its name for a screen reader."""
    element.props["aria-label"] = label
    return element


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


def icon_button(icon: str, label: str, on_click: Callable[[], Any], *, props: str = "flat round dense") -> ui.button:
    """Return a button that shows only ``icon``. ``label`` is its tooltip and its name for a screen reader.

    ``label`` is the translated text. ``props`` are the Quasar props of the button.
    """
    button = ui.button(icon=icon, on_click=on_click).props(props).tooltip(label)
    button.props["aria-label"] = label
    return button


def set_loading(button: ui.button, loading: bool) -> None:
    """Turn the loading state of ``button`` on or off.

    Quasar hides the text of a loading button, so the button has no name for a screen reader. While it loads, the
    button gets its text as ``aria-label``. A button without a text, such as an icon button, keeps its own label.
    """
    if loading:
        button.props("loading")
        if button.text:
            button.props["aria-label"] = button.text
    else:
        button.props(remove="loading")
        if button.text:
            button.props.pop("aria-label", None)


@contextmanager
def busy(button: ui.button) -> Generator[None]:
    """Show the ``button`` as loading and disabled while the block runs. The button is back to normal after the block.

    The block can close the page, so a button that is deleted by then stays as it is.
    """
    set_loading(button, True)
    button.disable()
    try:
        yield
    finally:
        if not button.is_deleted:
            set_loading(button, False)
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
