"""The shell of the pages: one route answers each page, and a click in the drawer changes only the content."""

import asyncio
import json
import re
from unittest.mock import Mock

import pytest
from nicegui import Client, ui
from nicegui import __version__ as nicegui_version
from ui_support import (
    client_of,
    element_of,
    elements_of,
    eventually,
    running_service,
    service_with_pages,
    text_of,
)

from youtube_automanager.db.models import RunTrigger
from youtube_automanager.i18n import N_
from youtube_automanager.web.ui import dashboard, history_page, layout
from youtube_automanager.web.ui.common import accessibility_script
from youtube_automanager.web.ui.live_runs import RunFollower

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]

# How long a test waits for a call that must not come.
SETTLE_SECONDS = 0.3
PAGES = ["/", "/rules", "/subscriptions", "/scheduler", "/history", "/settings"]


def active_items(user) -> list[str]:
    return [path for path in PAGES if "active" in element_of(user, f"nav:{path}").props]


def subscribers(services) -> list[int]:
    events = services.run_events
    return [len(events.started.callbacks), len(events.progress.callbacks), len(events.finished.callbacks)]


async def test_a_click_in_the_drawer_changes_the_content_in_the_same_client(user):
    client = await user.open("/")
    await user.should_see(marker="ytdlp-version")

    for path, marker in [("/rules", "new-rule"), ("/history", "refresh"), ("/", "ytdlp-version")]:
        user.find(marker=f"nav:{path}").click()
        await user.should_see(marker=marker)
        assert user.client is client

    # The frame and its head are made once: a change of the content adds nothing to the head.
    assert client.head_html.count(accessibility_script()) == 1
    assert len(elements_of(user, "navigation")) == 1


async def test_the_active_drawer_item_and_the_title_follow_a_navigation(user):
    client = await user.open("/scheduler")

    assert active_items(user) == ["/scheduler"]
    assert client.title == "Scheduler - YouTube AutoManager"

    user.find(marker="nav:/subscriptions").click()

    await eventually(lambda: active_items(user) == ["/subscriptions"], "The active item of the drawer")
    assert client.title == "Subscriptions - YouTube AutoManager"


async def test_each_path_of_a_page_marks_its_drawer_item(nicegui_globals, settings, fake_starter, monkeypatch):
    monkeypatch.setattr(layout, "_PAGES", dict(layout._PAGES))

    async def show_item(services, item_id: str):
        ui.label(f"item {int(item_id)} in {services.settings.tz}").mark("item")

    @layout.register_page("/items", label=N_("Items"), icon="list", routes=[("/items/{item_id}", show_item)])
    async def items_page(services):
        ui.label("all items").mark("items")

    async with running_service(settings, fake_starter) as user:
        client = await user.open("/items/7?sort=name")

        assert text_of(user, "item") == "item 7 in Europe/Kyiv"
        assert "active" in element_of(user, "nav:/items").props
        assert client.title == "Items - YouTube AutoManager"

        user.find(marker="nav:/items").click()

        await user.should_see(marker="items")
        await user.should_not_see(marker="item")


@pytest.mark.parametrize("annotation", [int, float, int | None, "int"])
def test_a_sub_route_refuses_a_path_parameter_of_another_type_than_str(monkeypatch, annotation):
    monkeypatch.setattr(layout, "_PAGES", dict(layout._PAGES))

    async def show_item(services, item_id):
        pass

    show_item.__annotations__["item_id"] = annotation

    with pytest.raises(ValueError, match="path parameter item_id"):
        layout.register_page("/items", label=N_("Items"), icon="list", routes=[("/items/{item_id}", show_item)])
    assert "/items" not in layout._PAGES


