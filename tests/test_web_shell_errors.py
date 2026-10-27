"""A page builder that fails inside the shell: at a full load of its path, and after a click in the drawer."""

import asyncio
import logging

import pytest
from nicegui import ui
from ui_support import eventually, running_service

from youtube_automanager.i18n import N_
from youtube_automanager.web.ui import layout

# These tests expect the ERROR record of NiceGUI, so they do not use ``no_logged_errors``.
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups")]

MESSAGE = "Something went wrong on this page. The log of the service holds the details."


class PageFailure(Exception):
    """The exception that the builder of the test page raises."""


@pytest.fixture
def failing_builder(nicegui_globals, monkeypatch):
    """Register the page ``/broken``, whose builder shows a label and then raises PageFailure."""
    monkeypatch.setattr(layout, "_PAGES", dict(layout._PAGES))

    @layout.register_page("/broken", label=N_("Broken"), icon="error")
    async def broken_page(services):
        ui.label("before the failure").mark("before-failure")
        await asyncio.sleep(0)
        raise PageFailure("page builder")


def errors_of(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.get_records("call") if record.levelno >= logging.ERROR]


async def test_a_failed_page_builder_at_a_full_load_tells_the_user_and_the_log_holds_one_trace(
    failing_builder, settings, fake_starter, caplog
):
    async with running_service(settings, fake_starter) as user:
        await user.open("/broken")
        await eventually(lambda: user.notify.messages, "the message of the failure")
        # A second message or a second trace would come within this pause.
        await asyncio.sleep(0.1)
        await user.should_see(marker="before-failure")
        await user.should_see(marker="navigation")
        assert user.notify.messages == [MESSAGE]
        (record,) = errors_of(caplog)
        assert record.name == "nicegui"
        assert isinstance(record.exc_info, tuple)
        assert type(record.exc_info[1]) is PageFailure
        # The simulated browser also collects the messages of this second client, so the request comes last.
        answer = await user.http_client.get("/broken")

    assert answer.status_code == 200
    assert "before the failure" in answer.text


async def test_a_failed_page_builder_after_a_click_in_the_drawer_tells_the_user(
    failing_builder, settings, fake_starter, caplog
):
    async with running_service(settings, fake_starter) as user:
        client = await user.open("/")
        await user.should_see(marker="ytdlp-version")

        user.find(marker="nav:/broken").click()

        await eventually(lambda: user.notify.messages, "the message of the failure")
        await asyncio.sleep(0.1)
        await user.should_see(marker="before-failure")
        assert user.client is client
        assert "active" in user.find(marker="nav:/broken").elements.pop().props

    assert user.notify.messages == [MESSAGE]
    (record,) = errors_of(caplog)
    assert isinstance(record.exc_info, tuple)
    assert type(record.exc_info[1]) is PageFailure
