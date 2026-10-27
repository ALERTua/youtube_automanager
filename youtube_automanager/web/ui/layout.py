"""The shell of each page: the header with the service name, the language button and the theme button, the navigation
drawer and the content frame.

A page module adds its page with the decorator ``register_page``. The navigation drawer shows each registered page,
in the order of the registration. The browser loads the shell once, and a click in the drawer changes only the content
of the frame.

A page keeps the state of its view in the query of its address, for example the filters of a table. The page reads
the query when it builds, writes a new query with ``replace_address``, and follows a later address with the same path,
for example after the Back button of the browser, with an ``AddressFollower``.
"""

import asyncio
import functools
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit
from weakref import WeakKeyDictionary

from nicegui import background_tasks, helpers, ui
from nicegui.client import Client

from youtube_automanager.i18n import _, language, set_language
from youtube_automanager.i18n.setting import NAMES, NEXT_LANGUAGE, save_language
from youtube_automanager.web.services import Services
from youtube_automanager.web.ui.common import accessibility_script, icon_button
from youtube_automanager.web.ui.theme import (
    DARK_MODE,
    ICONS,
    LABELS,
    NEXT_THEME,
    Theme,
    first_paint_style,
    save_theme,
)

type PageBuilder = Callable[[Services], Awaitable[None]]
# The builder of a sub route gets the services as ``services`` and each parameter of its path by its name.
type SubRouteBuilder = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class Page:
    path: str
    # The English text with the mark N_: the shell translates it each time that it shows the label.
    label: str
    # The name of a Material icon.
    icon: str
    build: PageBuilder
    # The other paths of the page with their builders, for example ("/rules/{rule_id}", edit_rule). A part "{name}" of
    # a path matches one segment of the address.
    routes: tuple[tuple[str, SubRouteBuilder], ...] = ()

    def owns(self, path: str) -> bool:
        """Return True when ``path``, without its query and its fragment, is the path of the page or of a sub route."""
        segments = (urlsplit(path).path.rstrip("/") or "/").split("/")
        return any(_matches(pattern, segments) for pattern in (self.path, *(route for route, _build in self.routes)))


def _matches(pattern: str, segments: list[str]) -> bool:
    parts = pattern.split("/")
    return len(parts) == len(segments) and all(
        part == segment or (part.startswith("{") and part.endswith("}") and bool(segment))
        for part, segment in zip(parts, segments, strict=True)
    )


def _check_parameters(pattern: str, build: SubRouteBuilder) -> None:
    path_parameters = set(re.findall(r"\{(\w+)\}", pattern))
    for name, parameter in inspect.signature(build).parameters.items():
        if name == "services" or parameter.annotation in (inspect.Parameter.empty, str):
            continue
        kind = "path parameter" if name in path_parameters else "query parameter"
        raise ValueError(
            f"The builder of the route {pattern} annotates its {kind} {name} as {parameter.annotation!r}. "
            f"A {kind} is a str, and the builder converts it itself."
        )


_PAGES: dict[str, Page] = {}


def register_page(
    path: str, *, label: str, icon: str, routes: Sequence[tuple[str, SubRouteBuilder]] = ()
) -> Callable[[PageBuilder], PageBuilder]:
    """Add the decorated function as the page at ``path``, with an entry in the navigation drawer.

    The function gets the services of the running application and fills the content frame of the page. ``routes``
    gives the page more paths, each with its own builder, and the drawer marks the page as the current one on each of
    them. A second page with the same path replaces the first one.

    A builder gets each path parameter, a part "{name}" of its path, and each query parameter with the name of one of
    its parameters, as the text of the address, and converts it itself. So each parameter of a builder other than
    ``services`` has no annotation or the annotation ``str``, and another annotation raises ValueError here.
    ``ui.sub_pages`` would convert it before the builder starts, and a text such as "abc" for an ``int`` would fail
    there, and the first answer of the page would wait for the end of a build that never starts.

    ``ui.sub_pages`` also builds the page again at each change of a query parameter that the builder has as a parameter.
    A page that writes its view into the query, such as its filters, therefore reads the query with ``address_query``
    and does not have these parameters.
    """
    for pattern, route_build in routes:
        _check_parameters(pattern, route_build)

    def decorator(build: PageBuilder) -> PageBuilder:
        _check_parameters(path, build)
        _PAGES[path] = Page(path=path, label=label, icon=icon, build=build, routes=tuple(routes))
        return build

    return decorator


