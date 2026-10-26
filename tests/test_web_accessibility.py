"""Tests of the accessibility of the admin pages: each icon-only button has a name, and each clickable table row has a
keyboard path. The tests read the elements of the page. The cells of a table slot do not render in the user
simulation, so a test of such a cell reads the template of the slot.
"""

from datetime import UTC, datetime

import pytest
from nicegui import ui
from nicegui.elements.skip_link import SkipLink
from ui_support import client_of, element_of, elements_of, eventually, store

from youtube_automanager.db.models import (
    Channel,
    NotificationTarget,
    Playlist,
    Rule,
    RuleChannel,
    Run,
    RunAction,
    RunActionKind,
    RunStatus,
    RunTrigger,
)
from youtube_automanager.web.ui import layout

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]

PAGES = ["/", "/rules", "/history", "/scheduler", "/settings", "/subscriptions"]
CHANNEL = "UCaaaaaaaaaaaaaaaaaaaaaa"
MOMENT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
LONG = {">=": [{"var": "duration_s"}, 120]}


@pytest.fixture
def stored_data(sessions):
    """A channel, a playlist, a rule with a nested group, a run with an action and a notification target."""
    store(
        sessions,
        Channel(channel_id=CHANNEL, title="Alpha", handle="@alpha", is_subscribed=True),
        Playlist(playlist_id="WL", title="Watch later", is_own=True),
        Rule(
            id=1,
            name="Long videos",
            enabled=True,
            position=1,
            condition={"and": [LONG, {"or": [LONG, LONG]}]},
            target_playlist_id="WL",
            no_shorts=False,
            created_at=MOMENT,
            updated_at=MOMENT,
        ),
        RuleChannel(rule_id=1, channel_id=CHANNEL),
        Run(id=1, started_at=MOMENT, status=RunStatus.SUCCEEDED, trigger=RunTrigger.SCHEDULE),
        RunAction(run_id=1, id=1, at=MOMENT, action=RunActionKind.ADDED),
        NotificationTarget(
            id=1, name="Phone", url="json://localhost", min_level=0, events=["run_failed"], enabled=True
        ),
    )


def icon_buttons(user) -> list[ui.button]:
    """Return each shown button that has an icon and no text."""
    return [
        element
        for element in client_of(user).elements.values()
        if isinstance(element, ui.button) and element.props.get("icon") and not element.text and element.visible
    ]


def tooltips_of(user, button: ui.element) -> list[str]:
    """Return the text of each tooltip of ``button``: a child of it, or a tooltip that targets it by its id."""
    return [
        element.text
        for element in client_of(user).elements.values()
        if isinstance(element, ui.tooltip)
        and (element.parent_slot.parent is button or element.props.get("target") == f"#{button.html_id}")
    ]


def nameless_buttons(user) -> list[str]:
    """Return the marker of each shown icon-only button that has no ``aria-label``."""
    return [
        str(sorted(element._markers) or element.props["icon"])
        for element in icon_buttons(user)
        if not element.props.get("aria-label")
    ]


@pytest.mark.parametrize("path", PAGES)
async def test_no_icon_button_of_a_page_lacks_a_name(user, stored_data, path):
    await user.open(path)

    nameless = nameless_buttons(user)

    assert nameless == []
    # The walk finds buttons, at least the menu and the theme button of the header.
    assert {"menu", "theme"} <= {marker for element in icon_buttons(user) for marker in element._markers}


async def test_the_rule_editor_has_a_name_for_each_icon_button(user, stored_data):
    await user.open("/rules")
    user.find(marker="rules-table").trigger("rowClick", args=1)
    await eventually(lambda: elements_of(user, "editor"), "The editor")
    await eventually(lambda: elements_of(user, "remove-1"), "The nested group")

    assert nameless_buttons(user) == []
    labels = {marker: element.props["aria-label"] for element in icon_buttons(user) for marker in element._markers}
    assert labels["remove-0"] == "Remove the condition"
    assert labels["remove-1"] == "Remove the group"


async def test_the_run_details_have_a_name_for_the_close_button(user, stored_data):
    await user.open("/history")
    user.find(marker="runs-table").trigger("rowClick", args=1)
    await eventually(lambda: elements_of(user, "details-close"), "The details of the run")

    assert nameless_buttons(user) == []
    assert element_of(user, "details-close").props["aria-label"] == "Close"


