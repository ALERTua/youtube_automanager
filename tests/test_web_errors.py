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

        ui.button("Fail", on_click=fail).mark("fail")
        ui.button("Fail later", on_click=fail_later).mark("fail-later")
        ui.button("Start timer", on_click=lambda: ui.timer(0.01, fail_in_timer, once=True)).mark("fail-timer")


def errors_of(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.get_records("call") if record.levelno >= logging.ERROR]


@pytest.mark.parametrize(
    ("marker", "detail"),
    [("fail", "sync handler"), ("fail-later", "async handler"), ("fail-timer", "timer")],
)
async def test_failed_handler_tells_the_user_and_the_log_holds_one_trace(
    failing_page, settings, fake_starter, caplog, marker, detail
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
    assert isinstance(record.exc_info[1], PageFailure)
    assert str(record.exc_info[1]) == detail


@pytest.mark.usefixtures("no_logged_errors")
async def test_the_handler_of_the_service_is_added_once_to_the_reset_application(failing_page, settings, fake_starter):
    async with running_service(settings, fake_starter):
        assert nicegui_app._exception_handlers.count(errors.notify_failure) == 1


@pytest.mark.usefixtures("no_logged_errors")
async def test_failure_outside_a_page_tells_nobody_and_does_not_raise(monkeypatch):
    shown = Mock()
    monkeypatch.setattr(ui, "notify", shown)

    async def background_task() -> None:
        errors.notify_failure()

    # A new task starts with an empty slot stack, as a background task of the service does.
    await asyncio.create_task(background_task())

    shown.assert_not_called()


@pytest.mark.usefixtures("no_logged_errors")
def test_failure_in_a_deleted_element_tells_nobody(monkeypatch):
    shown = Mock()
    monkeypatch.setattr(ui, "notify", shown)
    slot = Mock()
    type(slot).parent = PropertyMock(side_effect=RuntimeError("The parent element has been deleted."))
    monkeypatch.setattr(Slot, "get_stack", Mock(return_value=[slot]))

    errors.notify_failure()

    shown.assert_not_called()


@pytest.mark.usefixtures("no_logged_errors")
def test_failure_in_a_deleted_client_tells_nobody(monkeypatch):
    shown = Mock()
    monkeypatch.setattr(ui, "notify", shown)
    slot = Mock()
    slot.parent.client.is_deleted = True
    monkeypatch.setattr(Slot, "get_stack", Mock(return_value=[slot]))

    errors.notify_failure()

    shown.assert_not_called()


@pytest.mark.usefixtures("no_logged_errors")
def test_failure_in_a_page_shows_a_negative_message_in_the_client_of_the_page(monkeypatch):
    shown = Mock()
    monkeypatch.setattr(ui, "notify", shown)
    slot = Mock()
    client = MagicMock(is_deleted=False)
    slot.parent.client = client
    monkeypatch.setattr(Slot, "get_stack", Mock(return_value=[slot]))

    errors.notify_failure()

    shown.assert_called_once_with(MESSAGE, type="negative")
    client.__enter__.assert_called_once_with()
