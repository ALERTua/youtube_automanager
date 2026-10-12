"""Read public YouTube data through yt-dlp, anonymously and without cookies.

The functions are synchronous, because the yt-dlp extraction calls are synchronous.
"""

import datetime
import itertools
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import quote_plus

from yt_dlp import YoutubeDL
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import ExtractorError, YoutubeDLError

from youtube_automanager.youtube._ytdlp import (
    BASE_OPTIONS,
    CHANNEL_ID,
    YtDlpLogger,
    avatar_url,
    cause_of_download_error,
    closing_entries,
    utc_date,
)

LOG = logging.getLogger(__name__)

_YT_DLP_LOGGER = YtDlpLogger(LOG)
_OPTIONS = {**BASE_OPTIONS, "logger": _YT_DLP_LOGGER}
_APPROXIMATE_DATE = {"youtubetab": {"approximate_date": [""]}}
# The `sp` filter of the YouTube search that keeps only channels: a protobuf in base64, URL-encoded twice.
_CHANNELS_ONLY = "EgIQAg%253D%253D"
_REDIRECT_TYPES = ("url", "url_transparent")
_MISSING_CHANNEL_ALERT = "YouTube said: This channel does not exist."
_NO_SHORTS_TAB = "This channel does not have a shorts tab"

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


def is_channel_reference(text: str) -> bool:
    """Tell whether ``text`` names one channel: ``@handle``, a channel id or a channel URL.

    A word without ``@`` is not a reference, because a person who types a word means a name.
    """
    text = text.strip()
    if text.startswith("@"):
        return _HANDLE.fullmatch(text) is not None
    return CHANNEL_ID.fullmatch(text) is not None or _CHANNEL_URL.fullmatch(text) is not None


def find_channels(text: str, limit: int = 20) -> list[ChannelInfo]:
    """Find the channels for the text that a person typed, at most ``limit`` of them.

    A reference (``@handle``, a channel id or a channel URL) gives its channel, or no channel when it does not exist.
    Other text is a name, and it goes to the channel search of YouTube. Empty text finds nothing and sends no request.
    """
    text = text.strip()
    if not text:
        return []
    if is_channel_reference(text):
        channel = resolve_channel(text)
        return [] if channel is None else [channel]
    return search_channels(text, limit)


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
        info["entries"] = closing_entries(entries, ydl)
    else:
        ydl.close()
    return info


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
    return VideoEntry(
        video_id=entry["id"],
        title=entry.get("title") or "",
        channel_id=entry.get("channel_id") or channel_id,
        channel_title=entry.get("channel"),
        duration_s=None if duration is None else int(duration),
        upload_date=utc_date(entry.get("timestamp")),
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
        thumbnail_url=avatar_url(info.get("thumbnails") or ()),
        subscriber_count=info.get("channel_follower_count"),
    )


def _require_channel_id(channel_id: str) -> str:
    if not CHANNEL_ID.fullmatch(channel_id):
        raise ValueError(f"not a YouTube channel id: {channel_id!r}")
    return channel_id


def _channel_page_url(text: str) -> str:
    text = text.strip()
    if CHANNEL_ID.fullmatch(text):
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
    cause = cause_of_download_error(error)
    if isinstance(cause, HTTPError):
        return cause.status == 404
    return _is_extractor_alert(error, _MISSING_CHANNEL_ALERT)


def _is_extractor_alert(error: BaseException | None, message: str) -> bool:
    """Tell whether yt-dlp failed with an expected extractor error that has exactly this message."""
    cause = cause_of_download_error(error)
    return isinstance(cause, ExtractorError) and cause.expected and cause.orig_msg == message
