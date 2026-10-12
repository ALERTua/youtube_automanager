"""Read and change the YouTube account of the user through yt-dlp, with the browser cookies of the user.

The client uses no YouTube Data API. Lists come from the flat extraction of yt-dlp. The account menu, the Save to
playlist dialog and each change go through internal methods of the ``YoutubeTab`` extractor of yt-dlp. A method name
with a leading underscore is internal, and a new release of yt-dlp can change it.

Two requests with the cookies of the account are at least ``pause_s`` seconds apart, from the end of one answer to the
start of the next. The functions are synchronous, because the yt-dlp calls are synchronous.

The page config of a logged-in YouTube page holds the value of the ``LOGIN_INFO`` cookie, so the client never logs it.
"""

import contextlib
import copy
import datetime
import io
import logging
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from yt_dlp import YoutubeDL
from yt_dlp.cookies import YoutubeDLCookieJar
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import DownloadError, ExtractorError, YoutubeDLError, traverse_obj

from youtube_automanager.youtube._ytdlp import (
    BASE_OPTIONS,
    CHANNEL_ID,
    YtDlpLogger,
    avatar_url,
    closing_entries,
    utc_date,
)

LOG = logging.getLogger(__name__)

PAUSE_S = 5.0
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
_PLAYLIST_ID = re.compile(r"[0-9A-Za-z_-]{2,64}")
_NETSCAPE_HEADER = re.compile(r"#( Netscape)? HTTP Cookie File")
_COOKIE_EXPIRES = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_HTTPONLY_PREFIX = "#HttpOnly_"


class AccountError(Exception):
    """yt-dlp failed to read or change the YouTube account."""


class NotLoggedInError(AccountError):
    """YouTube answered as to an anonymous user, or refused the session with HTTP 401.

    A session that ends first gets the answers for an anonymous user, and only later HTTP 401.
    """


class WriteRejectedError(AccountError):
    """YouTube answered a change, but the answer does not confirm the change."""


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


class _Pacer:
    """Keeps two requests at least ``pause_s`` seconds apart, from the end of one answer to the start of the next."""

    def __init__(self, pause_s: float, clock: Callable[[], float], sleep: Callable[[float], object]) -> None:
        self._pause_s = pause_s
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_answer: float | None = None

    @contextlib.contextmanager
    def request(self) -> Iterator[None]:
        with self._lock:
            if self._last_answer is not None:
                remaining = self._last_answer + self._pause_s - self._clock()
                if remaining > 0:
                    self._sleep(remaining)
            try:
                yield
            finally:
                self._last_answer = self._clock()


class _AccountYoutubeDL(YoutubeDL):
    """A yt-dlp instance that sends the cookies of the account, paces its requests and checks the login.

    It has no ``cookiefile`` option, so it never writes a cookie file. ``ytcfgs`` collects the config of each page
    that confirmed the login. A page that answers as to an anonymous user, or a redirect to the Google sign-in page,
    raises ``NotLoggedInError`` at once, before yt-dlp retries the page or reads more pages.
    """

    def __init__(self, params: dict, cookiejar: YoutubeDLCookieJar, pacer: _Pacer) -> None:
        self._account_cookiejar = cookiejar
        self._pacer = pacer
        super().__init__(params)
        self.ytcfgs: list[dict] = []
        self.tab = self.get_info_extractor(_TAB_EXTRACTOR)
        download_webpage = self.tab._download_webpage

        def checked_download_webpage(url_or_request, *args, **kwargs):
            webpage = download_webpage(url_or_request, *args, **kwargs)
            if isinstance(webpage, str):
                self._check_login(
                    self.tab.extract_ytcfg("page", webpage), getattr(url_or_request, "url", url_or_request)
                )
            return webpage

        self.tab._download_webpage = checked_download_webpage

    def _check_login(self, ytcfg: dict, url: str) -> None:
        logged_in = ytcfg.get("LOGGED_IN")
        if logged_in is False:
            raise NotLoggedInError(f"YouTube answered {url} as to an anonymous user")
        if logged_in is True:
            self.ytcfgs.append(ytcfg)

    @property
    def cookiejar(self) -> YoutubeDLCookieJar:
        return self._account_cookiejar

    def urlopen(self, req):
        with self._pacer.request():
            response = super().urlopen(req)
        if urllib.parse.urlsplit(response.url).hostname == _SIGN_IN_HOST:
            response.close()
            raise NotLoggedInError("YouTube sent the request to the Google sign-in page")
        return response


