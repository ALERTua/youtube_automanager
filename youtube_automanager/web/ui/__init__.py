"""The web pages of the service, made with NiceGUI on the FastAPI application of ``create_app``.

Each page module registers its page with ``layout.register_page`` when it is imported. Import each page module here,
in the order of the navigation drawer.
"""

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI
from nicegui import app as nicegui_app
from nicegui import ui

from youtube_automanager.i18n import _
from youtube_automanager.web.services import Services

# The drawer lists the pages in the order of these imports, so the sorting of the imports stays off here.
# isort: off
from youtube_automanager.web.ui import dashboard  # noqa: F401
from youtube_automanager.web.ui import rules_page  # noqa: F401
from youtube_automanager.web.ui import subscriptions_page  # noqa: F401
from youtube_automanager.web.ui import scheduler_page  # noqa: F401
from youtube_automanager.web.ui import history_page  # noqa: F401
from youtube_automanager.web.ui import settings_page  # noqa: F401

# isort: on
from youtube_automanager.web.ui.errors import notify_failure
from youtube_automanager.web.ui.layout import SubRouteBuilder, frame, pages
from youtube_automanager.web.ui.theme import read_theme

# The path of the shell route that answers each path that no other route answers.
SHELL_ROUTE = "/{path:path}"


def _with_end_signal(build: SubRouteBuilder, built: asyncio.Event, known: Callable[[str], bool]) -> SubRouteBuilder:
    # ui.sub_pages gives a builder its arguments by the names of its parameters, and wraps keeps them visible.
    @functools.wraps(build)
    async def run(*args: Any, **kwargs: Any) -> None:
        try:
            # An unknown path matches "/" or the path of a page as a prefix, so ui.sub_pages starts the builder of that
            # page. Its 404 text does not stop the task, and the page would grow under the text, so the builder
            # starts only for a path that a page owns.
            if known(ui.context.client.sub_pages_router.current_path):
                await build(*args, **kwargs)
        finally:
            built.set()

    return run


def _shell(app: FastAPI) -> Callable[[], Awaitable[None]]:
    pages_now = pages()

    def known(path: str) -> bool:
        return any(page.owns(path) for page in pages_now)

    async def show_shell() -> None:
        # A page request reaches the NiceGUI application, not ``app``, so the services come from ``app`` itself.
        services: Services = app.state.services
        theme = await asyncio.to_thread(read_theme, services.sessions)
        built = asyncio.Event()
        routes = {
            path: _with_end_signal(build, built, known)
            for page in pages_now
            for path, build in ((page.path, page.build), *page.routes)
        }
        with frame(services, theme):
            ui.sub_pages(routes, data={"services": services})
        # ui.sub_pages runs an async builder as a task. The first HTML waits for it, so the browser gets the page with
        # its data, as before the shell. A later change of the address builds the content in the open page.
        # The wait ends when the wrapper of the matched route ends. ui.sub_pages starts that wrapper only after it
        # converted the path and query parameters (PageArguments.build_kwargs), and a failed conversion would leave the
        # wait to the response_timeout of NiceGUI. So register_page refuses a parameter of another type than str. An
        # unknown path gets no wait: the wrapper builds nothing, and NiceGUI shows the 404 text.
        if known(ui.context.client.sub_pages_router.current_path):
            await built.wait()

    return show_shell


def mount_ui(app: FastAPI) -> None:
    """Add the pages to ``app``, before it starts.

    NiceGUI keeps one global application, which this function mounts at the root of ``app``. The routes of ``app``
    stay first, and NiceGUI answers each other path. NiceGUI starts before the lifespan of ``app`` and stops after
    it. A second call in the same process needs a reset of NiceGUI first, which only tests do.

    One shell route answers each page: the browser loads the frame once, and ``ui.sub_pages`` in the frame changes the
    content when the address changes. An unknown path gets the frame with the 404 text of ``ui.sub_pages``.
    """
    shell = _shell(app)
    ui.page("/")(shell)
    # NiceGUI adds the route /favicon.ico when it starts, and a route that comes after the shell route never answers.
    # The handlers of the start run after that, so the shell route comes last.
    nicegui_app.on_startup(lambda: ui.page(SHELL_ROUTE)(shell))
    # NiceGUI logs the exception of a page handler with its trace, and this handler tells the user of the page. The reset
    # of NiceGUI between two tests removes each added handler, so the handlers do not pile up.
    nicegui_app.on_exception(notify_failure)
    # None is the auto mode of Quasar, which follows the system of the device until a page sets the stored theme.
    ui.run_with(app, title=_("YouTube AutoManager"), show_welcome_message=False, dark=None)
