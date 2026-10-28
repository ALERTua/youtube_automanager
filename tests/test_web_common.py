import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock, Mock, call
from zoneinfo import ZoneInfo

import pytest
from nicegui import ui
from ui_support import client_of, element_of, elements_of, eventually, store, text_of

from youtube_automanager.db.models import Schedule, ScheduleKind
from youtube_automanager.i18n import N_
from youtube_automanager.web.ui import layout
from youtube_automanager.web.ui.common import (
    ResultLabel,
    actions_column,
    add_row_button,
    busy,
    column,
    confirm,
    focus_when_drawn,
    format_time,
    icon_button,
    keyboard_sort_template,
    named,
    notify_in,
    read_timezone,
    row_button_selector,
    row_button_template,
    set_loading,
    stored_timezone,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]


class HelperPages:
    """Two pages that use the helpers: ``/ask`` asks a question with ``confirm``, and ``/result`` shows a ResultLabel.

    ``options`` goes to ``confirm`` at each click, and ``answers`` keeps what ``confirm`` returned.
    """

    def __init__(self) -> None:
        self.options: dict = {}
        self.answers: list[bool] = []
        # ``/busy`` keeps its button busy until this event is set.
        self.release = asyncio.Event()
        # The ids that the button of a row of ``/rows`` sent.
        self.pressed: list = []


@pytest.fixture
def helper_pages(nicegui_globals, monkeypatch):
    """Register the two pages. A test that uses the fixture asks for it before ``user``, which mounts the pages."""
    monkeypatch.setattr(layout, "_PAGES", dict(layout._PAGES))
    pages = HelperPages()

    @layout.register_page("/ask", label=N_("Ask"), icon="help")
    async def ask_page(services):
        async def ask() -> None:
            pages.answers.append(await confirm("Really?", "Do it", marker="ask", **pages.options))

        ui.button("Ask", on_click=ask).mark("ask-open")

    @layout.register_page("/result", label=N_("Result"), icon="help")
    async def result_page(services):
        label = ResultLabel().mark("result")
        ui.button("Problem", on_click=lambda: label.problem("It failed")).mark("result-problem")
        ui.button("Ok", on_click=lambda: label.ok("It worked")).mark("result-ok")

    @layout.register_page("/busy", label=N_("Busy"), icon="help")
    async def busy_page(services):
        async def work() -> None:
            with busy(button):
                await pages.release.wait()

        async def load_icon() -> None:
            set_loading(icon, True)

        button = ui.button("Work", on_click=work).mark("busy-work")
        icon = icon_button("delete", "Remove it", lambda: None).mark("busy-icon")
        ui.button("Load the icon", on_click=load_icon).mark("busy-load-icon")

    @layout.register_page("/rows", label=N_("Rows"), icon="help")
    async def rows_page(services):
        table = ui.table(
            columns=[column("name", "Name"), actions_column()], rows=[{"id": 7, "name": 'Say "hi" <b>'}], row_key="id"
        ).mark("rows")
        add_row_button(table, lambda event: pages.pressed.append(event.args), icon="edit", label='Edit "it" <now>')

    return pages


def test_column_left_aligns_the_field_with_the_name_of_the_column():
    assert column("title", "Channel") == {"name": "title", "label": "Channel", "field": "title", "align": "left"}


def test_column_can_be_sortable():
    assert column("title", "Channel", sortable=True) == {
        "name": "title",
        "label": "Channel",
        "field": "title",
        "align": "left",
        "sortable": True,
    }


def test_time_zone_of_the_schedule_is_the_default_before_a_schedule_exists(sessions):
    assert read_timezone(sessions, "Europe/Kyiv") == "Europe/Kyiv"
    with sessions() as session:
        assert stored_timezone(session, "Europe/Kyiv") == "Europe/Kyiv"


