"""The language of the pages: one stored setting of the service, with the language button of the header."""

import asyncio

import pytest
from nicegui import Client, app, ui
from ui_support import (
    client_of,
    element_of,
    elements_of,
    eventually,
    running_service,
    set_value,
    store,
    text_of,
)

from youtube_automanager.db.models import Setting
from youtube_automanager.i18n import language
from youtube_automanager.i18n.setting import LANGUAGE_KEY, read_language

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]

# How long a test waits for a call that must not come.
SETTLE_SECONDS = 0.3
# The History page with a view in its query.
HISTORY_VIEW = "/history?status=failed"


def tooltip_of(user, button: ui.element) -> str:
    """Return the text of the tooltip of ``button``, which NiceGUI makes as a sibling with the prop target."""
    (text,) = [
        element.text
        for element in client_of(user).elements.values()
        if isinstance(element, ui.tooltip) and element.props.get("target") == f"#{button.html_id}"
    ]
    return text


def label_of(user, path: str) -> str:
    """Return the label of the drawer item of the page at ``path``."""
    (label,) = [
        element for element in element_of(user, f"nav:{path}").descendants() if isinstance(element, ui.item_label)
    ]
    return label.text


async def switch_language(user) -> Client:
    """Click the language button, and wait for the new load of the page. Return the client of the new load."""
    old = user.client
    user.find(marker="language").click()
    return await eventually(lambda: user.client if user.client is not old else None, "The new load of the page")


async def test_the_language_button_names_the_shown_language(user):
    await user.open("/")

    button = element_of(user, "language")
    assert button.props["icon"] == "translate"
    assert button.props["aria-label"] == "English"
    assert tooltip_of(user, button) == "English"
    assert app.config.language == "en-US"


async def test_the_switch_stores_the_language_and_the_new_load_shows_the_catalog(user, sessions, uk_catalog):
    await user.open(HISTORY_VIEW)
    assert label_of(user, "/history") == "History"

    client = await switch_language(user)

    assert read_language(sessions) == "uk"
    assert language() == "uk"
    # The new load keeps the address with its query.
    assert user.back_history[-1] == HISTORY_VIEW
    await user.should_see(marker="refresh")
    assert label_of(user, "/history") == "Історія"
    assert label_of(user, "/") == "Панель"
    # A text without a translation in the catalog stays English.
    assert label_of(user, "/scheduler") == "Scheduler"
    assert client.title == "Історія — YouTube AutoManager"
    button = element_of(user, "language")
    assert button.props["aria-label"] == "Українська"
    assert tooltip_of(user, button) == "Українська"


async def test_the_new_load_gets_the_language_pack_of_quasar(user, uk_catalog):
    await user.open("/")
    page = (await user.http_client.get("/")).text
    assert '<html lang="en-US">' in page
    assert "/static/lang/en-US.umd.prod.js" in page

    await switch_language(user)

    assert app.config.language == "uk"
    page = (await user.http_client.get("/")).text
    assert '<html lang="uk">' in page
    assert "/static/lang/uk.umd.prod.js" in page


async def test_a_second_switch_goes_back_to_english(user, sessions, uk_catalog):
    await user.open("/")
    await switch_language(user)
    await user.should_see(marker="language")

    await switch_language(user)

    assert read_language(sessions) == "en"
    await user.should_see(marker="language")
    assert label_of(user, "/") == "Dashboard"
    assert element_of(user, "language").props["aria-label"] == "English"
    assert app.config.language == "en-US"


async def test_the_stored_language_applies_at_the_start(nicegui_globals, settings, sessions, fake_starter, uk_catalog):
    store(sessions, Setting(key=LANGUAGE_KEY, value="uk"))

    async with running_service(settings, fake_starter) as user:
        assert language() == "uk"
        await user.open("/")
        assert label_of(user, "/") == "Панель"
        assert element_of(user, "language").props["aria-label"] == "Українська"
        assert app.config.language == "uk"


async def test_a_service_without_a_stored_language_starts_in_english(user, sessions, uk_catalog):
    await user.open("/")

    assert language() == "en"
    assert label_of(user, "/") == "Dashboard"
    assert read_language(sessions) == "en"


async def test_the_switch_with_changes_of_the_editor_asks_and_keep_editing_stays(user, sessions, uk_catalog):
    client = await user.open("/rules/new")
    set_value(user, "rule-name", "Changed")

    user.find(marker="language").click()

    await eventually(lambda: elements_of(user, "discard-dialog"), "The question of the switch")
    assert text_of(user, "discard-question") == "Discard the changes?"
    user.find(marker="discard-cancel").click()
    await eventually(lambda: not elements_of(user, "discard-dialog"), "The close of the question")
    await asyncio.sleep(SETTLE_SECONDS)
    assert user.client is client
    assert element_of(user, "rule-name").value == "Changed"
    assert read_language(sessions) == "en"
    assert language() == "en"


async def test_the_switch_with_changes_of_the_editor_and_discard_switches(user, sessions, uk_catalog, monkeypatch):
    written: list[str] = []
    real = Client.run_javascript

    def run_javascript(self, code, **kwargs):
        written.append(code)
        return real(self, code, **kwargs)

    monkeypatch.setattr(Client, "run_javascript", run_javascript)
    await user.open("/rules/new")
    set_value(user, "rule-name", "Changed")
    await eventually(lambda: "window.__warnBeforeUnload = true;" in written, "The warning before a reload")

    user.find(marker="language").click()
    await eventually(lambda: elements_of(user, "discard-dialog"), "The question of the switch")
    user.find(marker="discard-confirm").click()

    await eventually(lambda: read_language(sessions) == "uk", "The stored language")
    await eventually(lambda: user.back_history[-1] == "/rules/new" and elements_of(user, "rule-name"), "The new load")
    # The user agreed to leave, so the browser must not ask again before the new load.
    assert "window.__warnBeforeUnload = false;" in written[written.index("window.__warnBeforeUnload = true;") :]
    assert label_of(user, "/") == "Панель"
