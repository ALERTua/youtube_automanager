"""Tests of the pure functions of the processing modes."""

import datetime

import pytest

from youtube_automanager.db.models import SeenVideoState
from youtube_automanager.engine.modes import (
    MIN_CHECKED,
    Known,
    Processing,
    ProcessingMode,
    Video,
    baseline,
    collect,
    has_state,
    ids_to_keep,
    moved_mark,
)

DAY = datetime.date(2026, 10, 1)
# Older than the date limit of mode "stop at seen".
LATE = datetime.timedelta(days=3)
SEEN = Known(SeenVideoState.SEEN, DAY)
DEFERRED = Known(SeenVideoState.DEFERRED, None)
STOP = Processing()
MARK = Processing(ProcessingMode.MARK_DATE)


def video(video_id, day=DAY, live_status=None, duration_s=600):
    return Video(video_id, f"Video {video_id}", "UCaaaaaaaaaaaaaaaaaaaaaa", duration_s, day, live_status)


def ids(videos):
    return [item.video_id for item in videos]


def counted(videos, taken):
    """Iterate over ``videos`` and count each item that the caller takes."""
    for item in videos:
        taken.append(item.video_id)
        yield item


class TestProcessing:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ({"mode": "stop_at_seen"}, Processing()),
            ({"mode": "newest", "count": 30}, Processing(ProcessingMode.NEWEST, 30)),
            ({"mode": "mark_date"}, Processing(ProcessingMode.MARK_DATE)),
        ],
    )
    def test_a_valid_value_round_trips(self, value, expected):
        assert Processing.from_value(value) == expected
        assert expected.to_value() == value

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("stop_at_seen", "must be an object"),
            ({"mode": "stop_at_seen", "extra": 1}, "must be an object"),
            ({"mode": "sometimes"}, "unknown mode 'sometimes'"),
            ({"mode": "newest"}, "needs a count of 1 or more, not None"),
            ({"mode": "newest", "count": 0}, "needs a count of 1 or more, not 0"),
            ({"mode": "newest", "count": True}, "needs a count of 1 or more, not True"),
            ({"mode": "newest", "count": "5"}, "needs a count of 1 or more, not '5'"),
            ({"mode": "mark_date", "count": 5}, "takes no count"),
        ],
    )
    def test_a_wrong_value_is_refused(self, value, message):
        with pytest.raises(ValueError, match=message):
            Processing.from_value(value)


class TestVideo:
    def test_the_fields_give_the_date_as_text_and_none_for_a_missing_value(self):
        fields = video("v1", live_status="is_upcoming", duration_s=None).fields(is_short=False)

        assert fields == {
            "video_id": "v1",
            "title": "Video v1",
            "channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa",
            "duration_s": None,
            "upload_date": "2026-10-01",
            "live_status": "is_upcoming",
            "is_short": False,
        }

    @pytest.mark.parametrize(
        ("live_status", "finished"),
        [
            (None, True),
            ("not_live", True),
            ("was_live", True),
            ("is_upcoming", False),
            ("is_live", False),
            ("post_live", False),
        ],
    )
    def test_an_upcoming_or_live_entry_is_not_finished(self, live_status, finished):
        assert video("v1", live_status=live_status).is_finished is finished


class TestStopAtSeen:
    def test_without_a_seen_video_the_read_goes_to_the_end(self):
        videos = [video(f"v{index}") for index in range(30)]

        collected = collect(STOP, videos, {"v29": DEFERRED}, None)

        assert ids(collected.read) == ids(collected.todo) == ids(videos)

    def test_the_read_stops_at_the_minimum_when_it_met_a_seen_video(self):
        videos = [video(f"v{index}") for index in range(30)]
        taken = []

        collected = collect(STOP, counted(videos, taken), {"v3": SEEN}, None)

        assert len(taken) == len(collected.read) == MIN_CHECKED
        assert "v3" not in ids(collected.todo)
        assert len(collected.todo) == MIN_CHECKED - 1

    def test_a_repeated_entry_counts_once(self):
        videos = [video("v1"), video("v1"), video("v2")]

        collected = collect(STOP, videos, {}, None)

        assert ids(collected.read) == ["v1", "v2"]

    def test_after_the_minimum_the_read_stops_two_days_before_the_last_read(self):
        recent = [video(f"r{index}") for index in range(MIN_CHECKED)]
        videos = [*recent, video("two", day=DAY - datetime.timedelta(days=2)), video("three", day=DAY - LATE)]

        collected = collect(STOP, videos, {}, None, read_before=DAY)

        assert ids(collected.read) == [*ids(recent), "two"]

    def test_without_a_last_read_the_read_has_no_date_limit(self):
        videos = [video(f"v{index}", day=DAY - LATE) for index in range(30)]

        assert len(collect(STOP, videos, {}, None).read) == 30

    def test_the_date_limit_never_stops_the_read_before_the_minimum(self):
        videos = [video(f"v{index}", day=DAY - LATE) for index in range(30)]

        assert len(collect(STOP, videos, {}, None, read_before=DAY).read) == MIN_CHECKED

    def test_the_date_limit_never_stops_at_an_entry_without_a_date(self):
        recent = [video(f"r{index}") for index in range(MIN_CHECKED)]
        videos = [*recent, video("up", day=None, live_status="is_upcoming"), video("old", day=DAY - LATE)]

        collected = collect(STOP, videos, {}, None, read_before=DAY)

        assert ids(collected.read)[-1] == "up"


