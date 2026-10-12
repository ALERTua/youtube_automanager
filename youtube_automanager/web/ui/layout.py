"""The shell of each page: the header with the service name, the navigation drawer and the content frame.

A page module adds its page with the decorator ``register_page``. The navigation drawer shows each registered page,
in the order of the registration.
"""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from nicegui import ui

from youtube_automanager.i18n import _
from youtube_automanager.web.services import Services

type PageBuilder = Callable[[Services], Awaitable[None]]


@dataclass(frozen=True)
class Page:
    path: str
    # The English text with the mark N_: the shell translates it each time that it shows the label.
    label: str
    # The name of a Material icon.
    icon: str
    build: PageBuilder


_PAGES: dict[str, Page] = {}


def register_page(path: str, *, label: str, icon: str) -> Callable[[PageBuilder], PageBuilder]:
    """Add the decorated function as the page at ``path``, with an entry in the navigation drawer.

    The function gets the services of the running application and fills the content frame of the page.
    A second page with the same path replaces the first one.
    """

    def decorator(build: PageBuilder) -> PageBuilder:
        _PAGES[path] = Page(path=path, label=label, icon=icon, build=build)
        return build

    return decorator


def pages() -> list[Page]:
    """Return the registered pages in the order of the navigation drawer."""
    return list(_PAGES.values())


@contextmanager
def frame(current: Page) -> Iterator[None]:
    """Draw the header and the navigation drawer, and put the elements of the ``with`` block into the content frame."""
    # Translators: the title of the browser tab. {page} is the name of the page, for example "Dashboard".
    title = _("{page} - YouTube AutoManager").format(page=_(current.label))
    ui.page_title(title)
    with ui.left_drawer(bordered=True).mark("navigation") as drawer, ui.list().classes("w-full"):
        for page in pages():
            with ui.item(on_click=lambda path=page.path: ui.navigate.to(path)).mark(f"nav:{page.path}") as item:
                with ui.item_section().props("avatar"):
                    ui.icon(page.icon)
                with ui.item_section():
                    ui.item_label(_(page.label))
            if page.path == current.path:
                item.props("active")
    with ui.header(elevated=True).classes("items-center"):
        ui.button(icon="menu", on_click=drawer.toggle).props("flat round color=white")
        ui.label(_("YouTube AutoManager")).classes("text-h6")
    with ui.column().classes("w-full max-w-screen-xl mx-auto p-4 gap-4"):
        yield
