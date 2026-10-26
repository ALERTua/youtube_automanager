"""Tests of the accessibility of the admin pages: each icon-only button has a name, and each clickable table row has a
keyboard path. The tests read the elements of the page. The cells of a table slot do not render in the user
simulation, so a test of such a cell reads the template of the slot.
"""

import json
from datetime import UTC, datetime

import pytest
from nicegui import ui
from nicegui.elements.skip_link import SkipLink
from ui_support import client_of, element_of, elements_of, eventually, set_value, store

from youtube_automanager import i18n
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
from youtube_automanager.web.ui.common import quasar_names_script

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
    # The tooltip says the same as the name, for a sighted user of the mouse.
    assert [(element.props["aria-label"], tooltips_of(user, element)) for element in icon_buttons(user)] == [
        (element.props["aria-label"], [element.props["aria-label"]]) for element in icon_buttons(user)
    ]


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
    assert table.slots["body-cell-actions"].template.count('icon="edit"') == 1

    user.find(marker="rules-table").trigger("rowButton", args=1)

    await eventually(lambda: elements_of(user, "editor"), "The editor")
    assert element_of(user, "editor-title").text == "Change the rule"
    assert element_of(user, "rule-name").value == "Long videos"


async def test_the_edit_button_of_each_channel_row_opens_the_channel_like_a_click_on_the_row(user, stored_data):
    await user.open("/subscriptions")
    table = element_of(user, "channels-table")
    assert table.slots["body-cell-actions"].template.count('aria-label="Edit"') == 1
    assert table.slots["body-cell-actions"].template.count('icon="edit"') == 1

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
    assert table.slots["body-cell-actions"].template.count('icon="open_in_new"') == 1

    user.find(marker="runs-table").trigger("rowButton", args=1)

    await eventually(lambda: elements_of(user, "details-title"), "The actions of the run")
    assert element_of(user, "details-title").text == "Actions of run 1"


async def test_the_walk_covers_each_registered_page(user):
    await user.open("/")

    assert sorted(page.path for page in layout.pages()) == sorted(PAGES)


# The focus after a rebuild.


@pytest.fixture
def scripts(monkeypatch) -> list[str]:
    """The JavaScript that the pages send to the browser, which the user simulation does not run."""
    codes: list[str] = []
    monkeypatch.setattr(ui, "run_javascript", lambda code, **kwargs: codes.append(code))
    return codes


def selector_in(code: str, selector: str) -> bool:
    """True when the script looks for ``selector`` as its first choice."""
    return f"document.querySelector({json.dumps(selector)})" in code


async def test_the_focus_goes_back_to_the_edit_button_of_the_rule_when_the_editor_closes(user, stored_data, scripts):
    await user.open("/rules")
    user.find(marker="rules-table").trigger("rowButton", args=1)
    await eventually(lambda: elements_of(user, "editor"), "The editor")
    scripts.clear()

    user.find(marker="editor-cancel").click()

    await eventually(lambda: scripts, "The move of the focus")
    (code,) = scripts
    assert selector_in(code, '[data-row-button="1"]')
    # The rule can be gone, for example after a delete, so the New rule button is the second choice.
    assert json.dumps(f"#{element_of(user, 'new-rule').html_id}") in code
    # The dialog is still closing, so the browser waits a moment before the first try.
    assert code.rstrip().endswith("}, 200);")


async def test_the_focus_goes_to_new_rule_when_the_editor_of_a_new_rule_closes(user, stored_data, scripts):
    await user.open("/rules")
    user.find(marker="new-rule").click()
    await eventually(lambda: elements_of(user, "editor"), "The editor")
    scripts.clear()

    user.find(marker="editor-cancel").click()

    await eventually(lambda: scripts, "The move of the focus")
    (code,) = scripts
    assert selector_in(code, f"#{element_of(user, 'new-rule').html_id}")
    assert "data-row-button" not in code


async def test_the_focus_goes_to_the_card_when_a_run_opens_and_back_to_its_button_when_it_closes(
    user, stored_data, scripts
):
    await user.open("/history")
    scripts.clear()

    user.find(marker="runs-table").trigger("rowButton", args=1)

    await eventually(lambda: scripts, "The move of the focus")
    (code,) = scripts
    assert selector_in(code, "[data-run-details]")
    card = element_of(user, "details")
    # A card is no control, so only a tabindex lets a script give it the focus.
    assert (str(card.props["tabindex"]), "data-run-details" in card.props) == ("-1", True)
    scripts.clear()

    user.find(marker="details-close").click()

    await eventually(lambda: scripts, "The move of the focus")
    (code,) = scripts
    assert selector_in(code, '[data-row-button="1"]')


@pytest.mark.parametrize("event", ["rowButton", "rowClick"])
async def test_the_focus_goes_back_to_the_row_button_when_the_channel_dialog_closes(user, stored_data, scripts, event):
    await user.open("/subscriptions")
    assert element_of(user, "channels-table").props["data-row-scope"] == "channels"
    user.find(marker="channels-table").trigger(event, args=CHANNEL)
    await eventually(lambda: element_of(user, "channel-dialog").value, "The open dialog")
    scripts.clear()

    with client_of(user):
        element_of(user, "channel-dialog").close()

    await eventually(lambda: scripts, "The move of the focus")
    (code,) = scripts
    # The page has two tables of rows, so the scope tells which table gets the focus.
    assert selector_in(code, f'[data-row-scope="channels"] [data-row-button="{CHANNEL}"]')


# The skip link.