def test_time_zone_of_the_schedule_is_the_stored_one(sessions):
    store(sessions, Schedule(enabled=True, kind=ScheduleKind.CRON, timezone="Asia/Tokyo"))

    assert read_timezone(sessions, "Europe/Kyiv") == "Asia/Tokyo"
    with sessions() as session:
        assert stored_timezone(session, "Europe/Kyiv") == "Asia/Tokyo"


def test_time_shows_in_the_zone_with_its_name():
    moment = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    assert format_time(moment, ZoneInfo("Europe/Kyiv")) == "2026-10-01 15:00 EEST"


def test_busy_button_loads_and_is_off_during_the_block_and_back_to_normal_after_it():
    button = Mock(is_deleted=False, text="")

    with busy(button):
        assert button.mock_calls == [call.props("loading"), call.disable()]

    assert button.mock_calls[2:] == [call.props(remove="loading"), call.enable()]


def test_busy_button_is_back_to_normal_when_the_block_fails():
    button = Mock(is_deleted=False, text="")

    with pytest.raises(RuntimeError), busy(button):
        raise RuntimeError

    assert button.mock_calls[2:] == [call.props(remove="loading"), call.enable()]


def test_busy_button_that_the_block_deleted_stays_as_it_is():
    button = Mock(is_deleted=False, text="")

    with busy(button):
        button.is_deleted = True

    assert button.mock_calls == [call.props("loading"), call.disable()]


async def ask(user, helper_pages, **options):
    """Open the page ``/ask``, give ``confirm`` the ``options``, press the button, and wait for the question."""
    helper_pages.options = options
    await user.open("/ask")
    user.find(marker="ask-open").click()
    await user.should_see(marker="ask-confirm")


async def test_confirm_returns_true_on_the_action_button_and_deletes_the_dialog(helper_pages, user):
    await ask(user, helper_pages)
    assert text_of(user, "ask-question") == "Really?"

    user.find(marker="ask-confirm").click()

    await eventually(lambda: helper_pages.answers, "The answer")
    assert helper_pages.answers == [True]
    assert not elements_of(user, "ask-dialog")


async def test_confirm_returns_false_on_cancel_and_deletes_the_dialog(helper_pages, user):
    await ask(user, helper_pages)

    user.find(marker="ask-cancel").click()

    await eventually(lambda: helper_pages.answers, "The answer")
    assert helper_pages.answers == [False]
    assert not elements_of(user, "ask-dialog")


async def test_confirm_returns_false_when_the_dialog_closes_without_a_button(helper_pages, user):
    await ask(user, helper_pages)
    dialog = element_of(user, "ask-dialog").parent_slot.parent

    # The same close as the Escape key or a click outside of the dialog.
    with client_of(user):
        dialog.close()

    await eventually(lambda: helper_pages.answers, "The answer")
    assert helper_pages.answers == [False]
    assert not elements_of(user, "ask-dialog")


async def test_confirm_shows_the_detail_under_the_question_when_it_is_given(helper_pages, user):
    await ask(user, helper_pages, detail="The history stays.")

    labels = [element.text for element in element_of(user, "ask-dialog").descendants() if isinstance(element, ui.label)]
    assert labels == ["Really?", "The history stays."]


async def test_confirm_shows_only_the_question_without_a_detail(helper_pages, user):
    await ask(user, helper_pages)

    labels = [element.text for element in element_of(user, "ask-dialog").descendants() if isinstance(element, ui.label)]
    assert labels == ["Really?"]


async def test_confirm_gives_the_action_button_the_color(helper_pages, user):
    await ask(user, helper_pages, color="negative")

    assert element_of(user, "ask-confirm").props["color"] == "negative"
    assert element_of(user, "ask-confirm").text == "Do it"


async def test_confirm_action_button_is_primary_by_default(helper_pages, user):
    await ask(user, helper_pages)

    assert element_of(user, "ask-confirm").props["color"] == "primary"


async def test_confirm_gives_the_card_the_classes(helper_pages, user):
    await ask(user, helper_pages, classes="max-w-lg")

    assert "max-w-lg" in element_of(user, "ask-dialog").classes