def address_query(client: Client) -> dict[str, str]:
    """Return the query parameters of the current address of the tab of ``client``. A name that comes twice has its
    last value."""
    return dict(parse_qsl(urlsplit(client.sub_pages_router.current_path).query))


def with_query(path: str, query: Mapping[str, str | None]) -> str:
    """Return ``path`` with the query parameters of ``query`` that have a value, in the order of ``query``."""
    items = [(name, value) for name, value in query.items() if value]
    return f"{path}?{urlencode(items)}" if items else path


def replace_address(client: Client, address: str, *, only_from: str | None = None) -> None:
    """Show ``address`` in the address bar of the tab of ``client``, in place of the current entry of the history of
    the browser. The content of the page stays as it is.

    A page calls this when the user changes its view, for example a filter, so a reload and a shared link show the same
    view. The entry is replaced, so the Back button does not go through each typed letter of a filter. The router of
    the tab takes the address too, and a later navigation compares with it.

    With ``only_from``, the browser replaces the entry only while it still shows that address, without its fragment. A
    page that corrects its address when it builds gives it: the script runs after the connection of the tab, and a quick
    Back can come first. A replace after it would give the old entry the address of this page.
    """
    client.sub_pages_router.current_path = address
    text = json.dumps(address)
    # NiceGUI keeps the address of an entry in its state as "page", and Back opens the address of that state.
    replace = f"history.replaceState({{page: {text}}}, '', (window.path_prefix || '') + {text});"
    if only_from is not None:
        parts = urlsplit(only_from)
        old = json.dumps(parts.path + (f"?{parts.query}" if parts.query else ""))
        replace = f"if (location.pathname + location.search === (window.path_prefix || '') + {old}) {replace}"
    client.run_javascript(replace)


def _tell_followers(followers: list[AddressFollower], address: str) -> None:
    parts = urlsplit(address)
    path = parts.path.rstrip("/") or "/"
    query = dict(parse_qsl(parts.query))
    for follower in list(followers):
        if follower.path == path and not follower.is_deleted:
            follower.tell(query)


class AddressFollower(ui.element):
    """An invisible element that gives each new address of the tab with the path ``path`` to ``handler``, while the
    element exists.

    ``ui.sub_pages`` builds a page again only when the path changes, or a query parameter that the builder has as a
    parameter. Another address with the same path keeps the content: a click in the drawer on the page that shows, a
    navigation of the page itself, or the Back button of the browser to an entry with another query. The page then
    shows the query of the new address with ``handler``, which gets the query parameters. An async handler runs as a
    task in the content of the page. The router calls the handler before it changes the content, and an address with
    another path calls nothing.
    """

    def __init__(self, path: str, handler: Callable[[dict[str, str]], Any]) -> None:
        super().__init__()
        self.set_visibility(False)
        self.path = path
        self.handler = handler
        client = self.client
        followers = _FOLLOWERS.get(client)
        if followers is None:
            followers = _FOLLOWERS[client] = []
            client.sub_pages_router.on_path_changed(functools.partial(_tell_followers, followers))
        followers.append(self)
        self._followers = followers

    def tell(self, query: dict[str, str]) -> None:
        result = self.handler(query)
        if helpers.should_await(result):
            background_tasks.create(result, name=f"follow the address of {self.path}", context=self.parent_slot)

    def _handle_delete(self) -> None:
        if self in self._followers:
            self._followers.remove(self)
        super()._handle_delete()


# The followers of the addresses of each client. NiceGUI has no way to remove a handler of ``on_path_changed``, so the
# router of a client gets one handler, which gives each new address to the followers that exist at that moment.
_FOLLOWERS: WeakKeyDictionary[Client, list[AddressFollower]] = WeakKeyDictionary()

