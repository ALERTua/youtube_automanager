"""The language of the pages: one stored setting of the service, with the language button of the header."""

import asyncio
from urllib.parse import urlsplit

import pytest
from conftest import recording_run_javascript
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

from youtube_automanager.db.models import Run, RunStatus, RunTrigger, Setting
from youtube_automanager.i18n import language
from youtube_automanager.i18n.setting import LANGUAGE_KEY, read_language
from youtube_automanager.web.ui.layout import RELOAD_SCRIPT, UNLOAD_WARNING_FLAG

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]

# How long a test waits for a call that must not come.
SETTLE_SECONDS = 0.3
# The History page with a view in its query.
HISTORY_VIEW = "/history?status=failed"


def tooltip_of(user, button: ui.element) -> ui.tooltip:
    """Return the tooltip of ``button``, a sibling with the prop target."""
    (tooltip,) = [
        element
        for element in client_of(user).elements.values()
        if isinstance(element, ui.tooltip) and element.props.get("target") == f"#{button.html_id}"
    ]
    return tooltip


def label_of(user, path: str) -> str:
    """Return the label of the drawer item of the page at ``path``."""
    (label,) = [
        element for element in element_of(user, f"nav:{path}").descendants() if isinstance(element, ui.item_label)
    ]
    return label.text


@pytest.fixture
def browser(monkeypatch) -> list[str]:
    """Record the scripts of the switch that the user simulation does not run: the new load of the page and the
    warning flag before a reload."""
    scripts: list[str] = []
    markers = (RELOAD_SCRIPT, UNLOAD_WARNING_FLAG)
    monkeypatch.setattr(Client, "run_javascript", recording_run_javascript(scripts.append, markers=markers))
    return scripts


def browser_address(client: Client) -> str:
    """Return the address that ``RELOAD_SCRIPT`` loads in the tab of ``client``: its path and its query."""
    parts = urlsplit(client.sub_pages_router.current_path)
    return parts.path + (f"?{parts.query}" if parts.query else "")


async def switch_language(user, browser: list[str]) -> Client:
    """Click the language button, wait for the script of the new load, and load the address of the tab as the browser
    does. Return the client of the new load."""
    reloads = browser.count(RELOAD_SCRIPT)
    user.find(marker="language").click()
    await eventually(lambda: browser.count(RELOAD_SCRIPT) > reloads, "The script of the new load")
    return await user.open(browser_address(client_of(user)))


def test_the_new_load_keeps_the_path_and_the_query_and_works_after_a_refused_reload():
    # Firefox refuses location.reload() and history.go(0) after the user stayed on the page in the warning before a
    # reload. location.replace of an address that only differs in the fragment would only scroll.
    assert RELOAD_SCRIPT == "location.replace(location.pathname + location.search);"


async def test_the_language_button_names_the_language_of_its_click(user):
    await user.open("/")

    button = element_of(user, "language")
    assert button.props["icon"] == "translate"
    assert button.props["aria-label"] == "Українська"
    tooltip = tooltip_of(user, button)
    assert tooltip.text == "Українська"
    # A screen reader speaks the name with the voice of its language, also in the tooltip that Quasar moves to the body.
    assert (button.props["lang"], tooltip.props["lang"]) == ("uk", "uk")
    assert app.config.language == "en-US"


async def test_the_switch_stores_the_language_and_the_new_load_shows_the_catalog(user, sessions, uk_catalog, browser):
    await user.open(HISTORY_VIEW)
    assert label_of(user, "/history") == "History"

    client = await switch_language(user, browser)

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
    assert button.props["aria-label"] == "English"
    tooltip = tooltip_of(user, button)
    assert tooltip.text == "English"
    assert (button.props["lang"], tooltip.props["lang"]) == ("en", "en")


async def test_the_new_load_of_an_open_history_run_shows_the_run_again(user, sessions, uk_catalog, browser):
    store(sessions, Run(status=RunStatus.FAILED, trigger=RunTrigger.MANUAL, error_text="boom"))
    await user.open("/history?run=1")
    await eventually(lambda: client_of(user).sub_pages_router.current_path.endswith("#run-actions"), "The fragment")

    client = await switch_language(user, browser)

    # The new load leaves the fragment out, and the page writes it again when it builds.
    assert user.back_history[-1] == "/history?run=1"
    await eventually(lambda: client.sub_pages_router.current_path == "/history?run=1#run-actions", "The fragment")
    await user.should_see("boom")


async def test_the_new_load_gets_the_language_pack_of_quasar(user, uk_catalog, browser):
    await user.open("/")
    page = (await user.http_client.get("/")).text
    assert '<html lang="en-US">' in page
    assert "/static/lang/en-US.umd.prod.js" in page

    await switch_language(user, browser)

    assert app.config.language == "uk"
    page = (await user.http_client.get("/")).text
    assert '<html lang="uk">' in page
    assert "/static/lang/uk.umd.prod.js" in page


async def test_a_second_switch_goes_back_to_english(user, sessions, uk_catalog, browser):
    await user.open("/")
    await switch_language(user, browser)
    await user.should_see(marker="language")

    await switch_language(user, browser)

    assert read_language(sessions) == "en"
    await user.should_see(marker="language")
    assert label_of(user, "/") == "Dashboard"
    assert element_of(user, "language").props["aria-label"] == "Українська"
    assert app.config.language == "en-US"


async def test_the_stored_language_applies_at_the_start(nicegui_globals, settings, sessions, fake_starter, uk_catalog):
    store(sessions, Setting(key=LANGUAGE_KEY, value="uk"))

    async with running_service(settings, fake_starter) as user:
        assert language() == "uk"
        await user.open("/")
        assert label_of(user, "/") == "Панель"
        assert element_of(user, "language").props["aria-label"] == "English"
        assert app.config.language == "uk"


async def test_a_service_without_a_stored_language_starts_in_english(user, sessions, uk_catalog):
    await user.open("/")

    assert language() == "en"
    assert label_of(user, "/") == "Dashboard"
    assert read_language(sessions) == "en"


async def test_the_switch_with_changes_of_the_editor_asks_and_keep_editing_stays(user, sessions, uk_catalog, browser):
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
    assert RELOAD_SCRIPT not in browser


async def test_the_switch_with_changes_of_the_editor_and_discard_switches(user, sessions, uk_catalog, browser):
    await user.open("/rules/new")
    set_value(user, "rule-name", "Changed")
    await eventually(lambda: "window.__warnBeforeUnload = true;" in browser, "The warning before a reload")

    user.find(marker="language").click()
    await eventually(lambda: elements_of(user, "discard-dialog"), "The question of the switch")
    user.find(marker="discard-confirm").click()

    await eventually(lambda: RELOAD_SCRIPT in browser, "The script of the new load")
    assert read_language(sessions) == "uk"
    # The user agreed to leave, so the browser must not ask again before the new load.
    after_change = browser[browser.index("window.__warnBeforeUnload = true;") :]
    assert after_change.index("window.__warnBeforeUnload = false;") < after_change.index(RELOAD_SCRIPT)
    await user.open(browser_address(client_of(user)))
    assert user.back_history[-1] == "/rules/new"
    assert label_of(user, "/") == "Панель"
