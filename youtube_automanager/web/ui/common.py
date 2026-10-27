"""The helpers that two or more pages share: the table columns, the button of a table row, the icon button, the confirm
dialog, the busy button, the button "Run now", the result label, the time zone of the schedule, the format of a time,
and the labels of the run statuses, the session states and the run triggers.
"""

import html
import json
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from nicegui import ui
from nicegui.client import Client
from nicegui.events import GenericEventArguments

from youtube_automanager.db.models import RunStatus, RunTrigger, SessionState
from youtube_automanager.i18n import N_, _

# The pages import the time zone readers from here, and the scheduler owns them.
from youtube_automanager.scheduler import RunNowOutcome, read_timezone, stored_timezone  # noqa: F401
from youtube_automanager.web.services import Services

TIME_FORMAT = "%Y-%m-%d %H:%M %Z"
# The name of the table column that holds the button of each row.
ROW_BUTTON_COLUMN = "actions"
# The event that the cell of a row button emits, and the code that the browser runs to send only the id of the row.
ROW_BUTTON_EVENT = "rowButton"
# The attribute of a table that tells a page with more than one table of rows which table is which.
ROW_SCOPE_ATTRIBUTE = "data-row-scope"
ROW_BUTTON_JS_HANDLER = "(id) => emit(id)"
# The attribute of a control that keeps the focus when a redraw replaces it. See ``focus_key``.
FOCUS_KEY_ATTRIBUTE = "data-focus-key"
# The most digits of a whole number in an address. Each number of 18 digits fits into the 64 bits of a database id.
MAX_NUMBER_DIGITS = 18
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


def focus_key[T: ui.element](element: T, key: str) -> T:
    """Give ``element`` the key ``key``, so it keeps the focus when a redraw replaces it.

    A refresh of a card can come from the server at any time, for example at a progress of a run, and the focus then
    falls to the page body. The head script remembers the control with a key that has the focus. When a redraw removes
    it and the focus is on the body, the script focuses the new control with the same key. A focus on an ``input``
    inside the control goes back to the ``input`` of the new control, and any other focus inside it goes to the control.
    Each key is unique on its page.
    """
    element.props[FOCUS_KEY_ATTRIBUTE] = key
    return element


def remember_focus(client: Client) -> None:
    """Let the browser keep the element that has the focus now, as the start of the next ``focus_when_drawn``.

    A page calls this before a read that takes time. A user who moves the focus during the read then keeps it.
    """
    client.run_javascript("window.__focusStart = document.activeElement;")


def focus_when_drawn(client: Client, selector: str, *, fallback: str | None = None, delay_ms: int = 0) -> None:
    """Move the keyboard focus to the element that matches ``selector``, as soon as the browser has drawn it.

    A rebuild of a table or a card removes the element that had the focus, and the focus goes to the top of the page.
    The page calls this after such a rebuild. The browser tries for about a second. When no element matches
    ``selector``, the focus goes to the element that matches ``fallback``. ``delay_ms`` is a wait before the first try,
    for a dialog that is still closing.

    ``client`` is the client of the page. A handler can run after the element that started it is gone, and then the
    context of the handler has no client any more.

    The browser gives up at once when the user has moved the focus. The focus is free when it is on the page body,
    on the element that had it at the start, on the button of the same row as the start, on an element that left the
    page, or inside the dialog that held the start. A focus inside another dialog, for example a dialog that the user
    opened meanwhile, is not free. The start is the element of the last ``remember_focus``, or the element that has
    the focus at the call.
    """
    client.run_javascript(
        f"""
(() => {{
  const start = window.__focusStart || document.activeElement;
  window.__focusStart = null;
  const sameRow = (a, b) => a.hasAttribute('data-row-button') && !!b?.hasAttribute?.('data-row-button')
    && a.getAttribute('data-row-button') === b.getAttribute('data-row-button');
  const free = () => {{
    const active = document.activeElement;
    if (active === document.body || active === start || !active.isConnected || sameRow(active, start)) return true;
    const dialog = active.closest('.q-dialog');
    return !!dialog && (dialog.contains(start) || !dialog.isConnected);
  }};
  setTimeout(() => {{
    let tries = 20;
    const go = () => {{
      if (!free()) return;
      const element = document.querySelector({json.dumps(selector)});
      if (element) element.focus();
      else if (--tries > 0) setTimeout(go, 50);
      else if ({json.dumps(fallback)}) document.querySelector({json.dumps(fallback)})?.focus();
    }};
    go();
  }}, {delay_ms});
}})();
"""
    )


def safe_json(value: Any) -> str:
    """Return ``value`` as JSON that is safe inside a ``<script>`` element: no ``<`` stays, so no text can end the element."""
    return json.dumps(value).replace("<", "\\" + "u003c")