async def test_the_skip_link_moves_the_focus_without_a_new_address(user):
    await user.open("/")

    link = next(element for element in client_of(user).elements.values() if isinstance(element, SkipLink))
    listeners = [(listener.type, listener.js_handler) for listener in link._event_listeners.values()]
    # The link of NiceGUI moves the focus itself. The second listener stops the browser from adding "#c44" to the URL.
    assert ("click.prevent", "() => {}") in listeners
    assert len(listeners) == 2


# The names of the fields.


def unnamed_fields(user) -> list[str]:
    """Return the marker of each field that has no visible label and no ``aria-label``."""
    return [
        str(element._markers or element.props.get("type"))
        for element in client_of(user).elements.values()
        if isinstance(element, ui.select | ui.input | ui.textarea | ui.number)
        and not (element.props.get("label") or element.props.get("aria-label"))
    ]


def aria_labels(user) -> dict[str, str]:
    """Return the ``aria-label`` of each marked element, by its marker."""
    return {
        marker: element.props["aria-label"]
        for element in client_of(user).elements.values()
        for marker in element._markers
        if "aria-label" in element.props
    }


async def test_each_field_of_the_rule_editor_has_a_name(user, stored_data):
    await user.open("/rules")
    user.find(marker="rules-table").trigger("rowButton", args=1)
    await eventually(lambda: elements_of(user, "editor"), "The editor")
    # One leaf for each kind of value: a text, a channel, some channels, a duration, a date, a live status and a Short.
    leaves = [
        ("title", None),
        ("channel_id", "=="),
        ("channel_id", "in"),
        ("duration_s", None),
        ("upload_date", None),
        ("live_status", None),
        ("is_short", None),
    ]
    for number, (field, operator) in enumerate(leaves):
        user.find(marker="add-condition").click()
        index = number + 2
        await eventually(lambda index=index: elements_of(user, f"field-{index}"), f"The condition {index}")
        set_value(user, f"field-{index}", field)
        if operator is not None:
            await eventually(lambda index=index: elements_of(user, f"operator-{index}"), f"The operator {index}")
            set_value(user, f"operator-{index}", operator)
    await eventually(lambda: "value-8" in aria_labels(user), "The select of the last condition")

    assert unnamed_fields(user) == []
    names = aria_labels(user)
    assert names["group-kind"] == "Group type"
    assert names["field-0"] == "Field"
    assert names["operator-0"] == "Operator"
    assert names["value-3"] == "Channel"
    assert names["value-4"] == "Channels"
    assert names["value-7"] == "Live status"
    assert names["value-8"] == "Short"


async def test_the_json_field_of_the_rule_editor_has_a_name(user, sessions):
    store(
        sessions,
        Rule(
            id=1,
            name="Hand-written",
            enabled=True,
            position=1,
            condition=LONG,
            target_playlist_id="WL",
            no_shorts=False,
            created_at=MOMENT,
            updated_at=MOMENT,
        ),
        Playlist(playlist_id="WL", title="Watch later", is_own=True),
    )
    await user.open("/rules")
    user.find(marker="rules-table").trigger("rowButton", args=1)
    await eventually(lambda: elements_of(user, "rule-json"), "The JSON field")

    assert element_of(user, "rule-json").props["aria-label"] == "Condition as JsonLogic"
    assert unnamed_fields(user) == []


@pytest.mark.parametrize("path", ["/", "/history", "/scheduler", "/settings", "/subscriptions"])
async def test_each_field_of_the_other_pages_has_a_name(user, stored_data, path):
    await user.open(path)

    assert unnamed_fields(user) == []


async def test_the_url_of_a_new_target_has_a_toggle_with_a_name_a_state_and_a_key(user, stored_data):
    await user.open("/settings")
    user.find(marker="target-add").click()
    await eventually(lambda: elements_of(user, "target-url-toggle"), "The toggle")
    toggle = element_of(user, "target-url-toggle")
    field = element_of(user, "target-url")

    # It is a real button, so the keyboard reaches it and a screen reader says its name and its state.
    assert isinstance(toggle, ui.button)
    assert (toggle.props["aria-label"], toggle.props["aria-pressed"], toggle.props["icon"]) == (
        "Show the URL",
        "false",
        "visibility_off",
    )
    assert field.props["type"] == "password"

    user.find(marker="target-url-toggle").click()
    await eventually(lambda: field.props["type"] == "text", "The shown URL")
    assert (toggle.props["aria-pressed"], toggle.props["icon"]) == ("true", "visibility")

    user.find(marker="target-url-toggle").click()
    await eventually(lambda: field.props["type"] == "password", "The hidden URL")
    assert (toggle.props["aria-pressed"], toggle.props["icon"]) == ("false", "visibility_off")


async def test_the_url_of_a_stored_target_shows_in_plain_text_without_a_toggle(user, stored_data):
    await user.open("/settings")
    user.find(marker="target-1-edit").click()
    await eventually(lambda: elements_of(user, "target-url"), "The editor")

    assert elements_of(user, "target-url-toggle") == []


# The parts of Quasar that no prop names.


@pytest.mark.parametrize("path", PAGES)
async def test_each_page_names_the_select_of_a_table_and_the_buttons_of_an_upload_in_the_browser(user, path):
    await user.open(path)

    head = client_of(user).head_html
    assert ("q-table__select" in head, "q-uploader" in head) == (True, True)


def test_the_labels_of_the_upload_buttons_go_through_the_translation(monkeypatch):
    class Marking:
        def gettext(self, message):
            return f"<<{message}>>"

    monkeypatch.setattr(i18n, "_translations", Marking())

    script = quasar_names_script()

    for label in ["Add a file", "Remove all files", "Upload the files", "Stop the upload", "Remove the file"]:
        assert f"<<{label}>>" in script
    # The select of the table gets the text before it, which is a text of Quasar in the language of the page.
    assert "previousElementSibling" in script