async def test_result_label_shows_a_problem_in_the_color_of_an_error_and_an_ok_without_it(helper_pages, user):
    await user.open("/result")
    label = element_of(user, "result")

    user.find(marker="result-problem").click()
    await eventually(lambda: label.text == "It failed", "The problem text")
    assert "text-negative" in label.classes

    user.find(marker="result-ok").click()
    await eventually(lambda: label.text == "It worked", "The ok text")
    assert "text-negative" not in label.classes


async def test_busy_button_keeps_its_name_for_a_screen_reader_while_it_loads(helper_pages, user):
    await user.open("/busy")
    button = element_of(user, "busy-work")
    assert "aria-label" not in button.props

    user.find(marker="busy-work").click()
    await eventually(lambda: button.props.get("loading"), "The loading button")
    # Quasar hides the text of a loading button, so the button needs its text as its name.
    assert button.props["aria-label"] == "Work"

    helper_pages.release.set()
    await eventually(lambda: not button.props.get("loading"), "The button after the work")
    assert "aria-label" not in button.props


async def test_a_loading_icon_button_keeps_its_own_name(helper_pages, user):
    await user.open("/busy")
    icon = element_of(user, "busy-icon")
    assert isinstance(icon, ui.button)

    user.find(marker="busy-load-icon").click()
    await eventually(lambda: icon.props.get("loading"), "The loading icon button")
    assert icon.props["aria-label"] == "Remove it"

    with client_of(user):
        set_loading(icon, False)
    assert icon.props["aria-label"] == "Remove it"


async def test_icon_button_has_the_label_as_its_tooltip_and_its_name(helper_pages, user):
    await user.open("/busy")
    icon = element_of(user, "busy-icon")

    assert icon.props["aria-label"] == "Remove it"
    assert icon.props["icon"] == "delete"
    # A round dense flat button is a compact icon button, not a filled rectangle.
    assert {"flat", "round", "dense"} <= set(icon.props)
    tooltips = [
        element.text
        for element in client_of(user).elements.values()
        if isinstance(element, ui.tooltip) and element.props.get("target") == f"#{icon.html_id}"
    ]
    assert tooltips == ["Remove it"]
    assert icon.text == ""


async def test_row_button_has_the_label_as_its_name_and_sends_the_id_of_its_row(helper_pages, user):
    await user.open("/rows")
    table = element_of(user, "rows")

    template = table.slots["body-cell-actions"].template or ""
    # The label goes into the page as text, so a quote or a tag in it cannot break the template.
    assert 'aria-label="Edit &quot;it&quot; &lt;now&gt;"' in template
    assert "<q-tooltip>Edit &quot;it&quot; &lt;now&gt;</q-tooltip>" in template
    # The click of the button does not reach the row, so the row does not act a second time.
    assert "@click.stop" in template
    assert [column["label"] for column in table.columns] == ["Name", "Actions"]

    user.find(marker="rows").trigger("rowButton", args=7)

    await eventually(lambda: helper_pages.pressed, "The press of the row button")
    assert helper_pages.pressed == [7]


def test_the_template_of_a_row_button_and_its_listener_agree_on_the_event_and_the_id():
    # The user simulation never draws a slot, so these literals are the only check of the pair that the browser runs.
    template = row_button_template("edit", "Edit")

    assert "@click.stop=\"() => $parent.$emit('rowButton', props.row.id)\"" in template
    assert 'icon="edit"' in template
    assert ':data-row-button="props.row.id"' in template
    assert template.count("props.row.") == 2


async def test_the_listener_of_a_row_button_sends_only_the_id(helper_pages, user):
    await user.open("/rows")
    table = element_of(user, "rows")

    listeners = [
        (listener.type, listener.js_handler)
        for listener in table._event_listeners.values()
        if listener.type == "rowButton"
    ]

    assert listeners == [("rowButton", "(id) => emit(id)")]


