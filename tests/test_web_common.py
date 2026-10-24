from datetime import UTC, datetime
from unittest.mock import Mock, call
from zoneinfo import ZoneInfo

import pytest
from nicegui import ui
from ui_support import client_of, element_of, elements_of, eventually, store, text_of

from youtube_automanager.db.models import Schedule, ScheduleKind
from youtube_automanager.i18n import N_
from youtube_automanager.web.ui import layout
from youtube_automanager.web.ui.common import (
    ResultLabel,
    busy,
    column,
    confirm,
    format_time,
    read_timezone,
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
    button = Mock(is_deleted=False)

    with busy(button):
        assert button.mock_calls == [call.props("loading"), call.disable()]

    assert button.mock_calls[2:] == [call.props(remove="loading"), call.enable()]


def test_busy_button_is_back_to_normal_when_the_block_fails():
    button = Mock(is_deleted=False)

    with pytest.raises(RuntimeError), busy(button):
        raise RuntimeError

    assert button.mock_calls[2:] == [call.props(remove="loading"), call.enable()]


def test_busy_button_that_the_block_deleted_stays_as_it_is():
    button = Mock(is_deleted=False)

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