# The name of the flag in the browser that makes the browser warn before a reload or a close of the tab.
UNLOAD_WARNING_FLAG = "__warnBeforeUnload"
# The script that loads the page of the tab again, with its path and its query. After the user chose to stay on the page
# in the warning before a reload, Firefox refuses each later reload of that page by a script, ``location.reload()`` and
# ``history.go(0)`` of ``ui.navigate.reload``, also 20 s later. ``location.replace`` of another address still loads it.
# An address that differs only in its fragment would only scroll, so the address leaves the fragment out. A page that
# opens a part by its fragment, such as a History run, writes the fragment again when it builds.
RELOAD_SCRIPT = "location.replace(location.pathname + location.search);"


def unload_warning_script() -> str:
    """Return the ``<script>`` that makes the browser ask before a reload or a close of the tab, while the flag
    ``UNLOAD_WARNING_FLAG`` is true. The browser shows its own text."""
    return f"""<script>
addEventListener('beforeunload', (event) => {{
  if (!window.{UNLOAD_WARNING_FLAG}) return;
  event.preventDefault();
  event.returnValue = '';
}});
</script>"""


class LeaveCheck(ui.element):
    """An invisible element that decides, while it exists, whether a click in the drawer may leave the content.

    ``may_leave`` runs at each click in the drawer, and the drawer goes to the clicked page only when it returns True,
    for example after a question. ``warn_on_unload`` makes the browser warn before a reload or a close of the tab, for
    example while a form has changes that are not saved. The element ends the warning when it is deleted with the
    content, so the next page never warns.

    The Back and the Forward button of the browser leave without a check: ``ui.sub_pages`` gets the new address after
    the browser changed it (a ``popstate``), and NiceGUI cannot cancel that.
    """

    def __init__(self, may_leave: Callable[[], Awaitable[bool]]) -> None:
        super().__init__()
        self.set_visibility(False)
        self.may_leave = may_leave
        self.warning = False
        client = self.client
        checks = _LEAVE_CHECKS.get(client)
        if checks is None:
            checks = _LEAVE_CHECKS[client] = []
        checks.append(self)
        self._checks = checks

    def warn_on_unload(self, warning: bool) -> None:
        """Turn the warning of the browser before a reload or a close of the tab on or off."""
        if warning != self.warning:
            self.warning = warning
            self.client.run_javascript(f"window.{UNLOAD_WARNING_FLAG} = {json.dumps(warning)};")

    def _handle_delete(self) -> None:
        if self in self._checks:
            self._checks.remove(self)
        self.warn_on_unload(False)
        super()._handle_delete()


# The leave checks of the content of each client.
_LEAVE_CHECKS: WeakKeyDictionary[Client, list[LeaveCheck]] = WeakKeyDictionary()


async def may_leave(client: Client) -> bool:
    """Ask each leave check of the content of ``client``, and return True when each of them lets the user leave."""
    for check in list(_LEAVE_CHECKS.get(client, ())):
        if check.is_deleted or check.parent_slot is None:
            continue
        # A question of the check, for example a dialog, belongs to the content and not to the drawer.
        with check.parent_slot:
            if not await check.may_leave():
                return False
    return True


def pages() -> list[Page]:
    """Return the registered pages in the order of the navigation drawer."""
    return list(_PAGES.values())


def page_of(path: str) -> Page | None:
    """Return the page that shows ``path``, or None when no page has this path."""
    return next((page for page in pages() if page.owns(path)), None)


def _title(page: Page | None) -> str:
    if page is None:
        return _("YouTube AutoManager")
    # Translators: the title of the browser tab. {page} is the name of the page, for example "Dashboard".
    return _("{page} - YouTube AutoManager").format(page=_(page.label))


