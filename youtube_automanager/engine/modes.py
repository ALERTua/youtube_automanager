"""The processing modes: how far a run reads a source, which videos it evaluates, and which stored videos it keeps.

A source lists its videos newest first: the uploads playlist of a channel, or the subscriptions feed of the account.
The service stores the state of each video of a source in ``seen_video``. A ``seen`` video is done. A ``deferred``
video waits for a later run, because a rule could not decide it or because it is upcoming or live.

These functions are pure: they read the listing and the stored state, and they never write.
"""

import datetime
import enum
import itertools
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from youtube_automanager.db.models import SeenVideoState

# Mode "stop at seen" always checks this many of the newest entries of a source, because a premiere moves up to the
# top of the listing when it starts, above videos that the service saw before.
MIN_CHECKED = 20

# After MIN_CHECKED entries, mode "stop at seen" also stops at the first dated entry that is older than the day of the
# last read of the source minus this many days. A source whose stored videos all vanished then reads only the recent
# days, not the whole channel. The margin covers the dates of yt-dlp, which come from relative text.
READ_BEFORE_MARGIN_DAYS = 2

# These live states change: an upcoming or live entry gets its duration and its date only when it becomes a normal
# video, so such an entry never becomes ``seen`` before that.
UNFINISHED_LIVE_STATES = frozenset({"is_upcoming", "is_live", "post_live"})
# The live status of a normal video, which a flat entry does not give.
NOT_LIVE = "not_live"


class ProcessingMode(enum.StrEnum):
    # Read newest first and stop at the first seen video, but always check the MIN_CHECKED newest entries.
    STOP_AT_SEEN = "stop_at_seen"
    # Read the ``count`` newest entries at each run, and evaluate each one that is not seen.
    NEWEST = "newest"
    # Read until the entries are older than the date mark of the source, or until the first stored seen video of the
    # mark day. The dates have day precision at best, so the new videos of the mark day are read again.
    MARK_DATE = "mark_date"


@dataclass(frozen=True)
class Processing:
    """The processing mode of the service. ``count`` is the number of entries of mode NEWEST, and None otherwise."""

    mode: ProcessingMode = ProcessingMode.STOP_AT_SEEN
    count: int | None = None

    def __post_init__(self) -> None:
        if self.mode is ProcessingMode.NEWEST:
            if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 1:
                raise ValueError(f"mode {self.mode.value!r} needs a count of 1 or more, not {self.count!r}")
        elif self.count is not None:
            raise ValueError(f"mode {self.mode.value!r} takes no count")

    @classmethod
    def from_value(cls, value: Any) -> Processing:
        """Read the JSON value of the setting, for example ``{"mode": "newest", "count": 30}``."""
        if not isinstance(value, dict) or set(value) - {"mode", "count"}:
            raise ValueError(f'the value must be an object with "mode" and an optional "count", not {value!r}')
        try:
            mode = ProcessingMode(value.get("mode"))
        except ValueError:
            names = ", ".join(repr(mode.value) for mode in ProcessingMode)
            raise ValueError(f"unknown mode {value.get('mode')!r}: use one of {names}") from None
        return cls(mode, value.get("count"))

    def to_value(self) -> dict[str, Any]:
        return {"mode": self.mode.value} if self.count is None else {"mode": self.mode.value, "count": self.count}

    def describe(self) -> str:
        return self.mode.value if self.count is None else f"{self.mode.value} {self.count}"


@dataclass(frozen=True)
class Video:
    """One entry of a source. ``channel_id`` is None for a feed video of two or more channels together."""

    video_id: str
    title: str
    channel_id: str | None
    duration_s: int | None
    upload_date: datetime.date | None
    live_status: str | None

    @classmethod
    def from_entry(cls, entry: Any) -> Video:
        """Copy an entry of the uploads of a channel or of the subscriptions feed."""
        return cls(
            video_id=entry.video_id,
            title=entry.title,
            channel_id=entry.channel_id,
            duration_s=entry.duration_s,
            upload_date=entry.upload_date,
            live_status=entry.live_status,
        )

    @property
    def is_finished(self) -> bool:
        """False while the entry is upcoming or live."""
        return self.live_status not in UNFINISHED_LIVE_STATES

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    def fields(self, is_short: bool | None = None) -> dict[str, Any]:
        """The video fields that a rule reads. A field without a value is None.

        A flat entry has a live status only for a live stream: upcoming, live or past. An entry without one is a
        normal finished video, so a rule reads ``not_live`` for it, as ``is_finished`` does.
        """
        return {
            "video_id": self.video_id,
            "title": self.title,
            "channel_id": self.channel_id,
            "duration_s": self.duration_s,
            "upload_date": None if self.upload_date is None else self.upload_date.isoformat(),
            "live_status": self.live_status or NOT_LIVE,
            "is_short": is_short,
        }


@dataclass(frozen=True)
class Known:
    """The stored state of one video of a source."""

    state: SeenVideoState
    upload_date: datetime.date | None


@dataclass(frozen=True)
class Collected:
    """``read`` is each entry in the processing window, and ``todo`` the entries to evaluate. Both are newest first."""

    read: tuple[Video, ...]
    todo: tuple[Video, ...]


