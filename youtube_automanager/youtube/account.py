"""Read and change the YouTube account of the user through yt-dlp, with the browser cookies of the user.

The client uses no YouTube Data API. Lists come from the flat extraction of yt-dlp. The account menu, the Save to
playlist dialog and each change go through internal methods of the ``YoutubeTab`` extractor of yt-dlp. A method name
with a leading underscore is internal, and a new release of yt-dlp can change it.

Each request with the cookies of the account takes a token of the shared ``RequestBudget`` before it goes, and the
budget keeps the end of its answer, also of an error answer. HTTP 429, a "try again later" page or a bot check opens
the breaker of the budget, and while it is open, each call fails with ``RequestsPausedError``. A call that ends
without a limit lets the budget close a pause that ended. The functions are synchronous, because the yt-dlp calls are
synchronous.

The page config of a logged-in YouTube page holds the value of the ``LOGIN_INFO`` cookie, so the client never logs it.

A new release of yt-dlp can rename or change an internal that the client calls. Such a call then raises
``AttributeError``, ``TypeError`` or ``KeyError``, and the client turns it into an ``AccountError`` that names the
version of yt-dlp and the call, so a run goes on without the account. ``YTDLP_INTERNALS`` lists the internals, and the
check of a new yt-dlp release makes sure that each of them exists.
"""

import copy
import datetime
import io
import logging
import re
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass

from yt_dlp import YoutubeDL
from yt_dlp.cookies import YoutubeDLCookieJar
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import DownloadError, ExtractorError, YoutubeDLError, traverse_obj
from yt_dlp.version import __version__ as YTDLP_VERSION

from youtube_automanager.db.models import BreakerReason
from youtube_automanager.pacing import BreakerOpen, RequestBudget
from youtube_automanager.request_pause import RequestPause
from youtube_automanager.youtube._ytdlp import (
    BASE_OPTIONS,
    CHANNEL_ID,
    YtDlpLogger,
    avatar_url,
    closing_entries,
    utc_date,
)
from youtube_automanager.youtube.cookie_text import checked_lines

LOG = logging.getLogger(__name__)

WATCH_LATER = "WL"

_YT_DLP_LOGGER = YtDlpLogger(LOG)
_OPTIONS = {**BASE_OPTIONS, "logger": _YT_DLP_LOGGER}
_APPROXIMATE_DATE = {"youtubetab": {"approximate_date": [""]}}
_TAB_EXTRACTOR = "YoutubeTab"
_VIDEO_EXTRACTOR = "Youtube"

_HOME_URL = "https://www.youtube.com"
_CHANNELS_URL = "https://www.youtube.com/feed/channels"
_PLAYLISTS_URL = "https://www.youtube.com/feed/playlists"
_SUBSCRIPTIONS_URL = "https://www.youtube.com/feed/subscriptions"
_SIGN_IN_HOST = "accounts.google.com"

# The Save to playlist dialog lists the playlists of a video, so it needs one. This is the first video on YouTube.
_SAVE_DIALOG_VIDEO_ID = "jNQXAC9IVRw"
_SUCCEEDED = "STATUS_SUCCEEDED"

_VIDEO_ID = re.compile(r"[0-9A-Za-z_-]{11}")
# The texts of the pages of YouTube that open the breaker, as yt-dlp gives them in an error or in a warning that quotes
# YouTube. YouTube can write each apostrophe as U+2019.
_BOT_CHECK = re.compile("confirm you[\u2019']re not a bot", re.IGNORECASE)
_TRY_AGAIN_LATER = re.compile("this content isn[\u2019']t available, try again later", re.IGNORECASE)
# The lowest HTTP status of a fault of the server. Its text can ask to try again later, but it is no limit.
_SERVER_ERROR = 500
# yt-dlp puts this text before each alert of YouTube that it reports as a warning.
_YOUTUBE_SAID = "YouTube said:"
_PLAYLIST_ID = re.compile(r"[0-9A-Za-z_-]{2,64}")