async def test_the_buttons_of_a_notification_target_have_a_name(user, stored_data):
    await user.open("/settings")

    assert nameless_buttons(user) == []
    assert element_of(user, "target-1-edit").props["aria-label"] == "Change the name or the URL"
    assert element_of(user, "target-1-delete").props["aria-label"] == "Delete the target"


async def test_the_menu_button_has_a_tooltip_and_a_name(user):
    await user.open("/")

    menu = element_of(user, "menu")
    assert menu.props["aria-label"] == "Menu"
    assert tooltips_of(user, menu) == ["Menu"]


async def test_the_theme_button_name_follows_the_theme(user):
    await user.open("/")
    theme = element_of(user, "theme")
    seen = [theme.props["aria-label"]]

    for _ in range(3):
        user.find(marker="theme").click()
        await eventually(lambda: theme.props["aria-label"] != seen[-1], "The new name of the theme button")
        seen.append(theme.props["aria-label"])

    assert seen[0] == seen[-1]
    assert len(set(seen)) == 3
    # The tooltip says the same as the name.
    assert tooltips_of(user, theme) == [theme.props["aria-label"]]


@pytest.mark.parametrize("path", PAGES)
async def test_the_skip_link_is_the_first_element_of_the_page_and_targets_the_content(user, path):
    await user.open(path)

    (link,) = [element for element in client_of(user).elements.values() if isinstance(element, SkipLink)]
    layout_children = list(client_of(user).layout.default_slot.children)
    assert layout_children[0] is link
    assert link.text == "Skip to the content"
    target = next(
        element for element in client_of(user).elements.values() if f"#{element.html_id}" == link.props["href"]
    )
    # The target is the content column, the one that holds the elements of the page.
    assert "max-w-screen-xl" in target.classes
    assert not isinstance(target, ui.header | ui.left_drawer)
    assert next(target.descendants(), None) is not None


async def test_the_skip_link_target_holds_the_page_content_and_not_the_header(user):
    await user.open("/rules")

    link = next(element for element in client_of(user).elements.values() if isinstance(element, SkipLink))
    target = next(
        element for element in client_of(user).elements.values() if f"#{element.html_id}" == link.props["href"]
    )
    inside = set(target.descendants())
    assert element_of(user, "new-rule") in inside
    assert element_of(user, "menu") not in inside
    assert element_of(user, "navigation") not in inside


async def test_the_edit_button_of_a_rule_row_opens_the_editor_like_a_click_on_the_row(user, stored_data):
    await user.open("/rules")
    table = element_of(user, "rules-table")
    assert table.slots["body-cell-actions"].template.count('aria-label="Edit"') == 1

    user.find(marker="rules-table").trigger("rowButton", args=1)

    await eventually(lambda: elements_of(user, "editor"), "The editor")
    assert element_of(user, "editor-title").text == "Change the rule"
    assert element_of(user, "rule-name").value == "Long videos"


async def test_the_edit_button_of_each_channel_row_opens_the_channel_like_a_click_on_the_row(user, stored_data):
    await user.open("/subscriptions")
    table = element_of(user, "channels-table")
    assert table.slots["body-cell-actions"].template.count('aria-label="Edit"') == 1

    user.find(marker="channels-table").trigger("rowButton", args=CHANNEL)

    await eventually(lambda: element_of(user, "channel-dialog").value, "The open dialog of the channel")
    await eventually(
        lambda: [link.props["href"].endswith(CHANNEL) for link in elements_of(user, "channel-link")] == [True],
        "The link of the channel",
    )


async def test_the_open_button_of_a_run_row_opens_the_run_like_a_click_on_the_row(user, stored_data):
    await user.open("/history")
    table = element_of(user, "runs-table")
    assert table.slots["body-cell-actions"].template.count('aria-label="Open"') == 1

    user.find(marker="runs-table").trigger("rowButton", args=1)

    await eventually(lambda: elements_of(user, "details-title"), "The actions of the run")
    assert element_of(user, "details-title").text == "Actions of run 1"


async def test_the_walk_covers_each_registered_page(user):
    await user.open("/")

    assert sorted(page.path for page in layout.pages()) == sorted(PAGES)
