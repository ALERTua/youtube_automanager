"""The block of a page that shows when the stored account lists were refreshed, with the "Refresh lists" button.

The subscriptions page and the rules page read the stored copy of the subscriptions and the playlists of the account,
so a page load sends no request to YouTube. The button refreshes the copy through ``refresh_in_child``, which runs the
refresh in a child process. The page waits for it in a thread, so the event loop goes on.

``child_error_text`` gives the text of a failed child, for each page that starts a short child.
"""

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.account_cache import RefreshResult, last_refresh
from youtube_automanager.account_refresh import refresh_in_child
from youtube_automanager.i18n import _
from youtube_automanager.short_child import ChildBusy, ChildError, ChildLoggedOut, ChildUnavailable, ChildYouTubeError
from youtube_automanager.web.services import Services
from youtube_automanager.web.ui.common import ResultLabel, busy, format_time, read_timezone

LOG = logging.getLogger(__name__)

type OnRefreshed = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class RefreshTime:
    # None when the lists were never refreshed.
    refreshed_at: datetime | None
    timezone: str


def read_refresh_time(sessions: sessionmaker[Session], default_timezone: str) -> RefreshTime:
    """Read the time of the last refresh and the time zone of the schedule. This call blocks."""
    timezone = read_timezone(sessions, default_timezone)
    return RefreshTime(last_refresh(sessions), timezone)


def child_error_text(error: ChildError) -> str:
    """Return the text that a page shows for a failed short child. The cause from the child stays as it is."""
    if isinstance(error, ChildBusy):
        return _(
            "Another task works with YouTube now: a sync run, a session check, a refresh of the lists, a channel action "
            "or a yt-dlp update. Try again when it ends."
        )
    if isinstance(error, ChildUnavailable):
        # Translators: {cause} is the English cause from the service, for example that no cookies are stored.
        return _("The YouTube account is not available: {cause}").format(cause=error)
    if isinstance(error, ChildLoggedOut):
        return _(
            "YouTube does not accept the stored cookies any more ({cause}). "
            "Export new YouTube cookies from your browser and upload them to the service."
        ).format(cause=error)
    if isinstance(error, ChildYouTubeError):
        return _("YouTube did not give the expected answer: {cause}").format(cause=error)
    return _("The service could not finish the work: {cause}").format(cause=error)


def counts_text(result: RefreshResult) -> str:
    return _(
        "The lists are refreshed. Channels: {channels_new} new, {channels_updated} changed, {channels_unsubscribed} "
        "no longer subscribed. Playlists: {playlists_new} new, {playlists_updated} changed."
    ).format(**dataclasses.asdict(result))


async def account_lists(services: Services, on_refreshed: OnRefreshed | None = None) -> None:
    """Draw the time of the last refresh of the stored lists and the "Refresh lists" button.

    A click runs the refresh and shows its counts or the cause of its failure. After a success, the block shows the new
    time and awaits ``on_refreshed``, so the page can read the new lists.
    """

    @ui.refreshable
    def refreshed_view(time: RefreshTime) -> None:
        if time.refreshed_at is None:
            ui.label(_("The lists were never refreshed.")).mark("lists-refreshed")
            return
        text = format_time(time.refreshed_at, ZoneInfo(time.timezone))
        ui.label(_("Lists refreshed: {time}").format(time=text)).mark("lists-refreshed")

    async def refresh() -> None:
        with busy(button):
            result_label.ok(_("Refreshing the lists..."))
            try:
                result = await asyncio.to_thread(refresh_in_child, services.runner)
            except ChildError as error:
                LOG.warning("The account lists did not refresh: %s", error)
                result_label.problem(_("The lists did not refresh. {reason}").format(reason=child_error_text(error)))
                return
        result_label.set_text(counts_text(result))
        refreshed_view.refresh(await asyncio.to_thread(read_refresh_time, services.sessions, services.settings.tz))
        if on_refreshed is not None:
            await on_refreshed()

    initial = await asyncio.to_thread(read_refresh_time, services.sessions, services.settings.tz)
    with ui.row().classes("w-full items-center gap-4").mark("account-lists"):
        refreshed_view(initial)
        button = ui.button(_("Refresh lists"), icon="sync", on_click=refresh).mark("refresh-lists")
    result_label = ResultLabel().mark("refresh-lists-result")