# The attributes of yt-dlp that the client uses beyond the public ``extract_info``, by the object that has them:
# ``YoutubeDL``, and the extractor that ``YoutubeDL.get_info_extractor`` gives for ``_TAB_EXTRACTOR``.
YTDLP_INTERNALS: dict[str, tuple[str, ...]] = {
    "YoutubeDL": ("get_info_extractor", "urlopen", "cookiejar"),
    _TAB_EXTRACTOR: ("initialize", "_download_webpage", "extract_ytcfg", "_extract_response", "generate_api_headers"),
}
# What a call into a changed internal of yt-dlp raises.
_INTERNAL_ERRORS = (AttributeError, TypeError, KeyError)


class AccountError(Exception):
    """yt-dlp failed to read or change the YouTube account. ``pause`` is the pause of the requests that caused it."""

    pause: RequestPause | None = None


class NotLoggedInError(AccountError):
    """YouTube answered as to an anonymous user, or refused the session with HTTP 401.

    A session that ends first gets the answers for an anonymous user, and only later HTTP 401.
    """


class WriteRejectedError(AccountError):
    """YouTube answered a change, but the answer does not confirm the change."""


class RequestsPausedError(AccountError):
    """The breaker of the request budget is open, so the client sent no request, or YouTube just limited one.

    The text names the end of the pause and the answer of YouTube that started it, and ``pause`` holds both.
    """

    def __init__(self, pause: RequestPause) -> None:
        self.pause = pause
        super().__init__(str(pause))


class _YtDlpCall:
    """Turns an error of a changed yt-dlp internal in the ``with`` block into an ``AccountError``.

    ``call`` names the call in the error text, for example ``YoutubeTab._extract_response``. The log gets the trace
    once. A trace shows the code lines and the error text, not the arguments, so it holds no cookie value.
    """

    def __init__(self, call: str) -> None:
        self._call = call

    def __enter__(self) -> None:
        return None

    def __exit__(self, kind, error, trace) -> None:
        if not isinstance(error, _INTERNAL_ERRORS):
            return
        text = f"yt-dlp {YTDLP_VERSION} does not fit the client: {self._call} raised {type(error).__name__}: {error}"
        LOG.error("%s", text, exc_info=(kind, error, trace))
        raise AccountError(text) from error


def missing_internals() -> list[str]:
    """Return each name of ``YTDLP_INTERNALS`` that the imported yt-dlp lacks, as ``Owner.name``. Send no request."""
    ydl = YoutubeDL(_OPTIONS)
    try:
        owners = {"YoutubeDL": ydl, _TAB_EXTRACTOR: ydl.get_info_extractor(_TAB_EXTRACTOR)}
        return [
            f"{owner}.{name}"
            for owner, names in YTDLP_INTERNALS.items()
            for name in names
            if not hasattr(owners[owner], name)
        ]
    finally:
        ydl.close()


@dataclass(frozen=True)
class AccountIdentity:
    """The account. ``channel_id`` is None when the account has no YouTube channel."""

    channel_id: str | None
    title: str
    handle: str | None


@dataclass(frozen=True)
class SubscribedChannel:
    channel_id: str
    title: str
    handle: str | None
    thumbnail_url: str | None


@dataclass(frozen=True)
class AccountPlaylist:
    """A playlist in the library of the account.

    ``is_own`` is True for each playlist that the account can add videos to: Watch Later and the playlists of the
    account. YouTube lists exactly these in its Save to playlist dialog. A saved playlist of another channel, Liked
    videos and Saved Shorts have False.
    """

    playlist_id: str
    title: str
    is_own: bool


@dataclass(frozen=True)
class FeedVideo:
    """A video of the subscriptions feed. A video of two or more channels together has no ``channel_id``."""

    video_id: str
    title: str
    channel_id: str | None
    channel_title: str | None
    duration_s: int | None
    upload_date: datetime.date | None
    live_status: str | None


