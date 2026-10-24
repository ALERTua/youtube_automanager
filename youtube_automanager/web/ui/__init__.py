"""The web pages of the service, made with NiceGUI on the FastAPI application of ``create_app``.

Each page module registers its page with ``layout.register_page`` when it is imported. Import each page module here,
in the order of the navigation drawer.
"""

import asyncio
from collections.abc import Awaitable, Callable

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
from youtube_automanager.web.ui.layout import Page, frame, pages
from youtube_automanager.web.ui.theme import read_theme


def _page_function(page: Page, app: FastAPI) -> Callable[[], Awaitable[None]]:
    async def show_page() -> None:
        # A page request reaches the NiceGUI application, not ``app``, so the services come from ``app`` itself.
        services: Services = app.state.services
        theme = await asyncio.to_thread(read_theme, services.sessions)
        with frame(page, services, theme):
            await page.build(services)

    return show_page


def mount_ui(app: FastAPI) -> None:
    """Add the pages to ``app``, before it starts.

    NiceGUI keeps one global application, which this function mounts at the root of ``app``. The routes of ``app``
    stay first, and NiceGUI answers each other path. NiceGUI starts before the lifespan of ``app`` and stops after
    it. A second call in the same process needs a reset of NiceGUI first, which only tests do.
    """
    for page in pages():
        ui.page(page.path)(_page_function(page, app))
    # NiceGUI logs the exception of a page handler with its trace, and this handler tells the user of the page. The reset
    # of NiceGUI between two tests removes each added handler, so the handlers do not pile up.
    nicegui_app.on_exception(notify_failure)
    # None is the auto mode of Quasar, which follows the system of the device until a page sets the stored theme.
    ui.run_with(app, title=_("YouTube AutoManager"), show_welcome_message=False, dark=None)
