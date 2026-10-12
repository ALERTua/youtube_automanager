"""Helpers that the yt-dlp readers of YouTube share."""

import datetime
import logging
import re
from collections.abc import Iterable, Iterator

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

CHANNEL_ID = re.compile(r"UC[0-9A-Za-z_-]{22}")
BASE_OPTIONS = {"quiet": True, "no_warnings": True, "skip_download": True}


class YtDlpLogger:
    """Sends the messages of yt-dlp to a log of this package, so yt-dlp writes nothing to stderr.

    An error of yt-dlp is only a warning here, because each caller raises its own error for it.
    """

    def __init__(self, log: logging.Logger) -> None:
        self._log = log

    def debug(self, message: str) -> None:
        self._log.debug(message)

    def info(self, message: str) -> None:
        self._log.debug(message)

    def warning(self, message: str) -> None:
        self._log.warning(message)

    def error(self, message: str) -> None:
        self._log.warning(message)


def closing_entries(entries: Iterator[dict], ydl: YoutubeDL) -> Iterator[dict]:
    """Hand out the entries, then close the yt-dlp instance, also when the caller stops early or a page fails."""
    try:
        yield from entries
    finally:
        ydl.close()


def avatar_url(thumbnails: Iterable[dict]) -> str | None:
    """Return the URL of the largest square thumbnail, which is the avatar. A banner is wide."""
    squares = [t for t in thumbnails if t.get("url") and t.get("width") and t.get("width") == t.get("height")]
    if not squares:
        return None
    url = max(squares, key=lambda t: t["width"])["url"]
    return "https:" + url if url.startswith("//") else url


def utc_date(timestamp: float | None) -> datetime.date | None:
    """Return the UTC date of a yt-dlp timestamp."""
    return None if timestamp is None else datetime.datetime.fromtimestamp(timestamp, datetime.UTC).date()


def cause_of_download_error(error: BaseException | None) -> BaseException | None:
    if isinstance(error, DownloadError) and error.exc_info:
        return error.exc_info[1]
    return None