class _AccountYoutubeDL(YoutubeDL):
    """A yt-dlp instance that sends the cookies of the account, paces its requests and checks the login.

    It has no ``cookiefile`` option, so it never writes a cookie file. Each request takes a token of ``budget``.
    ``ytcfgs`` collects the config of each page that confirmed the login. A page that answers as to an anonymous user,
    or a redirect to the Google sign-in page, raises ``NotLoggedInError`` at once, before yt-dlp retries the page or
    reads more pages.
    """

    def __init__(self, params: dict, cookiejar: YoutubeDLCookieJar, budget: RequestBudget) -> None:
        self._account_cookiejar = cookiejar
        self._budget = budget
        super().__init__(params)
        self.ytcfgs: list[dict] = []
        # The HTTP status of the last answer, or None before the first answer and after a failed connection.
        self.last_status: int | None = None
        with _YtDlpCall(f"YoutubeDL.get_info_extractor of {_TAB_EXTRACTOR}"):
            self.tab = self.get_info_extractor(_TAB_EXTRACTOR)
        with _YtDlpCall(f"{_TAB_EXTRACTOR}._download_webpage"):
            download_webpage = self.tab._download_webpage

        def checked_download_webpage(url_or_request, *args, **kwargs):
            with _YtDlpCall(f"{_TAB_EXTRACTOR}._download_webpage"):
                webpage = download_webpage(url_or_request, *args, **kwargs)
            if isinstance(webpage, str):
                with _YtDlpCall(f"{_TAB_EXTRACTOR}.extract_ytcfg"):
                    ytcfg = self.tab.extract_ytcfg("page", webpage)
                self._check_login(ytcfg, getattr(url_or_request, "url", url_or_request))
            return webpage

        self.tab._download_webpage = checked_download_webpage

    def _check_login(self, ytcfg: dict, url: str) -> None:
        logged_in = ytcfg.get("LOGGED_IN")
        if logged_in is False:
            raise NotLoggedInError(f"YouTube answered {url} as to an anonymous user")
        if logged_in is True:
            self.ytcfgs.append(ytcfg)

    # yt-dlp has no type annotations, so the checker reads its cached property as a plain writable attribute. This
    # read-only property takes its place on purpose, so no code of yt-dlp can swap the cookies of the account.
    @property
    def cookiejar(self) -> YoutubeDLCookieJar:  # ty: ignore[invalid-property-type-override]
        return self._account_cookiejar

    def report_warning(self, message, only_once=False):
        # yt-dlp may report a limited answer only as a warning, so open the breaker here.
        text = str(message)
        if _YOUTUBE_SAID in text and not _server_error(self.last_status) and (reason := _text_reason(text)):
            self._budget.open_breaker(reason)
        super().report_warning(message, only_once)

    def urlopen(self, req):
        try:
            self._budget.before_request()
        except BreakerOpen as breaker_open:
            raise RequestsPausedError(breaker_open.pause) from None
        status = None
        try:
            response = super().urlopen(req)
            status = response.status
        except HTTPError as error:
            status = error.status
            raise
        finally:
            # An error answer and a failed connection count too, so the next request keeps the pause after them.
            self.last_status = status
            self._budget.after_answer(status)
        if urllib.parse.urlsplit(response.url).hostname == _SIGN_IN_HOST:
            response.close()
            raise NotLoggedInError("YouTube sent the request to the Google sign-in page")
        return response