@pytest.mark.parametrize("annotation", [int, float, int | None, "int"])
def test_a_page_refuses_a_query_parameter_of_another_type_than_str(monkeypatch, annotation):
    monkeypatch.setattr(layout, "_PAGES", dict(layout._PAGES))

    async def items_page(services, page=None):
        pass

    items_page.__annotations__["page"] = annotation

    # ui.sub_pages would convert "?page=abc" before the builder starts, and the first answer would wait 3 s for a 500.
    with pytest.raises(ValueError, match="query parameter page"):
        layout.register_page("/items", label=N_("Items"), icon="list")(items_page)
    assert "/items" not in layout._PAGES


def test_a_sub_route_refuses_a_query_parameter_of_another_type_than_str(monkeypatch):
    monkeypatch.setattr(layout, "_PAGES", dict(layout._PAGES))

    async def show_item(services, item_id: str, sort: int = 0):
        pass

    with pytest.raises(ValueError, match="query parameter sort"):
        layout.register_page("/items", label=N_("Items"), icon="list", routes=[("/items/{item_id}", show_item)])
    assert "/items" not in layout._PAGES


def test_a_sub_route_takes_a_path_parameter_without_a_type_or_as_str(monkeypatch):
    monkeypatch.setattr(layout, "_PAGES", dict(layout._PAGES))

    async def plain(services, item_id):
        pass

    async def text(services, item_id: str, sort: str = ""):
        pass

    @layout.register_page(
        "/items", label=N_("Items"), icon="list", routes=[("/items/{item_id}", plain), ("/items/{item_id}/x", text)]
    )
    async def items_page(services):
        pass

    assert [path for path, _build in layout._PAGES["/items"].routes] == ["/items/{item_id}", "/items/{item_id}/x"]


def test_a_page_owns_its_path_and_its_sub_routes():
    async def build(services):
        pass

    page = layout.Page(path="/rules", label="Rules", icon="rule", build=build, routes=(("/rules/{rule_id}", build),))

    assert [
        page.owns(path)
        for path in ["/rules", "/rules/", "/rules?q=1#top", "/rules/5", "/rules/5/more", "/rules//", "/", "/rule"]
    ] == [True, True, True, True, False, True, False, False]


async def test_an_unknown_path_gets_the_shell_with_the_404_text(user):
    answer = await user.http_client.get("/no-such-page")

    assert answer.status_code == 404
    assert answer.headers["X-Nicegui-Content"] == "page"
    assert "404: sub page /no-such-page not found" in answer.text
    assert "Skip to the content" in answer.text
    assert "<title>YouTube AutoManager</title>" in answer.text


def marked(client, marker) -> list[ui.element]:
    return [element for element in client.elements.values() if marker in element._markers]


@pytest.mark.parametrize(("path", "marker"), [("/no-such-page", "ytdlp-version"), ("/rules/xyz/more", "no-such-rule")])
async def test_a_full_load_of_an_unknown_path_shows_only_the_404_text(
    nicegui_globals, settings, fake_starter, path, marker
):
    async with running_service(settings, fake_starter) as user:
        # User.open expects HTTP 200, so the test finds the client of the answer by its id.
        answer = await user.http_client.get(path)
        assert answer.status_code == 404
        match = re.search(r"'client_id': '([0-9a-f-]+)'", answer.text)
        assert match is not None
        client = Client.instances[match.group(1)]
        # The builder of the page whose path is a prefix of the address would add its elements within this pause.
        await asyncio.sleep(0.5)
        found = marked(client, marker)

    assert found == []


@pytest.mark.parametrize(
    ("start", "path", "marker"),
    [("/rules", "/no-such-page", "ytdlp-version"), ("/", "/rules/xyz/more", "no-such-rule")],
)
async def test_a_navigation_to_an_unknown_path_shows_only_the_404_text(
    nicegui_globals, settings, fake_starter, start, path, marker
):
    async with running_service(settings, fake_starter) as user:
        await user.open(start)

        user.navigate.to(path)

        await user.should_see(f"404: sub page {path} not found")
        await asyncio.sleep(0.5)
        found = marked(client_of(user), marker)

    assert found == []


