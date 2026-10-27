"""The shell of each page: the header with the service name and the theme button, the navigation drawer and the content
frame.

A page module adds its page with the decorator ``register_page``. The navigation drawer shows each registered page,
in the order of the registration. The browser loads the shell once, and a click in the drawer changes only the content
of the frame.
"""

import asyncio
from collections.abc import Awaitable, Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from nicegui import ui

from youtube_automanager.i18n import _
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


_PAGES: dict[str, Page] = {}


def register_page(
    path: str, *, label: str, icon: str, routes: Sequence[tuple[str, SubRouteBuilder]] = ()
) -> Callable[[PageBuilder], PageBuilder]:
    """Add the decorated function as the page at ``path``, with an entry in the navigation drawer.

    The function gets the services of the running application and fills the content frame of the page. ``routes``
    gives the page more paths, each with its own builder, and the drawer marks the page as the current one on each of
    them. A second page with the same path replaces the first one.
    """

    def decorator(build: PageBuilder) -> PageBuilder:
        _PAGES[path] = Page(path=path, label=label, icon=icon, build=build, routes=tuple(routes))
        return build

    return decorator


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
    paints its background for the moment before the scripts of the page run. The button of the header goes to the next
    theme at once, and it stores it.
    """
    client = ui.context.client
    # The head gets each part once, at the full load: NiceGUI adds a later part to the head and never removes one.
    if style := first_paint_style(theme):
        ui.add_head_html(style)
    ui.add_head_html(accessibility_script())
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

    items: dict[str, ui.item] = {}
    with ui.left_drawer(bordered=True).mark("navigation") as drawer, ui.list().classes("w-full"):
        for page in pages():
            # The layout holds a ui.sub_pages, so ui.navigate.to changes only the content and loads no new page.
            with ui.item(on_click=lambda path=page.path: ui.navigate.to(path)).mark(f"nav:{page.path}") as item:
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