class AccountClient:
    """A client of one YouTube account. It holds the cookies of the account in memory only.

    ``cookies`` is the text of a Netscape cookie file. ``budget`` is the shared budget of the requests with cookies,
    on the database of the service, and each request of the client takes a token of it.
    """

    def __init__(self, cookies: str, *, budget: RequestBudget) -> None:
        self._cookiejar = _load_cookie_jar(cookies)
        self._budget = budget

    def use_cookies(self, cookies: str) -> None:
        """Replace the cookies of the client with the text of a Netscape cookie file.

        The budget stays, so the next request still takes a token and waits for the rest of the pause.
        """
        self._cookiejar = _load_cookie_jar(cookies)

    def check_session(self) -> None:
        """Read the page config of the YouTube home page and confirm that YouTube knows the session.

        Raise NotLoggedInError when YouTube answers as to an anonymous user, sends the request to the Google sign-in
        page, or refuses the session with HTTP 401. Raise AccountError when the answer does not tell either way.
        """
        what = "check the session"
        ydl = self._new_youtube_dl()
        try:
            _initialize(ydl)
            self._home_config(ydl, what)
        except YoutubeDLError as error:
            raise _account_error(error, what, self._budget) from error
        finally:
            ydl.close()
        self._budget.end_if_answered()

    def cookies_text(self) -> str:
        """Return the current cookies of the client as the text of a Netscape cookie file.

        The cookies change during the requests: YouTube sends new values and removes some cookies, and yt-dlp sets
        ``PREF``. The client itself keeps the cookies only in memory.
        """
        jar = YoutubeDLCookieJar()
        # ``save`` sets ``expires`` of each session cookie to 0, and a cookie with ``expires`` 0 counts as expired, so
        # the copy keeps the cookies of the client usable.
        for cookie in self._cookiejar:
            jar.set_cookie(copy.copy(cookie))
        text = io.StringIO()
        jar.save(text)
        return text.getvalue()

    def identity(self) -> AccountIdentity:
        """Return the channel id, the title and the handle of the account, from the account menu of YouTube."""
        response = self._call_api("account/account_menu", {}, "read the account menu")
        header = traverse_obj(
            response,
            (
                "actions",
                ...,
                "openPopupAction",
                "popup",
                "multiPageMenuRenderer",
                "header",
                "activeAccountHeaderRenderer",
                {dict},
                any,
            ),
        )
        title = traverse_obj(header, ("accountName", "simpleText", {str}))
        if not title:
            raise AccountError("the account menu of YouTube has no account name")
        channel_id = traverse_obj(
            header, ("manageAccountTitle", "runs", ..., "navigationEndpoint", "browseEndpoint", "browseId", {str}, any)
        )
        handle = traverse_obj(header, ("channelHandle", "simpleText", {str}))
        return AccountIdentity(
            channel_id=channel_id if channel_id and CHANNEL_ID.fullmatch(channel_id) else None,
            title=title,
            handle=handle if handle and handle.startswith("@") else None,
        )

    def subscribed_channels(self) -> list[SubscribedChannel]:
        """Return the channels that the account is subscribed to."""
        info, _ = self._read_page(_CHANNELS_URL)
        return [_subscribed_channel(entry) for entry in _iter_entries(info, _CHANNELS_URL, self._budget)]

    def playlists(self) -> list[AccountPlaylist]:
        """Return the playlists in the library of the account, with Watch Later.

        The library page gives the playlists. The Save to playlist dialog tells which of them the account can add
        videos to. A playlist that only the dialog lists comes last.
        """
        info, ytcfg = self._read_page(_PLAYLISTS_URL)
        entries = list(_iter_entries(info, _PLAYLISTS_URL, self._budget))
        editable = self._editable_playlists(ytcfg)
        playlists = [
            AccountPlaylist(playlist_id=playlist_id, title=entry.get("title") or "", is_own=playlist_id in editable)
            for entry in entries
            if (playlist_id := _entry_id(entry, _PLAYLISTS_URL))
        ]
        listed = {playlist.playlist_id for playlist in playlists}
        playlists += [
            AccountPlaylist(playlist_id=playlist_id, title=title, is_own=True)
            for playlist_id, title in editable.items()
            if playlist_id not in listed
        ]
        return playlists

    def playlist_video_ids(self, playlist_id: str) -> list[str]:
        """Return the ids of the videos in a playlist of the account, for example ``WL``, in the playlist order."""
        url = _playlist_url(playlist_id)
        info, _ = self._read_page(url)
        return [_entry_id(entry, url) for entry in _iter_entries(info, url, self._budget)]

    def iter_subscription_feed(self) -> Iterator[FeedVideo]:
        """Iterate over the videos of the subscriptions feed, newest first.

        The call reads the first page. The iteration reads each next page only when it reaches that page.
        The feed can also hold links that are not videos, and the iteration skips them.
        """
        info, _ = self._read_page(_SUBSCRIPTIONS_URL, extractor_args=_APPROXIMATE_DATE)
        return (
            _feed_video(entry)
            for entry in _iter_entries(info, _SUBSCRIPTIONS_URL, self._budget)
            if entry.get("ie_key") == _VIDEO_EXTRACTOR and entry.get("id")
        )

    def add_video(self, playlist_id: str, video_id: str) -> None:
        """Add a video to a playlist of the account. Raise an error unless YouTube confirms the change."""
        action = {"action": "ACTION_ADD_VIDEO", "addedVideoId": _require(video_id, _VIDEO_ID, "video id")}
        self._edit_playlist(playlist_id, action, f"add video {video_id} to playlist {playlist_id}")

    def remove_video(self, playlist_id: str, video_id: str) -> None:
        """Remove a video from a playlist of the account. Raise an error unless YouTube confirms the change."""
        action = {
            "action": "ACTION_REMOVE_VIDEO_BY_VIDEO_ID",
            "removedVideoId": _require(video_id, _VIDEO_ID, "video id"),
        }
        self._edit_playlist(playlist_id, action, f"remove video {video_id} from playlist {playlist_id}")

    def subscribe(self, channel_id: str) -> None:
        """Subscribe the account to a channel. Raise an error unless YouTube confirms the change."""
        self._change_subscription("subscription/subscribe", channel_id, subscribed=True)

    def unsubscribe(self, channel_id: str) -> None:
        """Unsubscribe the account from a channel. Raise an error unless YouTube confirms the change."""
        self._change_subscription("subscription/unsubscribe", channel_id, subscribed=False)

    def _new_youtube_dl(self, **options) -> _AccountYoutubeDL:
        return _AccountYoutubeDL({**_OPTIONS, **options}, self._cookiejar, self._budget)

    def _read_page(self, url: str, **options) -> tuple[dict, dict]:
        """Read the first page of a list of the account. Return the answer of yt-dlp and the config of the page.

        The ``entries`` of the answer are flat, and they read each next page only when the iteration reaches it.
        The yt-dlp instance closes its connections when the entries end, or at once when the read fails.
        """
        ydl = self._new_youtube_dl(**options)
        try:
            with _YtDlpCall("YoutubeDL.extract_info"):
                info = ydl.extract_info(url, download=False, process=False, ie_key=_TAB_EXTRACTOR)
            if not ydl.ytcfgs:
                raise AccountError(f"YouTube gave no page config for {url}, so the login is not confirmed")
            if not info or info.get("_type") != "playlist":
                raise AccountError(f"yt-dlp did not read {url} as a list")
        except YoutubeDLError as error:
            ydl.close()
            raise _account_error(error, f"read {url}", self._budget) from error
        except BaseException:
            ydl.close()
            raise
        self._budget.end_if_answered()
        entries = info.get("entries")
        if isinstance(entries, Iterator):
            info["entries"] = closing_entries(entries, ydl)
        else:
            ydl.close()
        return info, ydl.ytcfgs[0]

    def _call_api(
        self, endpoint: str, query: dict, what: str, *, ytcfg: dict | None = None, retries: int | None = None
    ) -> dict:
        """Send one request to the internal API of YouTube and return its answer.

        Without ``ytcfg``, the call first reads the config of the home page, which also confirms the login.
        ``retries`` limits the retries of yt-dlp. A change uses 0, so YouTube never gets the same change twice.
        """
        ydl = self._new_youtube_dl(**({} if retries is None else {"extractor_retries": retries}))
        try:
            tab = ydl.tab
            _initialize(ydl)
            if ytcfg is None:
                ytcfg = self._home_config(ydl, what)
            with _YtDlpCall(f"{_TAB_EXTRACTOR}.generate_api_headers"):
                headers = tab.generate_api_headers(ytcfg=ytcfg)
            with _YtDlpCall(f"{_TAB_EXTRACTOR}._extract_response"):
                response = tab._extract_response(
                    item_id=what, query=query, ep=endpoint, ytcfg=ytcfg, headers=headers, note=f"Calling {endpoint}"
                )
        except YoutubeDLError as error:
            raise _account_error(error, what, self._budget) from error
        finally:
            ydl.close()
        self._budget.end_if_answered()
        if not isinstance(response, dict) or not response:
            raise AccountError(f"YouTube gave an empty answer when the client tried to {what}")
        return response

    @staticmethod
    def _home_config(ydl: _AccountYoutubeDL, what: str) -> dict:
        """Read the config of the home page. The login check of the yt-dlp instance raises for an anonymous answer."""
        ydl.tab._download_webpage(_HOME_URL, what, note="Downloading the page config")
        if not ydl.ytcfgs:
            raise AccountError(f"YouTube gave no page config, so the login is not confirmed to {what}")
        return ydl.ytcfgs[0]

    def _editable_playlists(self, ytcfg: dict) -> dict[str, str]:
        """Return the id and the title of each playlist that the Save to playlist dialog lists, in its order."""
        response = self._call_api(
            "playlist/get_add_to_playlist",
            {"videoIds": [_SAVE_DIALOG_VIDEO_ID], "excludeWatchLater": False},
            "read the Save to playlist dialog",
            ytcfg=ytcfg,
        )
        options = traverse_obj(
            response, ("contents", ..., "addToPlaylistRenderer", "playlists", ..., "playlistAddToOptionRenderer")
        )
        editable = {
            option["playlistId"]: traverse_obj(option, ("title", "simpleText", {str})) or ""
            for option in options
            if isinstance(option, dict) and isinstance(option.get("playlistId"), str)
        }
        # The dialog always lists Watch Later. A dialog without it has a format that this client does not know.
        if WATCH_LATER not in editable:
            raise AccountError("the Save to playlist dialog of YouTube does not list Watch Later")
        return editable

    def _edit_playlist(self, playlist_id: str, action: dict, what: str) -> None:
        query = {"playlistId": _require(playlist_id, _PLAYLIST_ID, "playlist id"), "actions": [action]}
        response = self._call_api("browse/edit_playlist", query, what, retries=0)
        status = response.get("status")
        if status != _SUCCEEDED:
            raise WriteRejectedError(f"YouTube did not confirm that it could {what}: status {status!r}")
        LOG.info("YouTube confirmed the change: %s", what)

    def _change_subscription(self, endpoint: str, channel_id: str, *, subscribed: bool) -> None:
        verb = "subscribe to" if subscribed else "unsubscribe from"
        what = f"{verb} channel {channel_id}"
        query = {"channelIds": [_require(channel_id, CHANNEL_ID, "channel id")]}
        response = self._call_api(endpoint, query, what, retries=0)
        status = response.get("status")
        if status is not None and status != _SUCCEEDED:
            raise WriteRejectedError(f"YouTube did not confirm that it could {what}: status {status!r}")
        buttons = traverse_obj(response, ("actions", ..., "updateSubscribeButtonAction", {dict}))
        states = [button.get("subscribed") for button in buttons if button.get("channelId") == channel_id]
        if not states or any(state is not subscribed for state in states):
            raise WriteRejectedError(f"YouTube did not confirm that it could {what}: subscribed states {states!r}")
        LOG.info("YouTube confirmed the change: %s", what)