async def test_a_navigation_to_an_unknown_path_shows_the_404_text_in_the_shell(user):
    client = await user.open("/rules")

    user.navigate.to("/no-such-page")

    await user.should_see("404: sub page /no-such-page not found")
    assert user.client is client
    assert active_items(user) == []
    assert client.title == "YouTube AutoManager"
    await user.should_see(marker="navigation")


async def test_a_timer_of_a_page_stops_after_a_navigation(user, monkeypatch):
    reads = []
    take_snapshot = dashboard.take_snapshot

    async def counting(services):
        reads.append(1)
        return await take_snapshot(services)

    monkeypatch.setattr(dashboard, "take_snapshot", counting)
    monkeypatch.setattr(dashboard, "REFRESH_SECONDS", 0.05)
    await user.open("/")
    await eventually(lambda: len(reads) >= 3, "The reads of the dashboard timer")

    user.find(marker="nav:/rules").click()
    await user.should_see(marker="new-rule")
    after_navigation = len(reads)
    await asyncio.sleep(SETTLE_SECONDS)

    assert len(reads) == after_navigation


async def test_the_old_page_stops_getting_run_events_after_a_navigation(
    nicegui_globals, settings, fake_starter, monkeypatch
):
    reads = []
    read_runs = history_page.read_runs

    def counting(*args):
        reads.append(1)
        return read_runs(*args)

    monkeypatch.setattr(history_page, "read_runs", counting)
    async with service_with_pages(settings, fake_starter) as (user, services):
        await user.open("/history")
        (follower,) = elements_of_kind(user, RunFollower)
        assert subscribers(services) == [1, 1, 1]
        # The handler of the history page reads the runs at the start of a run.
        services.run_events.started.emit(1, RunTrigger.MANUAL)
        await eventually(lambda: len(reads) == 2, "The read of the history page after the start of a run")

        user.find(marker="nav:/subscriptions").click()
        await user.should_see(marker="channels-table")

        assert follower.is_deleted
        assert subscribers(services) == [0, 0, 0]
        services.run_events.started.emit(2, RunTrigger.MANUAL)
        await asyncio.sleep(SETTLE_SECONDS)
        assert len(reads) == 2


async def test_the_rules_page_ends_its_subscriptions_when_the_drawer_leaves_it(nicegui_globals, settings, fake_starter):
    async with service_with_pages(settings, fake_starter) as (user, services):
        await user.open("/subscriptions")

        # A second visit must not add to the subscriptions of the first one.
        for _visit in range(2):
            user.find(marker="nav:/rules").click()
            await user.should_see(marker="new-rule")
            # The apply banner of the rules page, and the follower of the runs that reads the column Added again.
            assert subscribers(services) == [1, 2, 2]
            assert len(services.applies.changed.callbacks) == 1
            # The editor has the apply banner and the apply dialog, and the rules page ends its banner.
            user.find(marker="new-rule").click()
            await user.should_see(marker="editor")
            assert subscribers(services) == [0, 2, 2]
            assert len(services.applies.changed.callbacks) == 2

            user.find(marker="nav:/subscriptions").click()
            await user.should_see(marker="channels-table")
            assert subscribers(services) == [0, 0, 0]
            assert services.applies.changed.callbacks == []


async def test_a_navigation_adds_no_delete_handler_to_the_client(nicegui_globals, settings, fake_starter):
    async with running_service(settings, fake_starter) as user:
        client = await user.open("/")
        await user.should_see(marker="ytdlp-version")
        first = len(client.delete_handlers)

        for _round in range(3):
            for path, marker in [("/rules", "new-rule"), ("/history", "refresh"), ("/", "ytdlp-version")]:
                user.find(marker=f"nav:{path}").click()
                await user.should_see(marker=marker)

        assert len(client.delete_handlers) == first


