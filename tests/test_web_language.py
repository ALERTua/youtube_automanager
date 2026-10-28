"""The language of the pages: one stored setting of the service, with the language list of the header. Until the user
chooses a language, each load of a page takes the language of its browser."""

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
from youtube_automanager.i18n.setting import LANGUAGE_KEY, NAMES, read_language, save_language
from youtube_automanager.web.ui import QUASAR_LANGUAGES
from youtube_automanager.web.ui.layout import RELOAD_SCRIPT, UNLOAD_WARNING_FLAG

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]

# How long a test waits for a call that must not come.
SETTLE_SECONDS = 0.3
# The History page with a view in its query.
HISTORY_VIEW = "/history?status=failed"


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


def option_names(user) -> list[tuple[str, str]]:
    """Return the code and the shown name of each item of the language list, in order."""
    items = elements_of(user, "language:en") + elements_of(user, "language:uk")
    return [
        (item.props["lang"], label.text)
        for item in items
        for label in item.descendants()
        if isinstance(label, ui.item_label)
    ]


async def switch_language(user, browser: list[str], code: str = "uk") -> Client:
    """Choose the language ``code`` in the list, wait for the script of the new load, and load the address of the tab
    as the browser does. Return the client of the new load."""
    reloads = browser.count(RELOAD_SCRIPT)
    user.find(marker=f"language:{code}").click()
    await eventually(lambda: browser.count(RELOAD_SCRIPT) > reloads, "The script of the new load")
    return await user.open(browser_address(client_of(user)))


def test_the_new_load_keeps_the_path_and_the_query_and_works_after_a_refused_reload():
    # Firefox refuses location.reload() and history.go(0) after the user stayed on the page in the warning before a
    # reload. location.replace of an address that only differs in the fragment would only scroll.
    assert RELOAD_SCRIPT == "location.replace(location.pathname + location.search);"


async def test_the_header_list_shows_the_current_language_and_offers_exactly_the_two_languages(user):
    await user.open("/")

    dropdown = element_of(user, "language")
    assert isinstance(dropdown, ui.dropdown_button)
    assert dropdown.props["icon"] == "translate"
    # The list shows the current language, in that language. English is the language of a browser without a header.
    assert dropdown.props["label"] == "English"
    assert dropdown.props["aria-label"] == "Language: English"
    # A screen reader speaks each name with the voice of its language.
    assert dropdown.props["lang"] == "en"
    # No item for the language of the browser: the list has the two languages only.
    assert option_names(user) == [("en", "English"), ("uk", "Українська")]
    assert elements_of(user, "language:en")[0].props.get("active") in (True, "")
    assert "active" not in elements_of(user, "language:uk")[0].props
    assert app.config.language == "en-US"


async def test_the_list_shows_ukrainian_with_the_real_catalog_and_marks_its_item(user, sessions, real_uk_catalog):
    save_language(sessions, "uk")

    await user.open("/")

    dropdown = element_of(user, "language")
    assert dropdown.props["label"] == "Українська"
    assert dropdown.props["aria-label"] == "Мова: Українська"
    assert dropdown.props["lang"] == "uk"
    # Each item keeps its own language, whatever the language of the page is.
    assert option_names(user) == [("en", "English"), ("uk", "Українська")]
    assert elements_of(user, "language:uk")[0].props.get("active") in (True, "")
    assert "active" not in elements_of(user, "language:en")[0].props


async def test_the_list_is_a_button_that_the_keyboard_reaches_and_that_has_a_name(user):
    await user.open("/")

    dropdown = element_of(user, "language")
    # Quasar renders q-btn-dropdown as a button, so Tab reaches it, and Enter and Space open the list of its items.
    assert dropdown.tag == "q-btn-dropdown"
    assert not dropdown.props.get("disable")
    assert dropdown.props["aria-label"]
    # An item is a q-item with a click handler, which Quasar makes a focusable element.
    assert all(
        isinstance(item, ui.item) for item in elements_of(user, "language:en") + elements_of(user, "language:uk")
    )


async def test_the_choice_stores_the_language_and_the_new_load_shows_the_catalog(user, sessions, uk_catalog, browser):
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
    dropdown = element_of(user, "language")
    assert dropdown.props["label"] == "Українська"
    assert dropdown.props["lang"] == "uk"


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


async def test_a_second_choice_goes_back_to_english(user, sessions, uk_catalog, browser):
    await user.open("/")
    await switch_language(user, browser, "uk")
    await user.should_see(marker="language")

    await switch_language(user, browser, "en")

    assert read_language(sessions) == "en"
    await user.should_see(marker="language")
    assert label_of(user, "/") == "Dashboard"
    assert element_of(user, "language").props["label"] == "English"
    assert app.config.language == "en-US"


async def test_the_choice_of_the_shown_language_stores_it(user, sessions, browser):
    # With nothing stored, English comes from the browser. Choosing it stores it, so the browser no longer decides.
    await user.open("/")
    assert read_language(sessions) is None

    await switch_language(user, browser, "en")

    assert read_language(sessions) == "en"
    assert language() == "en"


