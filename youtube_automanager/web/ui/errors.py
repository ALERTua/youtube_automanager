"""The message to the user when a handler of a page fails.

NiceGUI catches the exception of an event handler, a timer, a refreshable or a task of a page, and calls each exception
handler of its application. Its first handler logs the exception with its trace, so this module only tells the user.
"""

from nicegui import ui
from nicegui.slot import Slot

from youtube_automanager.i18n import _


def notify_failure() -> None:
    """Tell the user of the page where the exception happened that the action failed.

    An exception outside a page, for example in a background task of the service, has no user to tell, so the function
    then does nothing. It never raises, because NiceGUI would only log a second error.
    """
    # NiceGUI finds the client of an exception through the slot stack of the current task, in the same way.
    stack = Slot.get_stack()
    if not stack:
        return
    try:
        client = stack[-1].parent.client
    except RuntimeError:
        # The element of the slot or its client was deleted, for example after a rebuild or a closed tab.
        return
    if client.is_deleted:
        return
    with client:
        ui.notify(_("Something went wrong on this page. The log of the service holds the details."), type="negative")
