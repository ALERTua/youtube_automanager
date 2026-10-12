"""Read public YouTube data through yt-dlp, anonymously and without cookies.

The functions are synchronous, because the yt-dlp extraction calls are synchronous.
"""

import datetime
import itertools
import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from urllib.parse import quote_plus

from yt_dlp import YoutubeDL
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import DownloadError, ExtractorError, YoutubeDLError

LOG = logging.getLogger(__name__)


class _YtDlpLogger:
    """Sends the messages of yt-dlp to the log of this module, so yt-dlp writes nothing to stderr.

    An error of yt-dlp is only a warning here, because the module raises its own error for it.
    """

    def debug(self, message: str) -> None:
        LOG.debug(message)

    def info(self, message: str) -> None:
        LOG.debug(message)

    def warning(self, message: str) -> None:
        LOG.warning(message)

    def error(self, message: str) -> None:
        LOG.warning(message)


_YT_DLP_LOGGER = _YtDlpLogger()
_OPTIONS = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": _YT_DLP_LOGGER}
_APPROXIMATE_DATE = {"youtubetab": {"approximate_date": [""]}}
_CHANNELS_ONLY = "EgIQAg%253D%253D"
_REDIRECT_TYPES = ("url", "url_transparent")
_MISSING_CHANNEL_ALERT = "YouTube said: This channel does not exist."
_NO_SHORTS_TAB = "This channel does not have a shorts tab"

_CHANNEL_ID = re.compile(r"UC[0-9A-Za-z_-]{22}")
_HANDLE = re.compile(r"@?(?P<handle>[\w.-]{3,30})")
_CHANNEL_URL = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?youtube\.com/"
    r"(?:@(?P<handle>[\w.-]{3,30})|channel/(?P<channel_id>UC[0-9A-Za-z_-]{22}))"
    r"(?:[/?#].*)?"
)


class PublicSourceError(Exception):
    """yt-dlp failed to read public YouTube data."""


@dataclass(frozen=True)
class VideoEntry:
    video_id: str
    title: str
    channel_id: str
    channel_title: str | None
    duration_s: int | None
    upload_date: datetime.date | None
    live_status: str | None


@dataclass(frozen=True)
class ChannelInfo:
    channel_id: str
    title: str
    handle: str | None
    url: str
    thumbnail_url: str | None
    subscriber_count: int | None


def uploads_playlist_id(channel_id: str) -> str:
    """Return the id of the playlist with all uploads of the channel: videos, Shorts and past live streams."""
    return "UU" + _require_channel_id(channel_id)[2:]


def iter_channel_uploads(channel_id: str) -> Iterator[VideoEntry]:
    """Iterate over the uploads of the channel, newest first.

    The call reads the first page. The iteration reads each next page only when it reaches that page,
    so a caller that stops early does not read the whole channel.
    """
    url = f"https://www.youtube.com/playlist?list={uploads_playlist_id(channel_id)}"
    info = _fetch(url, extractor_args=_APPROXIMATE_DATE)
    return (_video_entry(entry, channel_id) for entry in _iter_entries(info, url))


def shorts_video_ids(channel_id: str, limit: int | None = None) -> set[str]:
    """Return the ids of the Shorts of the channel, at most ``limit`` of the newest ones.

    A channel without a Shorts tab has no Shorts, so the set is empty.
    """
    url = f"https://www.youtube.com/channel/{_require_channel_id(channel_id)}/shorts"
    try:
        info = _fetch(url)
    except PublicSourceError as error:
        if _is_extractor_alert(error.__cause__, _NO_SHORTS_TAB):
            return set()
        raise
    return {entry["id"] for entry in itertools.islice(_iter_entries(info, url), limit)}


def search_channels(query: str, limit: int = 20) -> list[ChannelInfo]:
    """Search YouTube for channels."""
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp={_CHANNELS_ONLY}"
    info = _fetch(url)
    channels = (entry for entry in _iter_entries(info, url) if entry.get("channel_id"))
    return [_channel_info(entry) for entry in itertools.islice(channels, limit)]


def resolve_channel(text: str) -> ChannelInfo | None:
    """Find a channel by a handle, a channel id or a channel URL. Return None when the channel does not exist.

    Accepted forms: ``@handle``, ``handle``, ``https://www.youtube.com/@handle``,
    ``https://www.youtube.com/channel/UC...`` and ``UC...``.
    """
    url = _channel_page_url(text)
    try:
        info = _fetch(url, playlist_items="0")
    except PublicSourceError as error:
        if _is_missing_channel(error.__cause__):
            return None
        raise
    return _channel_info(info)


def _extract(url: str, **options) -> dict:
    """Return the answer of the yt-dlp extractor for ``url`` as it is, without further processing.

    Its ``entries`` are flat, and they read each next page only when the iteration reaches it.
    The yt-dlp instance closes its connections when the entries end, or at once when the answer has no lazy entries.
    """
    ydl = YoutubeDL({**_OPTIONS, **options})
    try:
        info = ydl.extract_info(url, download=False, process=False)
    except BaseException:
        ydl.close()
        raise
    entries = info.get("entries") if info else None
    if isinstance(entries, Iterator):
        info["entries"] = _closing(entries, ydl)
    else:
        ydl.close()
    return info