class AccountClient:
    """A client of one YouTube account. It holds the cookies of the account in memory only.

    ``cookies`` is the text of a Netscape cookie file. ``clock`` and ``sleep`` set the pause between two requests.
    """

    def __init__(
        self,
        cookies: str,
        *,
        pause_s: float = PAUSE_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        self._cookiejar = _load_cookie_jar(cookies)
        self._pacer = _Pacer(pause_s, clock, sleep)

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
        return [_subscribed_channel(entry) for entry in _iter_entries(info, _CHANNELS_URL)]

    def playlists(self) -> list[AccountPlaylist]:
        """Return the playlists in the library of the account, with Watch Later.

        The library page gives the playlists. The Save to playlist dialog tells which of them the account can add
        videos to. A playlist that only the dialog lists comes last.
        """
        info, ytcfg = self._read_page(_PLAYLISTS_URL)
        entries = list(_iter_entries(info, _PLAYLISTS_URL))
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
        return [_entry_id(entry, url) for entry in _iter_entries(info, url)]

    def iter_subscription_feed(self) -> Iterator[FeedVideo]:
        """Iterate over the videos of the subscriptions feed, newest first.

        The call reads the first page. The iteration reads each next page only when it reaches that page.
        The feed can also hold links that are not videos, and the iteration skips them.
        """
        info, _ = self._read_page(_SUBSCRIPTIONS_URL, extractor_args=_APPROXIMATE_DATE)
        return (
            _feed_video(entry)
            for entry in _iter_entries(info, _SUBSCRIPTIONS_URL)
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
        return _AccountYoutubeDL({**_OPTIONS, **options}, self._cookiejar, self._pacer)

    def _read_page(self, url: str, **options) -> tuple[dict, dict]:
        """Read the first page of a list of the account. Return the answer of yt-dlp and the config of the page.

        The ``entries`` of the answer are flat, and they read each next page only when the iteration reaches it.
        The yt-dlp instance closes its connections when the entries end, or at once when the read fails.
        """
        ydl = self._new_youtube_dl(**options)
        try:
            info = ydl.extract_info(url, download=False, process=False, ie_key=_TAB_EXTRACTOR)
            if not ydl.ytcfgs:
                raise AccountError(f"YouTube gave no page config for {url}, so the login is not confirmed")
            if not info or info.get("_type") != "playlist":
                raise AccountError(f"yt-dlp did not read {url} as a list")
        except YoutubeDLError as error:
            ydl.close()
            raise _account_error(error, f"read {url}") from error
        except BaseException:
            ydl.close()
            raise
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
            tab.initialize()
            if ytcfg is None:
                tab._download_webpage(_HOME_URL, what, note="Downloading the page config")
                if not ydl.ytcfgs:
                    raise AccountError(f"YouTube gave no page config, so the login is not confirmed to {what}")
                ytcfg = ydl.ytcfgs[0]
            response = tab._extract_response(
                item_id=what,
                query=query,
                ep=endpoint,
                ytcfg=ytcfg,
                headers=tab.generate_api_headers(ytcfg=ytcfg),
                note=f"Calling {endpoint}",
            )
        except YoutubeDLError as error:
            raise _account_error(error, what) from error
        finally:
            ydl.close()
        if not isinstance(response, dict) or not response:
            raise AccountError(f"YouTube gave an empty answer when the client tried to {what}")
        return response

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
    """Load the text of a Netscape cookie file. Refuse a text that has a line that yt-dlp would skip or refuse.

    yt-dlp writes a skipped line with its cookie value to stderr, and Python puts a refused line into its error
    message. So the check comes first, and its error names only the line number.
    """
    lines = text.splitlines()
    if not lines or not _NETSCAPE_HEADER.match(lines[0]):
        raise ValueError("the cookie text does not start with the header line of a Netscape cookie file")
    for number, line in enumerate(lines[1:], start=2):
        line = line.removeprefix(_HTTPONLY_PREFIX)
        if line.startswith("#") or not line.strip():
            continue
        fields = line.split("\t")
        if (
            len(fields) != 7
            or (fields[4] and not _COOKIE_EXPIRES.fullmatch(fields[4]))
            or (fields[1] == "TRUE") != fields[0].startswith(".")
        ):
            raise ValueError(f"line {number} of the cookie text is not a line of a Netscape cookie file")
    jar = YoutubeDLCookieJar()
    jar.load(io.StringIO("\n".join(lines) + "\n"))
    if not len(jar):
        raise ValueError("the cookie text holds no cookie")
    return jar


def _iter_entries(info: dict, url: str) -> Iterator[dict]:
    entries = iter(info.get("entries") or ())
    while True:
        try:
            entry = next(entries)
        except StopIteration:
            return
        except YoutubeDLError as error:
            raise _account_error(error, f"read {url}") from error
        yield entry


def _account_error(error: YoutubeDLError, what: str) -> AccountError:
    if _http_status(error) == 401:
        return NotLoggedInError(f"YouTube refused the session with HTTP 401 when the client tried to {what}")
    return AccountError(f"yt-dlp failed to {what}: {error}")


def _http_status(error: BaseException | None) -> int | None:
    """Return the status of the first ``HTTPError`` in the causes of a yt-dlp error, or None."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, HTTPError):
            return error.status
        if isinstance(error, DownloadError) and error.exc_info:
            error = error.exc_info[1]
        elif isinstance(error, ExtractorError) and error.cause is not None:
            error = error.cause
        else:
            error = error.__cause__ or error.__context__
    return None


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