def accessibility_script() -> str:
    """Return the ``<script>`` that each page has in its head, for two things that no prop of NiceGUI can do.

    First, it names the controls inside Quasar components. The table has a select for the rows of a page, and the file
    upload has buttons that show only an icon. The script sets ``aria-label`` on them as soon as the browser draws
    them. The select gets the text before it, which is a text of Quasar in the language of the page. A button of the
    upload gets the label that belongs to its icon.

    Second, it keeps the focus on a row button. A server update that touches a table makes Vue replace the cells of its
    rows, and the focus falls to the page body. The script remembers the row button that has the focus. When a redraw
    removes it and the focus is on the body, the script focuses the new button with the same row id in the same table.
    In the same way, it keeps the focus on a control with a key of ``focus_key`` that a refresh of its card replaces.
    A user who moved the focus, or who clicked on the page, keeps what they chose.
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
  const names = {safe_json(buttons)};
  let row = null;
  let keyed = null;
  document.addEventListener('focusin', (event) => {{
    const button = event.target.closest?.('[data-row-button]');
    row = button && {{node: button, id: button.getAttribute('data-row-button'), scope: button.closest('[data-row-scope]')?.getAttribute('data-row-scope')}};
    const control = !button && event.target.closest?.('[{FOCUS_KEY_ATTRIBUTE}]');
    keyed = control ? {{node: event.target, key: control.getAttribute('{FOCUS_KEY_ATTRIBUTE}'), inner: event.target !== control && event.target.matches('input') ? 'input' : null}} : null;
  }});
  document.addEventListener('pointerdown', () => {{ row = null; keyed = null; }});
  const keepKeyFocus = () => {{
    if (!keyed || keyed.node.isConnected || document.activeElement !== document.body) return;
    const control = document.querySelector(`[{FOCUS_KEY_ATTRIBUTE}="${{CSS.escape(keyed.key)}}"]`);
    const again = control && (keyed.inner ? control.querySelector(keyed.inner) : control);
    if (!again) return;
    keyed.node = again;
    again.focus();
  }};
  const keepRowFocus = () => {{
    if (!row || row.node.isConnected || document.activeElement !== document.body) return;
    const prefix = row.scope ? `[data-row-scope="${{CSS.escape(row.scope)}}"] ` : '';
    const again = [...document.querySelectorAll(prefix + '[data-row-button]')].find((button) => button.getAttribute('data-row-button') === row.id);
    if (!again) return;
    row.node = again;
    again.focus();
  }};
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
  new MutationObserver(() => {{ name(); keepRowFocus(); keepKeyFocus(); }}).observe(document.documentElement, {{childList: true, subtree: true}});
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
    cancel_label: str | None = None,
) -> bool:
    """Ask ``question`` in a dialog and return True only when the user presses the ``action`` button.

    The dialog, the question and the two buttons get the markers ``{marker}-dialog``, ``{marker}-question``,
    ``{marker}-confirm`` and ``{marker}-cancel``. ``detail`` is a second line under the question, ``color`` is the color
    of the ``action`` button, and ``classes`` go to the card. ``cancel_label`` is the text of the second button, "Cancel"
    when it is not given. A close of the dialog without a button returns False.
    """
    with ui.dialog() as dialog, ui.card().classes(classes).mark(f"{marker}-dialog"):
        ui.label(question).mark(f"{marker}-question")
        if detail is not None:
            ui.label(detail).classes("text-grey-7")
        with ui.row().classes("gap-2"):
            ui.button(action, color=color, on_click=lambda: dialog.submit(True)).mark(f"{marker}-confirm")
            ui.button(cancel_label or _("Cancel"), on_click=lambda: dialog.submit(False)).props("flat").mark(
                f"{marker}-cancel"
            )
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


async def run_now(services: Services) -> None:
    """Start a manual run and tell the user whether it started. The Scheduler page and the Dashboard share it."""
    result = await services.scheduler.run_now()
    if result.outcome is RunNowOutcome.STARTED:
        ui.notify(_("Run {id} started.").format(id=result.run_id), type="positive")
    else:
        ui.notify(_("A run is already active, so no new run started."), type="warning")


def run_now_button(services: Services, marker: str) -> ui.button:
    """Return the button "Run now" with the marker ``marker``.

    The button is busy while the start runs, so a double click starts one run.
    """

    async def press() -> None:
        with busy(button):
            await run_now(services)

    button = ui.button(_("Run now"), icon="bolt", on_click=press).mark(marker)
    return button


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


def whole_number(text: str | None) -> int | None:
    """Return the whole number of ``text``, a part of an address, or None when the text is not one, for example "abc".

    Only the ASCII digits count, so a text such as "1e3" or "-1" is not a number here. A number of more than
    ``MAX_NUMBER_DIGITS`` digits is not one either: a database id has 64 bits, and SQLite refuses a larger Python int.
    """
    if not text or len(text) > MAX_NUMBER_DIGITS or not (text.isascii() and text.isdigit()):
        return None
    return int(text)