class TestNewest:
    def test_the_read_takes_exactly_the_count(self):
        videos = [video(f"v{index}") for index in range(10)]
        taken = []

        collected = collect(Processing(ProcessingMode.NEWEST, 4), counted(videos, taken), {"v1": SEEN}, None)

        assert taken == ["v0", "v1", "v2", "v3"]
        assert ids(collected.todo) == ["v0", "v2", "v3"]


class TestMarkDate:
    def test_the_read_stops_at_the_first_entry_older_than_the_mark(self):
        day2 = DAY + datetime.timedelta(days=1)
        videos = [video("up", day=None, live_status="is_upcoming"), video("b", day=day2), video("a", day=DAY)]

        collected = collect(MARK, videos, {}, day2)

        assert ids(collected.read) == ["up", "b"]

    def test_the_read_stops_at_a_stored_seen_video_of_the_mark_day(self):
        videos = [video("new"), video("old"), video("older")]

        collected = collect(MARK, videos, {"old": SEEN}, DAY)

        assert ids(collected.read) == ids(collected.todo) == ["new"]

    def test_a_seen_video_of_another_day_is_skipped_but_does_not_stop_the_read(self):
        later = DAY + datetime.timedelta(days=1)
        videos = [video("up", day=None), video("new"), video("old")]
        known = {"up": Known(SeenVideoState.SEEN, None), "new": Known(SeenVideoState.SEEN, later)}

        collected = collect(MARK, videos, known, DAY)

        assert ids(collected.read) == ["up", "new", "old"]
        assert ids(collected.todo) == ["old"]

    def test_a_deferred_video_of_the_mark_day_does_not_stop_the_read(self):
        videos = [video("waits"), video("old")]

        collected = collect(MARK, videos, {"waits": Known(SeenVideoState.DEFERRED, DAY)}, DAY)

        assert ids(collected.todo) == ["waits", "old"]

    def test_without_a_mark_the_day_of_the_last_read_is_the_mark(self):
        videos = [video("new", day=DAY), video("old", day=DAY - datetime.timedelta(days=1))]

        collected = collect(MARK, videos, {}, None, read_before=DAY)

        assert ids(collected.read) == ["new"]

    def test_collect_needs_the_mark_or_the_last_read(self):
        with pytest.raises(ValueError, match="needs the mark or the day of the last read"):
            collect(MARK, [], {}, None)

    def test_the_baseline_reads_the_newest_day(self):
        day2 = DAY + datetime.timedelta(days=1)
        videos = [video("up", day=None), video("b2", day=day2), video("a2", day=day2), video("a1"), video("a0")]

        assert ids(baseline(MARK, videos)) == ["up", "b2", "a2"]

    def test_the_mark_moves_to_the_newest_recorded_day_and_never_back(self):
        day2 = DAY + datetime.timedelta(days=1)

        assert moved_mark(None, [video("a", day=day2), video("b")]) == day2
        assert moved_mark(day2, [video("b")]) == day2
        assert moved_mark(DAY, [video("up", day=None)]) == DAY
        assert moved_mark(None, []) is None

    def test_the_mark_day_and_the_entries_without_a_date_stay(self):
        day2 = DAY + datetime.timedelta(days=1)
        read = [video("up", day=None), video("c", day=day2), video("b", day=day2), video("a", day=DAY)]
        known = {
            "up": DEFERRED,
            "c": Known(SeenVideoState.DEFERRED, day2),
            "b": Known(SeenVideoState.SEEN, day2),
            "a": SEEN,
            "gone": Known(SeenVideoState.SEEN, day2),
        }

        assert ids_to_keep(MARK, read, known, day2) == {"up", "c", "b", "gone"}


@pytest.mark.parametrize(
    ("processing", "known", "mark", "read_before", "expected"),
    [
        (STOP, {}, DAY, None, False),
        (STOP, {"v": DEFERRED}, None, None, True),
        (Processing(ProcessingMode.NEWEST, 3), {"v": SEEN}, None, None, True),
        (MARK, {"v": SEEN}, None, None, False),
        (MARK, {}, DAY, None, True),
        # A source that was read before has state, also without stored videos and without a mark.
        (STOP, {}, None, DAY, True),
        (Processing(ProcessingMode.NEWEST, 3), {}, None, DAY, True),
        (MARK, {}, None, DAY, True),
    ],
)
def test_has_state(processing, known, mark, read_before, expected):
    assert has_state(processing, known, mark, read_before) is expected


@pytest.mark.parametrize("processing", [STOP, Processing(ProcessingMode.NEWEST, 3)])
def test_the_id_modes_keep_the_window(processing):
    read = [video("a"), video("b")]

    assert ids_to_keep(processing, read, {"a": SEEN, "b": SEEN, "c": SEEN}, None) == {"a", "b"}


@pytest.mark.parametrize(("processing", "count"), [(STOP, MIN_CHECKED), (Processing(ProcessingMode.NEWEST, 7), 7)])
def test_the_baseline_of_the_id_modes_reads_their_window(processing, count):
    videos = [video(f"v{index}") for index in range(50)]
    taken = []

    assert len(baseline(processing, counted(videos, taken))) == count
    assert len(taken) == count