async def test_the_address_followers_of_the_pages_end_with_their_content(nicegui_globals, settings, fake_starter):
    async with running_service(settings, fake_starter) as user:
        client = await user.open("/rules")
        router = client.sub_pages_router
        handlers = len(router._path_changed_handlers)

        for _round in range(3):
            for path, marker in [("/history", "refresh"), ("/rules", "new-rule"), ("/", "ytdlp-version")]:
                user.find(marker=f"nav:{path}").click()
                await user.should_see(marker=marker)

        # The router has one handler for the followers of the client, and the page left them all.
        assert len(router._path_changed_handlers) == handlers
        assert layout._FOLLOWERS[client] == []
        user.find(marker="nav:/history").click()
        await user.should_see(marker="refresh")
        assert [follower.path for follower in layout._FOLLOWERS[client]] == ["/history"]


async def test_the_leave_check_of_the_editor_ends_with_its_content(nicegui_globals, settings, fake_starter):
    async with running_service(settings, fake_starter) as user:
        client = await user.open("/rules")

        for _round in range(3):
            user.find(marker="new-rule").click()
            await eventually(lambda: elements_of(user, "editor"), "The editor")
            assert len(layout._LEAVE_CHECKS[client]) == 1
            # The editor has no changes, so a click in the drawer leaves at once.
            user.find(marker="nav:/history").click()
            await user.should_see(marker="refresh")
            # The check of the closed editor is gone, so the checks do not pile up in a tab that stays open.
            assert layout._LEAVE_CHECKS[client] == []
            user.find(marker="nav:/rules").click()
            await user.should_see(marker="new-rule")


async def test_the_first_html_holds_the_content_that_an_async_builder_shows_after_an_await(
    nicegui_globals, settings, fake_starter, monkeypatch
):
    monkeypatch.setattr(layout, "_PAGES", dict(layout._PAGES))

    @layout.register_page("/slow", label=N_("Slow"), icon="hourglass_empty")
    async def slow_page(services):
        await asyncio.sleep(0.05)
        ui.label("read after the wait").mark("slow")

    async with running_service(settings, fake_starter) as user:
        answer = await user.http_client.get("/slow")

    assert answer.status_code == 200
    assert "read after the wait" in answer.text


def elements_of_kind(user, kind) -> list[ui.element]:
    return [element for element in user.client.elements.values() if isinstance(element, kind)]


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/health", "application/json"),
        ("/api/status", "application/json"),
        ("/api/no-such-path", "application/json"),
        ("/docs", "text/html"),
        ("/openapi.json", "application/json"),
        ("/favicon.ico", "image/"),
        (f"/_nicegui/{nicegui_version}/static/nicegui.css", "text/css"),
    ],
)
async def test_the_shell_route_answers_no_path_of_another_route(user, path, kind):
    answer = await user.http_client.get(path)

    assert answer.headers["content-type"].startswith(kind)
    assert "X-Nicegui-Content" not in answer.headers


def test_a_write_of_the_address_in_place_gives_the_entry_the_new_address_as_its_state():
    sent: list[str] = []
    client = Mock(run_javascript=sent.append)
    address = '/rules?q="x"&state=disabled'

    layout.replace_address(client, address)

    assert client.sub_pages_router.current_path == address
    # Back opens the address of the state of the entry, so a state of the old address would open the old rules.
    text = json.dumps(address)
    assert sent == [f"history.replaceState({{page: {text}}}, '', (window.path_prefix || '') + {text});"]


def test_a_write_of_the_address_from_an_old_address_replaces_only_while_the_browser_shows_it():
    sent: list[str] = []
    client = Mock(run_javascript=sent.append)

    layout.replace_address(client, "/history?run=3#run-actions", only_from="/history?run=3#old")

    (code,) = sent
    assert code.startswith(
        """if (location.pathname + location.search === (window.path_prefix || '') + "/history?run=3") """
    )
    assert code.endswith(
        """history.replaceState({page: "/history?run=3#run-actions"}, '', (window.path_prefix || '') + "/history?run=3#run-actions");"""
    )