async def test_the_stored_language_applies_at_the_start(nicegui_globals, settings, sessions, fake_starter, uk_catalog):
    store(sessions, Setting(key=LANGUAGE_KEY, value="uk"))

    async with running_service(settings, fake_starter) as user:
        assert language() == "uk"
        await user.open("/")
        assert label_of(user, "/") == "Панель"
        assert element_of(user, "language").props["label"] == "Українська"
        assert app.config.language == "uk"


async def test_a_service_without_a_stored_language_starts_in_english(
    nicegui_globals, settings, sessions, fake_starter, uk_catalog
):
    async with running_service(settings, fake_starter) as user:
        # The start applies no language, and the first page takes the language of its browser.
        assert language() == "en"
        await user.open("/")

        assert language() == "en"
        assert label_of(user, "/") == "Dashboard"
        assert read_language(sessions) is None


async def test_a_stored_value_that_is_not_a_language_is_the_same_as_no_value(user, sessions, uk_catalog, caplog):
    store(sessions, Setting(key=LANGUAGE_KEY, value="xx"))
    user.http_client.headers["Accept-Language"] = "uk"

    await user.open("/")

    assert language() == "uk"
    assert label_of(user, "/") == "Панель"
    assert "The stored language 'xx' is not one of en, uk, so the pages follow the browser" in caplog.text


@pytest.mark.parametrize(
    ("header", "language_code", "dashboard", "quasar"),
    [
        ("uk", "uk", "Панель", "uk"),
        ("uk-UA,uk;q=0.9,en;q=0.8", "uk", "Панель", "uk"),
        ("en-US,uk;q=0.5", "en", "Dashboard", "en-US"),
        ("de-DE,de;q=0.9", "en", "Dashboard", "en-US"),
        ("fr;q=0.9,uk;q=0.8", "uk", "Панель", "uk"),
        ("", "en", "Dashboard", "en-US"),
        (";;q=,,", "en", "Dashboard", "en-US"),
    ],
)
async def test_with_nothing_stored_each_load_takes_the_language_of_its_browser(
    user, sessions, uk_catalog, header, language_code, dashboard, quasar
):
    user.http_client.headers["Accept-Language"] = header

    await user.open("/")

    assert language() == language_code
    assert label_of(user, "/") == dashboard
    assert element_of(user, "language").props["label"] == NAMES[language_code]
    # The Quasar language follows the language of the texts.
    assert app.config.language == quasar
    page = (await user.http_client.get("/")).text
    assert f'<html lang="{quasar}">' in page
    assert f"/static/lang/{quasar}.umd.prod.js" in page
    # The language of the browser is not stored.
    assert read_language(sessions) is None


async def test_a_load_without_the_header_gives_english(user, uk_catalog):
    assert "accept-language" not in user.http_client.headers

    await user.open("/")

    assert language() == "en"
    assert label_of(user, "/") == "Dashboard"


async def test_with_nothing_stored_the_last_load_decides_for_the_open_pages(user, sessions, uk_catalog):
    user.http_client.headers["Accept-Language"] = "uk"
    await user.open("/")
    assert label_of(user, "/") == "Панель"

    user.http_client.headers["Accept-Language"] = "de"
    await user.open("/")

    assert language() == "en"
    assert label_of(user, "/") == "Dashboard"


@pytest.mark.parametrize(("stored", "header"), [("en", "uk"), ("uk", "en"), ("uk", "de"), ("en", "uk-UA")])
async def test_a_stored_language_wins_over_the_header(user, sessions, uk_catalog, stored, header):
    save_language(sessions, stored)
    user.http_client.headers["Accept-Language"] = header

    await user.open("/")

    assert language() == stored
    assert label_of(user, "/") == ("Панель" if stored == "uk" else "Dashboard")
    assert element_of(user, "language").props["label"] == NAMES[stored]
    assert app.config.language == QUASAR_LANGUAGES[stored]


async def test_after_a_choice_the_header_is_never_read_again(user, sessions, uk_catalog, browser):
    user.http_client.headers["Accept-Language"] = "uk"
    await user.open("/")
    assert language() == "uk"

    # The user chooses English against the header of the browser.
    await switch_language(user, browser, "en")
    await user.open("/")

    assert read_language(sessions) == "en"
    assert language() == "en"
    assert label_of(user, "/") == "Dashboard"
    assert element_of(user, "language").props["label"] == "English"


async def test_the_switch_with_changes_of_the_editor_asks_and_keep_editing_stays(user, sessions, uk_catalog, browser):
    client = await user.open("/rules/new")
    set_value(user, "rule-name", "Changed")

    user.find(marker="language:uk").click()

    await eventually(lambda: elements_of(user, "discard-dialog"), "The question of the switch")
    assert text_of(user, "discard-question") == "Discard the changes?"
    user.find(marker="discard-cancel").click()
    await eventually(lambda: not elements_of(user, "discard-dialog"), "The close of the question")
    await asyncio.sleep(SETTLE_SECONDS)
    assert user.client is client
    assert element_of(user, "rule-name").value == "Changed"
    assert read_language(sessions) is None
    assert language() == "en"
    assert RELOAD_SCRIPT not in browser


async def test_the_switch_with_changes_of_the_editor_and_discard_switches(user, sessions, uk_catalog, browser):
    await user.open("/rules/new")
    set_value(user, "rule-name", "Changed")
    await eventually(lambda: "window.__warnBeforeUnload = true;" in browser, "The warning before a reload")

    user.find(marker="language:uk").click()
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