@pytest.mark.parametrize(
    "label",
    [
        "x {{ $parent.$emit('rowButton', 99) }} y",
        "{{constructor.constructor('alert(1)')()}}",
        "{ {{ } }}",
    ],
)
def test_a_label_with_a_vue_expression_cannot_run_in_the_template(label):
    template = row_button_template("edit", label)

    # Vue runs {{ ... }} in the text of a tooltip, so no brace of the label may stay in the template.
    assert "{" not in template
    # The label is in the template twice: as the name and as the tooltip.
    assert template.count("&#123;") == 2 * label.count("{")


def test_the_selector_of_a_row_button_is_scoped_to_a_table_on_request():
    assert row_button_selector(7) == '[data-row-button="7"]'
    assert row_button_selector("UCabc", "found") == '[data-row-scope="found"] [data-row-button="UCabc"]'


def test_the_move_of_the_focus_tries_the_selector_then_the_fallback_after_the_delay(monkeypatch):
    sent = []
    client = Mock(run_javascript=sent.append)

    focus_when_drawn(client, '[data-row-button="7"]', fallback="#c5", delay_ms=200)
    focus_when_drawn(client, "#c9")

    first, second = sent
    assert 'document.querySelector("[data-row-button=\\"7\\"]")' in first
    assert 'else if ("#c5") document.querySelector("#c5")?.focus();' in first
    assert "}, 200);" in first
    # Without a fallback and a delay, the browser tries once at once and gives up after about a second.
    assert "else if (null)" in second
    assert "}, 0);" in second
    assert "let tries = 20;" in second
    assert "setTimeout(go, 50)" in second


async def test_named_gives_a_field_its_name_for_a_screen_reader(helper_pages, user):
    await user.open("/busy")
    with client_of(user):
        field = named(ui.input(), "Condition")

    assert field.props["aria-label"] == "Condition"


def test_the_header_of_a_sortable_column_has_a_keyboard_path_and_tells_its_sort():
    # The user simulation never draws a slot, so these literals are the only check of the header that the browser runs.
    template = keyboard_sort_template("rules")

    assert '<q-th :props="props"' in template
    # Only a sortable header is a stop of the Tab key, and Enter and Space sort as a click does.
    assert ':tabindex="props.col.sortable ? 0 : null"' in template
    assert '@keydown.enter.prevent="props.col.sortable && props.sort(props.col)"' in template
    assert '@keydown.space.prevent="props.col.sortable && props.sort(props.col)"' in template
    # Quasar marks the sorted column in __thClass with " sorted" and "sort-desc".
    assert (
        ":aria-sort=\"props.col.sortable ? (props.col.__thClass.includes(' sorted') ? "
        "(props.col.__thClass.includes('sort-desc') ? 'descending' : 'ascending') : 'none') : null\""
    ) in template
    assert ":data-focus-key=\"props.col.sortable ? 'rules-sort-' + props.col.name : null\"" in template
    assert ">{{ props.col.label }}</q-th>" in template


@pytest.mark.parametrize("key", ["", "a b", "x'+alert(1)+'", "{{ 1 }}"])
def test_the_key_of_a_keyboard_sort_holds_only_letters_digits_and_dashes(key):
    with pytest.raises(ValueError, match="The key of a table must hold only letters, digits and '-'"):
        keyboard_sort_template(key)


def test_notify_in_shows_the_message_under_the_client_and_does_nothing_for_a_client_that_is_gone(monkeypatch):
    shown = []
    monkeypatch.setattr(ui, "notify", lambda message, **options: shown.append((message, options)))
    client = MagicMock(is_deleted=False)

    notify_in(client, "Saved.", type="positive")

    assert shown == [("Saved.", {"type": "positive"})]
    client.__enter__.assert_called_once()
    client.is_deleted = True

    notify_in(client, "Late.")

    assert shown == [("Saved.", {"type": "positive"})]