def _load_cookie_jar(text: str) -> YoutubeDLCookieJar:
    """Load the text of a Netscape cookie file. Refuse a text that has a line that yt-dlp would skip or refuse."""
    lines = checked_lines(text)
    jar = YoutubeDLCookieJar()
    jar.load(io.StringIO("\n".join(lines) + "\n"))
    if not len(jar):
        raise ValueError("the cookie text holds no cookie")
    return jar


def _initialize(ydl: _AccountYoutubeDL) -> None:
    with _YtDlpCall(f"{_TAB_EXTRACTOR}.initialize"):
        ydl.tab.initialize()


def _iter_entries(info: dict, url: str, budget: RequestBudget) -> Iterator[dict]:
    entries = iter(info.get("entries") or ())
    while True:
        try:
            with _YtDlpCall(f"the entries of {url}"):
                entry = next(entries)
        except StopIteration:
            return
        except YoutubeDLError as error:
            raise _account_error(error, f"read {url}", budget) from error
        yield entry


def _account_error(error: YoutubeDLError, what: str, budget: RequestBudget) -> AccountError:
    """Map a yt-dlp error to a client error. A limit of YouTube opens the breaker, and while it is open each error
    names the pause."""
    causes = list(_causes(error))
    status = next((cause.status for cause in causes if isinstance(cause, HTTPError)), None)
    reason = None
    if status == 429:
        reason = BreakerReason.HTTP_429
    elif not _server_error(status):
        reason = _text_reason(" ".join(map(str, causes)))
    breaker_open = budget.paused() if reason is None else budget.open_breaker(reason)
    if breaker_open is not None:
        return RequestsPausedError(breaker_open.pause)
    budget.end_if_answered()
    if status == 401:
        return NotLoggedInError(f"YouTube refused the session with HTTP 401 when the client tried to {what}")
    return AccountError(f"yt-dlp failed to {what}: {error}")


