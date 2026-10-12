"""Helpers of the tests that run the whole service with its pages. The fixtures of these tests are in ``conftest.py``."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
from nicegui import ElementFilter, ui
from nicegui.testing import User

from youtube_automanager.web.app import create_app
from youtube_automanager.web.ui import mount_ui

WAIT_SECONDS = 10


def store(sessions, *rows):
    with sessions.begin() as session:
        session.add_all(rows)


@asynccontextmanager
async def running_service(settings, start) -> AsyncIterator[User]:
    """Run the whole service, its lifespan, its routes and its pages, and give a simulated browser on it.

    The ``user`` fixture of NiceGUI runs only the lifespan of NiceGUI and sends each request to NiceGUI directly, so
    its pages would get no services.
    """
    app = create_app(settings, start=start)
    mount_ui(app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        yield User(client)


def elements_of(user: User, marker: str) -> list[ui.element]:
    """Return the elements with ``marker`` in the order of their creation, also the hidden ones.

    The list is empty when no element has the marker.
    """
    with user.client:
        found = list(ElementFilter(marker=marker, local_scope=False))
    return sorted(found, key=lambda element: element.id)


def element_of(user: User, marker: str) -> ui.element:
    (element,) = elements_of(user, marker)
    return element


def set_value(user: User, marker: str, value) -> None:
    """Give the element with ``marker`` the value ``value``, as if the user had entered it."""
    element = element_of(user, marker)
    with user.client:
        element.value = value


def text_of(user: User, marker: str) -> str:
    (element,) = user.find(marker=marker).elements
    return element.text


def texts_of(user: User, marker: str) -> list[str]:
    """Return the text of each element with ``marker`` in the order of their creation."""
    return [element.text for element in elements_of(user, marker)]


async def eventually(check: Callable[[], object], what: str, seconds: float = WAIT_SECONDS):
    """Wait until ``check()`` returns a truthy value, and return it. Raise AssertionError after ``seconds``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        try:
            if result := check():
                return result
        except AssertionError, LookupError, ValueError:
            pass
        if loop.time() >= deadline:
            raise AssertionError(f"{what} did not happen within {seconds} s")
        await asyncio.sleep(0.02)
