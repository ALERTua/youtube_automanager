"""The limits of a preview and an apply of one rule, the draft that a preview reads, and the result of a preview.

The child process of a preview writes the result as JSON, and the web process reads it with ``Preview.from_value``.
This module imports no yt-dlp, so the web process can use it.
"""

import datetime
import enum
import json
from dataclasses import dataclass
from typing import Any

# The read of one source of a rule stops after this many entries.
READ_LIMIT = 500
# One apply adds at most this many videos, the oldest first. A second apply skips the added ones and goes on.
APPLY_LIMIT = 100
# One preview lists at most this many videos, and counts the rest.
PREVIEW_LIMIT = 200


class Mark(enum.StrEnum):
    """What a preview tells about one listed video."""

    # The preview of a saved rule before an apply.
    WILL_ADD = "will_add"
    # An ``added_video`` row for the target playlist exists, so the apply skips the video.
    ALREADY_ADDED = "already_added"
    # The rule gives UNKNOWN, or the video is upcoming or live, so the apply skips the video.
    UNDECIDED = "undecided"
    # The preview of a new rule.
    MATCHES = "matches"
    # The preview of a change of a saved rule.
    MATCHES_NOW = "matches_now"
    NO_LONGER_MATCHES = "no_longer_matches"


@dataclass(frozen=True)
class DraftRule:
    """The parts of a rule from the editor that a preview reads. Without channels, the rule reads the feed."""

    condition: Any
    channel_ids: tuple[str, ...]
    no_shorts: bool

    def to_json(self) -> str:
        """Return the draft as JSON text. The text is ASCII, so it passes a command line unchanged on each system."""
        return json.dumps(
            {"condition": self.condition, "channel_ids": list(self.channel_ids), "no_shorts": self.no_shorts}
        )

    @classmethod
    def from_json(cls, text: str) -> DraftRule:
        """Read the JSON text of ``to_json``. Raise ValueError when the text is not a draft."""
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != {"condition", "channel_ids", "no_shorts"}:
            raise ValueError(f"not a draft of a rule: {text!r}")
        channel_ids = data["channel_ids"]
        if (
            not isinstance(channel_ids, list)
            or not all(isinstance(channel_id, str) for channel_id in channel_ids)
            or not isinstance(data["no_shorts"], bool)
        ):
            raise ValueError(f"not a draft of a rule: {text!r}")
        return cls(data["condition"], tuple(channel_ids), data["no_shorts"])


@dataclass(frozen=True)
class PreviewVideo:
    """One listed video. ``upload_date`` is None for an entry without a date, for example an upcoming one."""

    video_id: str
    title: str
    channel_title: str
    upload_date: datetime.date | None
    mark: Mark

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    def to_value(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "channel_title": self.channel_title,
            "upload_date": None if self.upload_date is None else self.upload_date.isoformat(),
            "mark": self.mark.value,
        }

    @classmethod
    def from_value(cls, value: Any) -> PreviewVideo | None:
        if not isinstance(value, dict) or set(value) != {"video_id", "title", "channel_title", "upload_date", "mark"}:
            return None
        texts = (value["video_id"], value["title"], value["channel_title"])
        if not all(isinstance(text, str) for text in texts) or not value["video_id"]:
            return None
        try:
            day = None if value["upload_date"] is None else datetime.date.fromisoformat(value["upload_date"])
            mark = Mark(value["mark"])
        except TypeError, ValueError:
            return None
        return cls(value["video_id"], value["title"], value["channel_title"], day, mark)


@dataclass(frozen=True)
class Preview:
    """The result of a preview.

    ``videos`` holds at most PREVIEW_LIMIT of the listed videos, newest first, and ``counts`` counts each listed
    video by its mark, also the videos beyond the limit. ``errors`` names each source or rule that failed, and
    ``cut_sources`` names each source whose read stopped after READ_LIMIT entries before it reached the date.

    A preview of the next run also names in ``baseline_sources`` each source that the next run starts with a
    baseline, and ``mode_changed`` tells that the next run starts each source again, because the mode changed.
    """

    videos: tuple[PreviewVideo, ...]
    counts: dict[Mark, int]
    errors: tuple[str, ...] = ()
    cut_sources: tuple[str, ...] = ()
    baseline_sources: tuple[str, ...] = ()
    mode_changed: bool = False

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def more(self) -> int:
        """The number of the listed videos beyond the limit of the list."""
        return self.total - len(self.videos)

    def count(self, mark: Mark) -> int:
        return self.counts.get(mark, 0)

    def to_value(self) -> dict[str, Any]:
        return {
            "videos": [video.to_value() for video in self.videos],
            "counts": {mark.value: count for mark, count in self.counts.items()},
            "errors": list(self.errors),
            "cut_sources": list(self.cut_sources),
            "baseline_sources": list(self.baseline_sources),
            "mode_changed": self.mode_changed,
        }

    @classmethod
    def from_value(cls, value: Any) -> Preview | None:
        """Read the value of ``to_value``. Return None when the value is not a preview."""
        if not isinstance(value, dict) or set(value) != _PREVIEW_KEYS:
            return None
        videos, counts, errors, cut = value["videos"], value["counts"], value["errors"], value["cut_sources"]
        baselines, mode_changed = value["baseline_sources"], value["mode_changed"]
        if not isinstance(videos, list) or not isinstance(counts, dict) or not isinstance(mode_changed, bool):
            return None
        if not _texts(errors) or not _texts(cut) or not _texts(baselines):
            return None
        parsed = [PreviewVideo.from_value(video) for video in videos]
        if None in parsed:
            return None
        try:
            marks = {Mark(mark): count for mark, count in counts.items()}
        except ValueError:
            return None
        # A count must be a whole number, and 1 == True in Python.
        if any(type(count) is not int or count < 0 for count in marks.values()):
            return None
        preview = cls(tuple(parsed), marks, tuple(errors), tuple(cut), tuple(baselines), mode_changed)
        if len(parsed) > PREVIEW_LIMIT or preview.more < 0:
            return None
        return preview


_PREVIEW_KEYS = frozenset({"videos", "counts", "errors", "cut_sources", "baseline_sources", "mode_changed"})


def _texts(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
