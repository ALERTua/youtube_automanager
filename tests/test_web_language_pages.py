"""Each page of the service opens in Ukrainian with the real catalog and shows its translated title."""

import pytest
from nicegui import app, ui
from ui_support import element_of, running_service, store

from youtube_automanager.db.models import Setting
from youtube_automanager.i18n.setting import LANGUAGE_KEY

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]

# The drawer of the shell in its order: the path of each page with its English and its Ukrainian label.
DRAWER = [
    ("/", "Dashboard", "Огляд"),
    ("/rules", "Rules", "Правила"),
    ("/subscriptions", "Subscriptions", "Підписки"),
    ("/history", "History", "Історія"),
    ("/scheduler", "Scheduler", "Планувальник"),
    ("/settings", "Settings", "Налаштування"),
]


def drawer_labels(user) -> dict[str, str]:
    """Return the label of each item of the navigation drawer by the path of its page."""
    labels = {}
    for path, _english, _ukrainian in DRAWER:
        (label,) = [
            element for element in element_of(user, f"nav:{path}").descendants() if isinstance(element, ui.item_label)
        ]
        labels[path] = label.text
    return labels


def headings(user) -> list[str]:
    """Return the text of each page heading, a label of the class ``text-h5``, in the content frame."""
    return [
        element.text
        for element in user.client.elements.values()
        if isinstance(element, ui.label) and "text-h5" in element.classes
    ]


@pytest.fixture
async def ukrainian_user(nicegui_globals, settings, sessions, fake_starter, real_uk_catalog):
    """A simulated browser on the whole service whose stored language is Ukrainian, with the real catalog."""
    store(sessions, Setting(key=LANGUAGE_KEY, value="uk"))
    async with running_service(settings, fake_starter) as user:
        yield user


@pytest.mark.parametrize(("path", "english", "ukrainian"), DRAWER)
async def test_each_page_opens_in_ukrainian_and_shows_its_translated_title(ukrainian_user, path, english, ukrainian):
    user = ukrainian_user

    client = await user.open(path)

    # The title of the browser tab, the heading of the page and the item of the drawer name the page in Ukrainian.
    assert client.title == f"{ukrainian} - YouTube AutoManager"
    assert headings(user) == [ukrainian]
    assert drawer_labels(user)[path] == ukrainian
    assert element_of(user, f"nav:{path}").props.get("active") in (True, "")
    # No English name of a page is left in the title, the heading or the drawer.
    assert english not in client.title
    assert english not in headings(user)
    assert english not in drawer_labels(user).values()
    # The language of the shell follows too: the list shows Ukrainian, and the Quasar texts load the Ukrainian pack.
    assert element_of(user, "language").props["label"] == "Українська"
    assert element_of(user, "language").props["aria-label"] == "Мова: Українська"
    assert app.config.language == "uk"


async def test_the_drawer_shows_each_page_in_ukrainian_on_the_first_page(ukrainian_user):
    await ukrainian_user.open("/")

    assert drawer_labels(ukrainian_user) == {path: ukrainian for path, _english, ukrainian in DRAWER}


async def test_the_rule_editor_opens_in_ukrainian(ukrainian_user):
    user = ukrainian_user

    client = await user.open("/rules/new")

    # The editor belongs to the Rules page, so the tab and the drawer name that page.
    assert client.title == "Правила - YouTube AutoManager"
    assert drawer_labels(user)["/rules"] == "Правила"
    assert element_of(user, "nav:/rules").props.get("active") in (True, "")
    assert element_of(user, "editor-title").text == "Нове правило"
    assert "New rule" not in [element.text for element in client.elements.values() if isinstance(element, ui.label)]