def _closing(entries: Iterator[dict], ydl: YoutubeDL) -> Iterator[dict]:
    """Hand out the entries, then close the yt-dlp instance, also when the caller stops early or a page fails."""
    try:
        yield from entries
    finally:
        ydl.close()


def _fetch(url: str, **options) -> dict:
    """Return the answer for ``url``. Follow one redirect answer of yt-dlp with the same options, but not a second one."""
    info = _extract_or_raise(url, options)
    if info.get("_type") in _REDIRECT_TYPES:
        info = _extract_or_raise(info["url"], options)
        if info.get("_type") in _REDIRECT_TYPES:
            raise PublicSourceError(f"yt-dlp redirected {url} more than once, the second time to {info['url']}")
    return info


def _extract_or_raise(url: str, options: dict) -> dict:
    try:
        info = _extract(url, **options)
    except YoutubeDLError as error:
        raise PublicSourceError(f"yt-dlp failed to read {url}: {error}") from error
    if info is None:
        raise PublicSourceError(f"yt-dlp returned nothing for {url}")
    return info


def _iter_entries(info: dict, url: str) -> Iterator[dict]:
    entries = iter(info.get("entries") or ())
    while True:
        try:
            entry = next(entries)
        except StopIteration:
            return
        except YoutubeDLError as error:
            raise PublicSourceError(f"yt-dlp failed to read {url}: {error}") from error
        yield entry


def _video_entry(entry: dict, channel_id: str) -> VideoEntry:
    duration = entry.get("duration")
    timestamp = entry.get("timestamp")
    return VideoEntry(
        video_id=entry["id"],
        title=entry.get("title") or "",
        channel_id=entry.get("channel_id") or channel_id,
        channel_title=entry.get("channel"),
        duration_s=None if duration is None else int(duration),
        upload_date=None if timestamp is None else datetime.datetime.fromtimestamp(timestamp, datetime.UTC).date(),
        live_status=entry.get("live_status"),
    )


def _channel_info(info: dict) -> ChannelInfo:
    channel_id = info["channel_id"]
    handle = info.get("uploader_id")
    return ChannelInfo(
        channel_id=channel_id,
        title=info.get("channel") or info.get("title") or "",
        handle=handle if isinstance(handle, str) and handle.startswith("@") else None,
        url=info.get("channel_url") or f"https://www.youtube.com/channel/{channel_id}",
        thumbnail_url=_avatar_url(info.get("thumbnails") or ()),
        subscriber_count=info.get("channel_follower_count"),
    )


def _avatar_url(thumbnails: Iterable[dict]) -> str | None:
    """Return the URL of the largest square thumbnail, which is the avatar. A banner is wide."""
    squares = [t for t in thumbnails if t.get("url") and t.get("width") and t.get("width") == t.get("height")]
    if not squares:
        return None
    url = max(squares, key=lambda t: t["width"])["url"]
    return "https:" + url if url.startswith("//") else url


def _require_channel_id(channel_id: str) -> str:
    if not _CHANNEL_ID.fullmatch(channel_id):
        raise ValueError(f"not a YouTube channel id: {channel_id!r}")
    return channel_id


def _channel_page_url(text: str) -> str:
    text = text.strip()
    if _CHANNEL_ID.fullmatch(text):
        return f"https://www.youtube.com/channel/{text}"
    if match := _CHANNEL_URL.fullmatch(text):
        if match["channel_id"]:
            return f"https://www.youtube.com/channel/{match['channel_id']}"
        return f"https://www.youtube.com/@{match['handle']}"
    if match := _HANDLE.fullmatch(text):
        return f"https://www.youtube.com/@{match['handle']}"
    raise ValueError(f"not a YouTube channel id, handle or channel URL: {text!r}")


def _is_missing_channel(error: BaseException | None) -> bool:
    """Tell whether yt-dlp failed because the channel does not exist.

    For a handle, YouTube answers HTTP 404. For a channel id, it shows the alert "This channel does not exist."
    """
    cause = _cause_of_download_error(error)
    if isinstance(cause, HTTPError):
        return cause.status == 404
    return _is_extractor_alert(error, _MISSING_CHANNEL_ALERT)


def _is_extractor_alert(error: BaseException | None, message: str) -> bool:
    """Tell whether yt-dlp failed with an expected extractor error that has exactly this message."""
    cause = _cause_of_download_error(error)
    return isinstance(cause, ExtractorError) and cause.expected and cause.orig_msg == message


def _cause_of_download_error(error: BaseException | None) -> BaseException | None:
    if isinstance(error, DownloadError) and error.exc_info:
        return error.exc_info[1]
    return None