@contextmanager
def frame(services: Services, theme: Theme) -> Generator[None]:
    """Draw the header and the navigation drawer, and put the elements of the ``with`` block into the content frame.

    The frame stays for the whole visit of the browser tab. When the address changes, the drawer marks the page of the
    new address, and the tab gets the title of that page. ``theme`` is the stored color theme. The head of the page
    paints its background for the moment before the scripts of the page run. The theme button of the header goes to the
    next theme at once, and it stores it.

    The language button names the language that a click gives. A click stores that language, and loads the page again
    at the same path and query, so the frame, the content and the texts of Quasar show the new language. The content
    can ask first, as for a click in the drawer.
    """
    client = ui.context.client
    # The head gets each part once, at the full load: NiceGUI adds a later part to the head and never removes one.
    if style := first_paint_style(theme):
        ui.add_head_html(style)
    ui.add_head_html(accessibility_script())
    ui.add_head_html(unload_warning_script())
    dark = ui.dark_mode(DARK_MODE[theme]).mark("dark-mode")
    chosen = [theme]

    async def switch_theme() -> None:
        theme = NEXT_THEME[chosen[0]]
        chosen[0] = theme
        dark.value = DARK_MODE[theme]
        theme_button.props(f"icon={ICONS[theme]}")
        theme_tooltip.text = _(LABELS[theme])
        theme_button.props["aria-label"] = _(LABELS[theme])
        await asyncio.to_thread(save_theme, services.sessions, theme)

    shown_language = language()

    async def switch_language() -> None:
        # The new load of the page loses the content, for example a form with changes that are not saved.
        if not await may_leave(client):
            return
        # The language of this frame decides the next one, so the click gives the language that the button promised,
        # also after another tab changed it.
        chosen = NEXT_LANGUAGE[shown_language]
        await asyncio.to_thread(save_language, services.sessions, chosen)
        set_language(chosen)
        # The user agreed to leave, so the browser does not ask a second time before the new load.
        for check in list(_LEAVE_CHECKS.get(client, ())):
            check.warn_on_unload(False)
        client.run_javascript(RELOAD_SCRIPT)

    items: dict[str, ui.item] = {}

    async def leave_to(path: str) -> None:
        # The content can ask first, for example a form with changes that are not saved.
        if await may_leave(client):
            with client:
                ui.navigate.to(path)

    with ui.left_drawer(bordered=True).mark("navigation") as drawer, ui.list().classes("w-full"):
        for page in pages():
            # The layout holds a ui.sub_pages, so ui.navigate.to changes only the content and loads no new page.
            with ui.item(on_click=lambda path=page.path: leave_to(path)).mark(f"nav:{page.path}") as item:
                with ui.item_section().props("avatar"):
                    ui.icon(page.icon)
                with ui.item_section():
                    ui.item_label(_(page.label))
            items[page.path] = item

    def show_path(path: str) -> None:
        current = page_of(path)
        for page_path, item in items.items():
            if current is not None and page_path == current.path:
                item.props("active")
            else:
                item.props(remove="active")
        with client:
            ui.page_title(_title(current))

    router = client.sub_pages_router
    show_path(router.current_path)
    router.on_path_changed(show_path)
    with ui.header(elevated=True).classes("items-center"):
        icon_button("menu", _("Menu"), drawer.toggle, props="flat round color=white").mark("menu")
        ui.label(_("YouTube AutoManager")).classes("text-h6")
        ui.space()
        # The button names the language of the click in that language, so a reader of each language finds it.
        target_name = NAMES[NEXT_LANGUAGE[shown_language]]
        icon_button("translate", target_name, switch_language, props="flat round color=white").mark("language")
        with (
            ui.button(icon=ICONS[theme], on_click=switch_theme)
            .props("flat round color=white")
            .mark("theme") as theme_button
        ):
            theme_button.props["aria-label"] = _(LABELS[theme])
            theme_tooltip = ui.tooltip(_(LABELS[theme]))
    content = ui.column().classes("w-full max-w-screen-xl mx-auto p-4 gap-4")
    # The link moves itself to the top of the page, so the keyboard reaches it first.
    skip_link = ui.skip_link(_("Skip to the content"), target=content)
    # The link moves the focus itself, so the click does not follow the link. Else the address keeps a "#" part.
    skip_link.on("click.prevent", js_handler="() => {}")
    with content:
        yield
