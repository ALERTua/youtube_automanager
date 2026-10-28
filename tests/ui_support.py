"""Helpers of the tests that run the whole service with its pages. The fixtures of these tests are in ``conftest.py``."""

import asyncio
import gc
import shutil
import threading
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import fastapi.dependencies.models as fastapi_models
import httpx
from nicegui import ElementFilter, ui
from nicegui import app as nicegui_app
from nicegui.app.app import State as NiceGUIState
from nicegui.client import Client
from nicegui.functions.notify import notify
from nicegui.testing import User
from nicegui.testing.general import nicegui_reset_globals
from nicegui.testing.user_notify import UserNotify
from sqlalchemy import make_url

from youtube_automanager.web.app import create_app
from youtube_automanager.web.ui import mount_ui

WAIT_SECONDS = 10
# The pause of NiceGUI between two tries of ``User.should_see``.
SEE_PAUSE_SECONDS = 0.1
# NiceGUI tries 3 times, 0.1 s apart. Under load, for example in a parallel run, a page can need longer. The tries end
# at the first success, so the longer limit costs a passing test nothing. A call can still give its own number of tries.
SEE_RETRIES = round(WAIT_SECONDS / SEE_PAUSE_SECONDS)
# A SQLite file with the newest schema. The fixture ``nicegui_globals`` sets it for each test.
DATABASE_TEMPLATE: Path | None = None


def store(sessions, *rows):
    with sessions.begin() as session:
        session.add_all(rows)


class RealNotify(UserNotify):
    """Replaces the ``ui.notify`` of the user simulation, which only records the message and reads no context.

    The real ``ui.notify`` takes the client from the current slot, and it raises when the element of that slot is gone.
    A page that calls it from a handler of a deleted element must fail in a test as it fails in the service.
    """

    def __call__(self, message: str, **kwargs) -> None:
        notify(message, **kwargs)
        super().__call__(message, **kwargs)


class PatientUser(User):
    """A simulated browser whose ``should_see`` waits up to WAIT_SECONDS for an element.

    Its ``ui.notify`` is the real one, and the message shows in ``notify.messages`` only when the real one took it.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.notify = RealNotify()

    async def should_see(self, target=None, *, kind=None, marker=None, content=None, retries=SEE_RETRIES) -> None:
        if target is None:
            await super().should_see(kind=kind, marker=marker, content=content, retries=retries)
        else:
            await super().should_see(target, retries=retries)


async def collect_deleted_elements() -> None:
    """Free the elements that a redraw of the page deleted.

    A slot keeps its parent by a weak reference, and an element sits in reference cycles, so a deleted button is gone
    only after the garbage collector ran. In the service this happens some time after the click. A test of a handler
    that tells its result after the redraw of its own button collects at once, so the test does not depend on the
    moment of the collector.
    """
    await asyncio.sleep(SEE_PAUSE_SECONDS)
    gc.collect()


class Held:
    """A function that waits for ``gate`` before it runs. A page calls it in a thread, so the test can change the page
    while the call waits. ``entered`` is set when the call starts, and the call ends after ``gate.set()``."""

    def __init__(self, function: Callable[..., Any]) -> None:
        self.function = function
        self.entered = threading.Event()
        self.gate = threading.Event()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.entered.set()
        self.gate.wait(WAIT_SECONDS)
        return self.function(*args, **kwargs)


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
    copy_database_template(settings.database_url)
    app = create_app(settings, start=start)
    mount_ui(app)
    # As in the service, the tabs are still open while the service stops, and they close after it.
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
        ):
            yield PatientUser(client), app.state.services
    finally:
        close_tabs()


def copy_database_template(url: str) -> None:
    """Copy ``DATABASE_TEMPLATE`` to the SQLite file of ``url`` when that file does not exist yet.

    The service then starts with a database at the newest schema, and its migrations at the start only check the
    version. The migrations of a fresh database take about 0.15 s, and nearly each test of the pages starts the service.
    The tests of the database and of the start of the service migrate their own databases.
    """
    parsed = make_url(url)
    if DATABASE_TEMPLATE is None or parsed.get_backend_name() != "sqlite" or parsed.database in (None, "", ":memory:"):
        return
    path = Path(parsed.database)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DATABASE_TEMPLATE, path)


def close_tabs() -> None:
    """Delete each NiceGUI client, as NiceGUI does after the browser closed its tab.

    The reset of NiceGUI between two tests only forgets the clients. A forgotten client stays in memory when one of its
    elements is still registered somewhere, for example an ``AddressFollower`` or the canary of a ``ui.dialog``, and
    it keeps its service with it. Each later ``gc.collect`` of the reset then scans more objects, and the suite gets
    slower with each test. The delete removes the elements of the client the same way as in the service.
    """
    for client in list(Client.instances.values()):
        client.delete()


@contextmanager
def fresh_nicegui() -> Generator[None]:
    """Reset the global state of NiceGUI for one test of the pages, and free its service at the end.

    The fixture ``nicegui_globals`` runs each test of the pages in this block.
    """
    with nicegui_reset_globals():
        try:
            yield
        finally:
            Client.page_routes.clear()
            forget_endpoints()
            mark_nicegui_stopped()


def mark_nicegui_stopped():
    """Mark NiceGUI as stopped after a test whose service did not stop cleanly.

    ``lifespan_wrapper`` of ``nicegui/ui_run_with.py`` calls ``_shutdown()`` only after its body ends without an
    exception. When a test fails inside ``running_service``, the private ``app._state`` stays ``State.STARTED``, and
    ``app.on_startup`` in ``mount_ui`` refuses each later test of the worker. ``_shutdown()`` would set
    ``State.STOPPED``, and the reset of NiceGUI at the start of the next test clears the rest.
    """
    if not nicegui_app.is_stopped:
        nicegui_app._state = NiceGUIState.STOPPED


def forget_endpoints():
    """Empty the caches in which FastAPI keeps each endpoint function that it classified.

    ``mount_ui`` makes a new page function for each application, and the function holds its application. The caches
    of FastAPI keep up to 4096 functions, so without this each test would keep its whole service in memory.
    """
    for cached in (
        fastapi_models._is_gen_callable_cached,
        fastapi_models._is_async_gen_callable_cached,
        fastapi_models._is_coroutine_callable_cached,
    ):
        cached.cache_clear()


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