def _server_error(status: int | None) -> bool:
    return status is not None and status >= _SERVER_ERROR


def _text_reason(text: str) -> BreakerReason | None:
    """Return the reason of the breaker that a text of YouTube shows, or None."""
    if _BOT_CHECK.search(text):
        return BreakerReason.BOT_CHECK
    if _TRY_AGAIN_LATER.search(text):
        return BreakerReason.TRY_AGAIN_LATER
    return None


def _causes(error: BaseException | None) -> Iterator[BaseException]:
    """Iterate over a yt-dlp error and its causes, up to the first ``HTTPError``."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        if isinstance(error, HTTPError):
            return
        if isinstance(error, DownloadError) and error.exc_info:
            error = error.exc_info[1]
        elif isinstance(error, ExtractorError) and error.cause is not None:
            error = error.cause
        else:
            error = error.__cause__ or error.__context__


def _entry_id(entry: dict, url: str) -> str:
    entry_id = entry.get("id")
    if not isinstance(entry_id, str) or not entry_id:
        raise AccountError(f"an entry of {url} has no id")
    return entry_id


def _subscribed_channel(entry: dict) -> SubscribedChannel:
    channel_id = entry.get("channel_id") or entry.get("id")
    if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
        raise AccountError(f"an entry of {_CHANNELS_URL} has no channel id")
    handle = entry.get("uploader_id")
    return SubscribedChannel(
        channel_id=channel_id,
        title=entry.get("title") or entry.get("channel") or "",
        handle=handle if isinstance(handle, str) and handle.startswith("@") else None,
        thumbnail_url=avatar_url(entry.get("thumbnails") or ()),
    )


def _feed_video(entry: dict) -> FeedVideo:
    channel_id = entry.get("channel_id")
    duration = entry.get("duration")
    return FeedVideo(
        video_id=entry["id"],
        title=entry.get("title") or "",
        channel_id=channel_id if isinstance(channel_id, str) and CHANNEL_ID.fullmatch(channel_id) else None,
        channel_title=entry.get("channel"),
        duration_s=None if duration is None else int(duration),
        upload_date=utc_date(entry.get("timestamp")),
        live_status=entry.get("live_status"),
    )


def _playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={_require(playlist_id, _PLAYLIST_ID, 'playlist id')}"


def _require(value: str, pattern: re.Pattern, name: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"not a YouTube {name}: {value!r}")
    return value
