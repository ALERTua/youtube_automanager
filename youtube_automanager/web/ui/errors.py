"""The message to the user when a handler of a page fails.

NiceGUI catches the exception of an event handler, a timer, a refreshable or a task of a page, and calls each exception
handler of its application. Its first handler logs the exception with its trace, so this module only tells the user.
"""

from pathlib import Path
from traceback import walk_tb

from nicegui import ui
from nicegui.client import Client
from nicegui.elements import drawer
from nicegui.slot import Slot

from youtube_automanager.i18n import _

DRAWER_FILE = Path(drawer.__file__)


def notify_failure(exception: Exception) -> None:
    """Tell the user of the page where ``exception`` happened that the action failed.

    An exception outside a page, for example in a background task of the service, has no user to tell, so the function
    then does nothing. A late answer to the request of the drawer is no failed action, so it also stays only in the log.
    The function never raises, because NiceGUI would only log a second error.
    """
    if is_drawer_timeout(exception):
        return
    client = page_client()
    if client is None or client.is_deleted:
        return
    with client:
        ui.notify(_("Something went wrong on this page. The log of the service holds the details."), type="negative")


def is_drawer_timeout(exception: Exception) -> bool:
    """Return True when ``exception`` is the timeout of the request with which the drawer reads its state.

    The drawer of the layout follows the width of the screen, so at each connect NiceGUI asks the browser whether the
    drawer is open and waits 1 s for the answer. A slow browser answers late, and NiceGUI reports the timeout as an
    exception of the page, although no handler of the page failed.
    """
    if not isinstance(exception, TimeoutError):
        return False
    return any(
        frame.f_code.co_name == "_request_value" and Path(frame.f_code.co_filename) == DRAWER_FILE
        for frame, _line in walk_tb(exception.__traceback__)
    )


def page_client() -> Client | None:
    """Return the client of the current slot stack, or None when the stack has no live element.

    NiceGUI finds the client of an exception in the same way: the newest slot whose element and client still exist.
    """
    for slot in reversed(Slot.get_stack()):
        try:
            return slot.parent.client
        except RuntimeError:
            # The element of the slot or its client was deleted, for example after a rebuild or a closed tab.
            continue
    return None
