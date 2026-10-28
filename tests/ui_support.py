"""Helpers of the tests that run the whole service with its pages. The fixtures of these tests are in ``conftest.py``."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from nicegui import ElementFilter, ui
from nicegui.client import Client
from nicegui.testing import User

from youtube_automanager.web.app import create_app
from youtube_automanager.web.ui import mount_ui

WAIT_SECONDS = 10
# The pause of NiceGUI between two tries of ``User.should_see``.
SEE_PAUSE_SECONDS = 0.1
# NiceGUI tries 3 times, 0.1 s apart. Under load, for example in a parallel run, a page can need longer. The tries end
# at the first success, so the longer limit costs a passing test nothing. A call can still give its own number of tries.
SEE_RETRIES = round(WAIT_SECONDS / SEE_PAUSE_SECONDS)


def store(sessions, *rows):
    with sessions.begin() as session:
        session.add_all(rows)


class PatientUser(User):
    """A simulated browser whose ``should_see`` waits up to WAIT_SECONDS for an element."""

    async def should_see(self, target=None, *, kind=None, marker=None, content=None, retries=SEE_RETRIES) -> None:
        if target is None:
            await super().should_see(kind=kind, marker=marker, content=content, retries=retries)
        else:
            await super().should_see(target, retries=retries)


def client_of(user: User) -> Client:
    """Return the NiceGUI client of the page that ``user`` has open."""
    assert user.client is not None, "the user has no page open"
    return user.client


@asynccontextmanager
async def service_with_pages(settings, start) -> AsyncGenerator[tuple[User, Any]]:
    """Run the whole service, its lifespan, its routes and its pages, and give a simulated browser on it and the
    services.

    The ``user`` fixture of NiceGUI runs only the lifespan of NiceGUI and sends each request to NiceGUI directly, so
    its pages would get no services. A second ``PatientUser`` on ``user.http_client`` is a second browser tab.
    """
    app = create_app(settings, start=start)
    mount_ui(app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        try:
            yield PatientUser(client), app.state.services
        finally:
            close_tabs()


def close_tabs() -> None:
    """Delete each NiceGUI client, as NiceGUI does after the browser closed its tab.

    The reset of NiceGUI between two tests only forgets the clients. A forgotten client stays in memory when one of its
    elements is still registered somewhere, for example an ``AddressFollower`` or the canary of a ``ui.dialog``, and
    it keeps its service with it. Each later ``gc.collect`` of the reset then scans more objects, and the suite gets
    slower with each test. The delete removes the elements of the client the same way as in the service.
    """
    for client in list(Client.instances.values()):
        client.delete()


@asynccontextmanager
async def running_service(settings, start) -> AsyncGenerator[User]:
    """Run the whole service with its pages, and give a simulated browser on it."""
    async with service_with_pages(settings, start) as (user, _services):
        yield user


def elements_of(user: User, marker: str) -> list[ui.element]:
    """Return the elements with ``marker`` in the order of their creation, also the hidden ones.

    The list is empty when no element has the marker.
    """
    with client_of(user):
        found = list(ElementFilter(marker=marker, local_scope=False))
    return sorted(found, key=lambda element: element.id)


def element_of(user: User, marker: str) -> ui.element:
    (element,) = elements_of(user, marker)
    return element


def set_value(user: User, marker: str, value) -> None:
    """Give the element with ``marker`` the value ``value``, as if the user had entered it."""
    element = element_of(user, marker)
    with client_of(user):
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
