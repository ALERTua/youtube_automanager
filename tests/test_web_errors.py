import asyncio
import logging
from unittest.mock import MagicMock, Mock, PropertyMock

import pytest
from nicegui import app as nicegui_app
from nicegui import ui
from nicegui.slot import Slot
from ui_support import eventually, running_service

from youtube_automanager.i18n import N_
from youtube_automanager.web.ui import errors, layout

# The tests of a failed handler expect the ERROR record of NiceGUI, so only the other tests use ``no_logged_errors``.
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups")]

MESSAGE = "Something went wrong on this page. The log of the service holds the details."


class PageFailure(Exception):
    """The exception that a handler of the test page raises."""


@pytest.fixture
def failing_page(nicegui_globals, monkeypatch):
    """Register the page ``/fail``, whose buttons and timer raise PageFailure. A test asks for it before ``user``."""
    monkeypatch.setattr(layout, "_PAGES", dict(layout._PAGES))

    @layout.register_page("/fail", label=N_("Fail"), icon="error")
    async def fail_page(services):
        def fail() -> None:
            raise PageFailure("sync handler")

        async def fail_later() -> None:
            await asyncio.sleep(0)
            raise PageFailure("async handler")

        def fail_in_timer() -> None:
            raise PageFailure("timer")

        async def time_out() -> None:
            async with asyncio.timeout(0):
                await asyncio.sleep(1)

        ui.button("Fail", on_click=fail).mark("fail")
        ui.button("Fail later", on_click=fail_later).mark("fail-later")
        ui.button("Start timer", on_click=lambda: ui.timer(0.01, fail_in_timer, once=True)).mark("fail-timer")
        ui.button("Time out", on_click=time_out).mark("time-out")


def errors_of(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.get_records("call") if record.levelno >= logging.ERROR]


@pytest.mark.parametrize(
    ("marker", "kind", "detail"),
    [
        ("fail", PageFailure, "sync handler"),
        ("fail-later", PageFailure, "async handler"),
        ("fail-timer", PageFailure, "timer"),
        # Only the timeout of the drawer stays silent, so a handler of a page that times out still tells the user.
        ("time-out", TimeoutError, ""),
    ],
)
async def test_failed_handler_tells_the_user_and_the_log_holds_one_trace(
    failing_page, settings, fake_starter, caplog, marker, kind, detail
):
    async with running_service(settings, fake_starter) as user:
        await user.open("/fail")
        user.find(marker=marker).click()

        await eventually(lambda: user.notify.messages, "the message of the failure")
        records = await eventually(lambda: errors_of(caplog), "the log record of the failure")
        # A second handler or a second trace would come within this pause.
        await asyncio.sleep(0.1)

    assert user.notify.messages == [MESSAGE]
    (record,) = errors_of(caplog)
    assert record is records[0]
    assert record.name == "nicegui"
    assert record.exc_info is not None
    assert type(record.exc_info[1]) is kind
    assert str(record.exc_info[1]) == detail


async def test_late_answer_to_the_drawer_stays_in_the_log_and_tells_nobody(
    failing_page, settings, fake_starter, caplog
):
    async with running_service(settings, fake_starter) as user:
        # Without its rule, the simulated browser never answers the request with which the drawer reads its state.
        user.javascript_rules.clear()
        await user.open("/fail")
        (record,) = await eventually(lambda: errors_of(caplog), "the timeout of the drawer")
        # The message would come right after the log record, within this pause.
        await asyncio.sleep(0.1)

    assert user.notify.messages == []
    assert record.name == "nicegui"
    assert record.getMessage() == "JavaScript did not respond within 1.0 s"
    assert record.exc_info is not None
    assert errors.is_drawer_timeout(record.exc_info[1])


@pytest.mark.usefixtures("no_logged_errors")
async def test_the_handler_of_the_service_is_added_once_to_the_reset_application(failing_page, settings, fake_starter):
    async with running_service(settings, fake_starter):
        assert nicegui_app._exception_handlers.count(errors.notify_failure) == 1


@pytest.fixture
def shown(monkeypatch):
    """Replace ``ui.notify`` with a mock and return the mock."""
    shown = Mock()
    monkeypatch.setattr(ui, "notify", shown)
    return shown


def deleted_slot() -> Mock:
    slot = Mock()
    type(slot).parent = PropertyMock(side_effect=RuntimeError("The parent element has been deleted."))
    return slot


def live_slot() -> Mock:
    slot = Mock()
    slot.parent.client = MagicMock(is_deleted=False)
    return slot


@pytest.mark.usefixtures("no_logged_errors")
async def test_failure_outside_a_page_tells_nobody_and_does_not_raise(shown):
    async def background_task() -> None:
        errors.notify_failure(PageFailure("background"))

    # A new task starts with an empty slot stack, as a background task of the service does.
    await asyncio.create_task(background_task())

    shown.assert_not_called()


@pytest.mark.usefixtures("no_logged_errors")
def test_failure_in_a_deleted_element_tells_nobody(shown, monkeypatch):
    monkeypatch.setattr(Slot, "get_stack", Mock(return_value=[deleted_slot()]))

    errors.notify_failure(PageFailure("deleted element"))

    shown.assert_not_called()


@pytest.mark.usefixtures("no_logged_errors")
def test_failure_in_a_deleted_client_tells_nobody(shown, monkeypatch):
    slot = live_slot()
    slot.parent.client.is_deleted = True
    monkeypatch.setattr(Slot, "get_stack", Mock(return_value=[slot]))

    errors.notify_failure(PageFailure("deleted client"))

    shown.assert_not_called()


@pytest.mark.usefixtures("no_logged_errors")
def test_failure_in_a_page_shows_a_negative_message_in_the_client_of_the_page(shown, monkeypatch):
    slot = live_slot()
    monkeypatch.setattr(Slot, "get_stack", Mock(return_value=[slot]))

    errors.notify_failure(PageFailure("page"))

    shown.assert_called_once_with(MESSAGE, type="negative")
    slot.parent.client.__enter__.assert_called_once_with()


@pytest.mark.usefixtures("no_logged_errors")
def test_failure_under_a_deleted_element_goes_to_the_client_of_the_newest_live_slot(shown, monkeypatch):
    older, newer = live_slot(), live_slot()
    monkeypatch.setattr(Slot, "get_stack", Mock(return_value=[older, newer, deleted_slot()]))

    errors.notify_failure(PageFailure("rebuilt element"))

    shown.assert_called_once_with(MESSAGE, type="negative")
    newer.parent.client.__enter__.assert_called_once_with()
    older.parent.client.__enter__.assert_not_called()