def _unique(videos: Iterable[Video]) -> Iterator[Video]:
    """Give each video once. A listing can repeat an entry."""
    ids = set()
    for video in videos:
        if video.video_id not in ids:
            ids.add(video.video_id)
            yield video


def _is_seen(known: Mapping[str, Known], video: Video) -> bool:
    row = known.get(video.video_id)
    return row is not None and row.state is SeenVideoState.SEEN


def has_state(
    processing: Processing,
    known: Mapping[str, Known],
    mark: datetime.date | None,
    read_before: datetime.date | None,
) -> bool:
    """Tell whether the source has stored state. Without it, the run reads a baseline of the source.

    ``read_before`` is the day of the last read of the source. A source that the service read before has state, also
    without stored videos and without a mark, so an empty channel never gets a second baseline.
    """
    if read_before is not None:
        return True
    if processing.mode is ProcessingMode.MARK_DATE:
        return mark is not None
    return bool(known)


def collect(
    processing: Processing,
    videos: Iterable[Video],
    known: Mapping[str, Known],
    mark: datetime.date | None,
    read_before: datetime.date | None = None,
) -> Collected:
    """Read the processing window of a source with stored state, and pick the entries to evaluate.

    The iteration stops as early as the mode allows, so a lazy listing reads no page after the window.
    A deferred video never stops the read, and it is evaluated again. An upcoming or live entry has no date, so a
    date never stops the read at it.

    ``read_before`` is the day of the last read of the source. Mode STOP_AT_SEEN uses it for the limit of
    READ_BEFORE_MARGIN_DAYS, and mode MARK_DATE uses it as the mark of a source without a mark.
    """
    read: list[Video] = []
    todo: list[Video] = []
    unique = _unique(videos)
    if processing.mode is ProcessingMode.STOP_AT_SEEN:
        oldest = None if read_before is None else read_before - datetime.timedelta(days=READ_BEFORE_MARGIN_DAYS)
        met_seen = False
        for video in unique:
            if (
                oldest is not None
                and len(read) >= MIN_CHECKED
                and video.upload_date is not None
                and video.upload_date < oldest
            ):
                break
            read.append(video)
            if _is_seen(known, video):
                met_seen = True
            else:
                todo.append(video)
            if met_seen and len(read) >= MIN_CHECKED:
                break
    elif processing.mode is ProcessingMode.NEWEST:
        for video in itertools.islice(unique, processing.count):
            read.append(video)
            if not _is_seen(known, video):
                todo.append(video)
    else:
        if mark is None:
            mark = read_before
        if mark is None:
            raise ValueError("mode mark_date needs the mark or the day of the last read of the source")
        for video in unique:
            if video.upload_date is not None and video.upload_date < mark:
                break
            row = known.get(video.video_id)
            if row is not None and row.state is SeenVideoState.SEEN and row.upload_date == mark:
                # The upload order is stable, so each entry below a stored seen video of the mark day is older, and a
                # run read it before. The relative text of an old entry can give it a date on the mark day again.
                break
            read.append(video)
            if row is None or row.state is not SeenVideoState.SEEN:
                todo.append(video)
    return Collected(tuple(read), tuple(todo))


def baseline(processing: Processing, videos: Iterable[Video]) -> tuple[Video, ...]:
    """Read the entries that the first run of a source stores without an evaluation: the window of the mode.

    Mode MARK_DATE reads the entries of the newest day and the entries without a date above them.
    """
    unique = _unique(videos)
    if processing.mode is ProcessingMode.STOP_AT_SEEN:
        return tuple(itertools.islice(unique, MIN_CHECKED))
    if processing.mode is ProcessingMode.NEWEST:
        return tuple(itertools.islice(unique, processing.count))
    read: list[Video] = []
    day = None
    for video in unique:
        if video.upload_date is not None:
            if day is None:
                day = video.upload_date
            elif video.upload_date < day:
                break
        read.append(video)
    return tuple(read)


def moved_mark(mark: datetime.date | None, recorded: Iterable[Video]) -> datetime.date | None:
    """Return the mark after a run: the newest day of the recorded videos, but never an older day than ``mark``."""
    days = [video.upload_date for video in recorded if video.upload_date is not None]
    if mark is not None:
        days.append(mark)
    return max(days, default=None)


def ids_to_keep(
    processing: Processing,
    read: Iterable[Video],
    known: Mapping[str, Known],
    mark: datetime.date | None,
) -> set[str]:
    """Return the ids of the stored videos of a source that the next run can still reach.

    ``read`` is the window of this run, ``known`` the stored state after this run, and ``mark`` the moved mark.
    Modes STOP_AT_SEEN and NEWEST keep each video of the window: at least MIN_CHECKED or ``count`` of the newest,
    so a long quiet source never gets a new baseline. Mode MARK_DATE keeps the stored seen videos of the mark day and
    each video of the window that the next run reads again: a video without a date, and a video of the mark day.
    """
    if processing.mode is not ProcessingMode.MARK_DATE:
        return {video.video_id for video in read}
    keep = {
        video.video_id
        for video in read
        if video.upload_date is None or (mark is not None and video.upload_date >= mark)
    }
    keep.update(
        video_id for video_id, row in known.items() if row.state is SeenVideoState.SEEN and row.upload_date == mark
    )
    return keep
