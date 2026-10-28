"""Tests of the sync engine with the test database, fake sources and a fake account. No test reads YouTube."""

import datetime

import pytest
from apprise.decorators import notify as apprise_notify

from youtube_automanager.db.models import (
    NotificationTarget,
    Run,
    RunActionKind,
    RunStatus,
    RunTrigger,
    SeenVideoState,
    Setting,
)
from youtube_automanager.engine.account_session import DEAD_SESSION_TEXT
from youtube_automanager.engine.modes import MIN_CHECKED
from youtube_automanager.engine.sync import (
    FEED_MARK_KEY,
    FEED_READ_KEY,
    FEED_SOURCE,
    SHORTS_LIMIT,
    STATE_MODE_KEY,
    AccountUnavailable,
    SyncAborted,
    run_with_youtube,
)
from youtube_automanager.notify import Event
from youtube_automanager.settings import Settings
from youtube_automanager.youtube import public
from youtube_automanager.youtube.account import AccountError, FeedVideo, NotLoggedInError
from youtube_automanager.youtube.public import PublicSourceError, VideoEntry

A = "UCaaaaaaaaaaaaaaaaaaaaaa"
B = "UCbbbbbbbbbbbbbbbbbbbbbb"
C = "UCcccccccccccccccccccccc"
ANY = {"==": [1, 1]}
LONG = {">=": [{"var": "duration_s"}, 120]}
# The day of the fake clock of the harness, so the entries are recent unless a test makes them older.
DAY = datetime.date(2026, 10, 12)
SEEN = SeenVideoState.SEEN
DEFERRED = SeenVideoState.DEFERRED
ADDED = RunActionKind.ADDED
SKIPPED = RunActionKind.SKIPPED
ERROR = RunActionKind.ERROR


def entry(video_id, channel=A, *, title=None, duration_s=600, day=DAY, live_status=None):
    return VideoEntry(
        video_id=video_id,
        title=title or f"Video {video_id}",
        channel_id=channel,
        channel_title=None,
        duration_s=duration_s,
        upload_date=day,
        live_status=live_status,
    )


def upcoming(video_id, channel=A):
    return entry(video_id, channel, duration_s=None, day=None, live_status="is_upcoming")


def feed_video(video_id, channel=A, *, duration_s=600, day=DAY, live_status=None):
    return FeedVideo(
        video_id=video_id,
        title=f"Video {video_id}",
        channel_id=channel,
        channel_title=None,
        duration_s=duration_s,
        upload_date=day,
        live_status=live_status,
    )


def entries(prefix, count, channel=A, **options):
    """``count`` entries, newest first: ``<prefix>00`` is the newest."""
    return [entry(f"{prefix}{index:02}", channel, **options) for index in range(count)]


def video_ids(items):
    return [item.video_id for item in items]


def wl(*ids):
    return [("WL", video_id) for video_id in ids]


# The first read of a source.


def test_first_read_stores_a_baseline_without_evaluation_or_add(sync):
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old

    report = sync.run()

    assert sync.account.added == []
    assert sync.account_opens == 0
    assert sync.sources.consumed[A] == MIN_CHECKED
    assert sync.seen(A) == dict.fromkeys(video_ids(old[:MIN_CHECKED]), SEEN)
    assert report.seen_count == MIN_CHECKED
    assert sync.run_row().seen_count == MIN_CHECKED
    assert sync.actions() == []


def test_an_upcoming_entry_of_a_baseline_waits_as_deferred(sync):
    sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = [entry("new"), upcoming("soon"), *entries("old", 5)]

    sync.run()

    assert sync.seen(A)["soon"] is DEFERRED
    assert sync.seen(A)["new"] is SEEN


def test_a_baseline_evaluates_its_upcoming_entry_when_it_becomes_a_video(sync):
    sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = [upcoming("soon"), *entries("old", 5)]
    sync.run()

    sync.sources.uploads[A] = [entry("soon"), *entries("old", 5)]
    sync.run()

    assert sync.account.added == wl("soon")
    assert sync.seen(A)["soon"] is SEEN


# Mode "stop at seen".


def test_new_videos_are_added_from_the_oldest_to_the_newest(sync):
    rule_id = sync.rule("all", ANY, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()

    sync.sources.uploads[A] = [entry("new2"), entry("new1"), entry("new0"), *old]
    report = sync.run()

    assert sync.account.added == wl("new0", "new1", "new2")
    assert sync.added_rows() == {("WL", "new0"): rule_id, ("WL", "new1"): rule_id, ("WL", "new2"): rule_id}
    assert [action[:2] for action in sync.actions()] == [(ADDED, "new0"), (ADDED, "new1"), (ADDED, "new2")]
    assert [video.video_url for video in report.added] == [
        f"https://www.youtube.com/watch?v={video_id}" for video_id in ("new0", "new1", "new2")
    ]
    assert (sync.run_row().added_count, sync.run_row().seen_count) == (3, 3)
    assert {video_id: sync.seen(A)[video_id] for video_id in ("new0", "new1", "new2")} == dict.fromkeys(
        ("new0", "new1", "new2"), SEEN
    )


def test_a_live_status_condition_reads_a_normal_video_as_not_live(sync):
    # A flat entry has a live status only for a live stream, so a normal video comes without one.
    sync.rule("no past streams", {"!=": [{"var": "live_status"}, "was_live"]}, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()

    sync.sources.uploads[A] = [entry("stream", live_status="was_live"), entry("normal"), *old]
    sync.run()

    assert sync.account.added == wl("normal")
    assert (sync.seen(A)["stream"], sync.seen(A)["normal"]) == (SEEN, SEEN)


def test_the_read_stops_after_the_newest_entries_when_it_met_a_seen_video(sync):
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 50)
    sync.sources.uploads[A] = old
    sync.run()

    sync.sources.uploads[A] = [entry("new"), *old]
    sync.run()

    # The entries below the window were never stored, so a longer read would add them.
    assert sync.account.added == wl("new")
    assert sync.sources.consumed[A] == MIN_CHECKED


def test_the_read_goes_on_until_a_seen_video_when_more_new_videos_came(sync):
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 40)
    sync.sources.uploads[A] = old
    sync.run()

    new = entries("new", 25)
    sync.sources.uploads[A] = [*new, *old]
    sync.run()

    assert sync.account.added == wl(*reversed(video_ids(new)))
    assert sync.sources.consumed[A] == 26


def test_a_premiere_that_moves_up_below_seen_videos_is_still_added(sync):
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()

    sync.sources.uploads[A] = [old[0], old[1], entry("premiere"), *old[2:]]
    sync.run()

    assert sync.account.added == wl("premiere")


def test_a_deferred_video_does_not_stop_the_read_and_is_checked_again(sync):
    sync.rule("long", LONG, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = [upcoming("soon"), *old]
    sync.run()
    assert sync.seen(A)["soon"] is DEFERRED

    new = entries("new", 25)
    sync.sources.uploads[A] = [*new, entry("soon"), *old]
    sync.run()

    assert sync.account.added == wl("soon", *reversed(video_ids(new)))


def test_a_deferred_video_matches_in_a_later_run(sync):
    rule_id = sync.rule("long", LONG, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()

    sync.sources.uploads[A] = [upcoming("soon"), *old]
    sync.run()
    assert sync.account.added == []
    assert sync.seen(A)["soon"] is DEFERRED
    (deferred,) = sync.action_rows()
    assert (deferred.action, deferred.video_id, deferred.rule_id) == (RunActionKind.DEFERRED, "soon", rule_id)
    first_check = sync.seen_rows(A)["soon"].last_checked_at

    # A check that finds the same state writes no second history entry, but it moves the check time.
    sync.run()
    assert sync.actions() == []
    assert sync.seen(A)["soon"] is DEFERRED
    assert sync.seen_rows(A)["soon"].last_checked_at > first_check

    sync.sources.uploads[A] = [entry("soon", duration_s=900), *old]
    sync.run()
    assert sync.account.added == wl("soon")
    assert sync.seen(A)["soon"] is SEEN


def test_an_upcoming_entry_that_matches_is_added_once_and_stays_deferred_until_it_is_a_video(sync):
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()

    sync.sources.uploads[A] = [upcoming("soon"), *old]
    sync.run()
    sync.run()
    assert sync.account.added == wl("soon")
    assert sync.seen(A)["soon"] is DEFERRED
    # A check of a known deferred video writes no history entry for the add that it skips.
    assert sync.actions() == []

    sync.sources.uploads[A] = [entry("soon"), *old]
    sync.run()
    assert sync.account.added == wl("soon")
    assert sync.seen(A)["soon"] is SEEN


def test_the_clean_up_keeps_the_window_and_the_add_record_of_a_video_that_left_it(sync):
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()

    first = entries("first", 5)
    sync.sources.uploads[A] = [*first, *old]
    sync.run()
    assert set(sync.seen(A)) == set(video_ids([*first, *old[:15]]))

    second = entries("second", 25)
    sync.sources.uploads[A] = [*second, *first, *old]
    sync.run()

    # The read went down to the first seen video, first00 at index 25.
    assert set(sync.seen(A)) == set(video_ids([*second, first[0]]))
    # An apply of the rule can still reach the videos that left the window, so their add records stay.
    assert set(sync.added_rows()) == set(wl(*video_ids([*second, *first])))


def test_a_removed_video_is_not_added_again_while_a_rule_waits_for_it(sync):
    """The user removed the video from WL by hand. A later check of the same video must not add it back."""
    sync.rule("all", ANY, channels=[A])
    sync.rule("long", LONG, channels=[A], target="PL2")
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()

    sync.sources.uploads[A] = [entry("nodur", duration_s=None), *old]
    sync.run()
    assert sync.account.added == wl("nodur")
    assert sync.seen(A)["nodur"] is DEFERRED

    sync.run()
    assert sync.account.added == wl("nodur")

    sync.sources.uploads[A] = [entry("nodur", duration_s=600), *old]
    sync.run()
    assert sync.account.added == [("WL", "nodur"), ("PL2", "nodur")]
    assert sync.seen(A)["nodur"] is SEEN


def test_an_empty_channel_gets_no_second_baseline(sync):
    sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = []
    sync.run()
    assert sync.channel_row(A).last_checked_at is not None

    sync.sources.uploads[A] = [entry("first")]
    sync.run()

    assert sync.account.added == wl("first")


def test_a_source_whose_stored_videos_vanished_reads_only_the_recent_days(sync):
    sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = entries("gone", 30)
    sync.run()

    # The channel deleted each stored video. Its older videos are 30 days old.
    recent = entries("recent", 25)
    old = entries("old", 30, day=DAY - datetime.timedelta(days=30))
    sync.sources.uploads[A] = [*recent, *old]
    sync.run()

    assert sync.account.added == wl(*reversed(video_ids(recent)))
    assert sync.sources.consumed[A] == 26


def test_the_limit_of_the_last_read_never_stops_the_read_before_the_newest_entries(sync):
    sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = entries("gone", 30)
    sync.run()

    recent = entries("recent", 3)
    old = entries("old", 30, day=DAY - datetime.timedelta(days=30))
    sync.sources.uploads[A] = [*recent, *old]
    sync.run()

    assert len(sync.account.added) == MIN_CHECKED
    assert sync.sources.consumed[A] == MIN_CHECKED + 1


def test_the_limit_of_the_last_read_never_stops_at_an_upcoming_entry(sync):
    sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = entries("gone", 30)
    sync.run()

    recent = entries("recent", MIN_CHECKED)
    old = entries("old", 5, day=DAY - datetime.timedelta(days=30))
    sync.sources.uploads[A] = [*recent, upcoming("soon"), *old]
    sync.run()

    assert sync.account.added[0] == ("WL", "soon")
    assert sync.sources.consumed[A] == MIN_CHECKED + 2


def test_the_feed_also_reads_only_the_recent_days_when_its_stored_videos_vanished(sync):
    sync.rule("feed", ANY)
    sync.account.feed = [feed_video(f"gone{index:02}") for index in range(30)]
    sync.run()

    recent = [feed_video(f"recent{index:02}") for index in range(25)]
    old = [feed_video(f"old{index:02}", day=DAY - datetime.timedelta(days=30)) for index in range(30)]
    sync.account.feed = [*recent, *old]
    sync.run()

    assert sync.account.added == wl(*reversed(video_ids(recent)))


# The time of the last read.


def test_each_full_read_writes_the_time_of_the_read(sync):
    sync.rule("all", ANY, channels=[A, B])
    sync.rule("feed", ANY)
    sync.sources.uploads[A] = entries("a", 5)
    sync.sources.uploads[B] = []
    sync.account.feed = []
    first = sync.now

    sync.run()

    assert sync.channel_row(A).last_checked_at == first
    assert sync.channel_row(B).last_checked_at == first
    with sync.sessions() as session:
        assert session.get(Setting, FEED_READ_KEY).value == first.isoformat()

    second = sync.now
    sync.run()

    assert sync.channel_row(A).last_checked_at == second
    with sync.sessions() as session:
        assert session.get(Setting, FEED_READ_KEY).value == second.isoformat()


def test_a_failed_source_keeps_the_time_of_its_last_full_read(sync):
    sync.rule("all", ANY, channels=[A, B])
    sync.sources.uploads[A] = entries("a", 25, A)
    sync.sources.uploads[B] = entries("b", 25, B)
    first = sync.now
    sync.run()

    sync.sources.errors[A] = PublicSourceError("HTTP Error 404")
    sync.sources.uploads[B] = [entry("bnew", B), *sync.sources.uploads[B]]
    sync.account.add_errors[("WL", "bnew")] = AccountError("YouTube refused the change")
    sync.run()

    assert sync.channel_row(A).last_checked_at == first
    assert sync.channel_row(B).last_checked_at == first


def test_a_source_that_no_rule_reads_any_more_loses_the_time_of_its_last_read(sync):
    channel_rule = sync.rule("channels", ANY, channels=[A, B])
    feed_rule = sync.rule("feed", ANY)
    sync.sources.uploads[A] = []
    sync.sources.uploads[B] = []
    sync.run()

    sync.unlink(channel_rule, A)
    sync.disable(feed_rule)
    sync.run()

    assert sync.channel_row(A).last_checked_at is None
    assert sync.channel_row(B).last_checked_at is not None
    with sync.sessions() as session:
        assert session.get(Setting, FEED_READ_KEY) is None


# Mode "newest N".


def test_mode_newest_reads_only_the_newest_entries(sync):
    sync.mode("newest", 5)
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()
    assert sync.sources.consumed[A] == 5
    assert set(sync.seen(A)) == set(video_ids(old[:5]))
    assert sync.account.added == []

    sync.sources.uploads[A] = [entry("new1"), entry("new0"), *old]
    sync.run()

    assert sync.account.added == wl("new0", "new1")
    assert sync.sources.consumed[A] == 5
    assert set(sync.seen(A)) == {"new1", "new0", *video_ids(old[:3])}


def test_mode_newest_checks_a_deferred_video_again_while_it_is_among_the_newest(sync):
    sync.mode("newest", 3)
    sync.rule("long", LONG, channels=[A])
    old = entries("old", 10)
    sync.sources.uploads[A] = old
    sync.run()

    sync.sources.uploads[A] = [upcoming("soon"), *old]
    sync.run()
    sync.sources.uploads[A] = [entry("new"), entry("soon"), *old]
    sync.run()

    assert sync.account.added == wl("soon", "new")


def test_mode_newest_drops_a_deferred_video_that_left_the_newest(sync):
    sync.mode("newest", 2)
    sync.rule("long", LONG, channels=[A])
    old = entries("old", 10)
    sync.sources.uploads[A] = old
    sync.run()
    sync.sources.uploads[A] = [upcoming("soon"), *old]
    sync.run()

    sync.sources.uploads[A] = [entry("new1"), entry("new0"), upcoming("soon"), *old]
    sync.run()

    assert set(sync.seen(A)) == {"new1", "new0"}


# Mode "mark date".


def test_mode_mark_date_baseline_sets_the_mark_to_the_newest_day(sync):
    sync.mode("mark_date")
    sync.rule("long", LONG, channels=[A])
    day3, day2 = DAY + datetime.timedelta(days=2), DAY + datetime.timedelta(days=1)
    sync.sources.uploads[A] = [
        upcoming("soon"),
        entry("d3a", day=day3),
        entry("d3b", day=day3),
        entry("d2a", day=day2),
        entry("d1a", day=DAY),
    ]

    sync.run()

    assert sync.mark(A) == day3
    assert sync.seen(A) == {"soon": DEFERRED, "d3a": SEEN, "d3b": SEEN}
    assert sync.sources.consumed[A] == 4
    assert sync.account.added == []


def test_mode_mark_date_reads_the_mark_day_again_and_moves_the_mark(sync):
    sync.mode("mark_date")
    sync.rule("long", LONG, channels=[A])
    day3, day4 = DAY + datetime.timedelta(days=2), DAY + datetime.timedelta(days=3)
    older = [entry("d2a", day=DAY + datetime.timedelta(days=1)), entry("d1a", day=DAY)]
    sync.sources.uploads[A] = [upcoming("soon"), entry("d3a", day=day3), entry("d3b", day=day3), *older]
    sync.run()

    # A new video of the mark day comes after the last run, and a video of a new day.
    sync.sources.uploads[A] = [
        upcoming("soon"),
        entry("d4a", day=day4),
        entry("d3c", day=day3),
        entry("d3a", day=day3),
        entry("d3b", day=day3),
        *older,
    ]
    sync.run()

    assert sync.account.added == wl("d3c", "d4a")
    # The read stops at d3a, the first stored seen video of the mark day.
    assert sync.sources.consumed[A] == 4
    assert sync.mark(A) == day4
    # Only the mark day and the entries without a date stay.
    assert sync.seen(A) == {"soon": DEFERRED, "d4a": SEEN}

    sync.sources.uploads[A] = [upcoming("soon"), entry("d4a", day=day4), entry("d3c", day=day3), *older]
    sync.run()
    assert sync.account.added == wl("d3c", "d4a")
    assert sync.sources.consumed[A] == 2


def test_mode_mark_date_does_not_read_again_an_older_entry_that_gets_the_mark_day(sync):
    """The relative text of an older entry gets less exact with age, so its date can move up to the mark day."""
    sync.mode("mark_date")
    sync.rule("all", ANY, channels=[A])
    day2 = DAY + datetime.timedelta(days=1)
    sync.sources.uploads[A] = [entry("newer", day=day2), entry("older", day=DAY)]
    sync.run()
    assert sync.seen(A) == {"newer": SEEN}

    sync.sources.uploads[A] = [entry("newer", day=day2), entry("older", day=day2)]
    sync.run()

    assert sync.account.added == []
    assert sync.sources.consumed[A] == 1


def test_mode_mark_date_checks_an_upcoming_entry_until_it_is_a_video(sync):
    sync.mode("mark_date")
    sync.rule("long", LONG, channels=[A])
    sync.sources.uploads[A] = [upcoming("soon"), entry("d1a")]
    sync.run()
    sync.run()
    assert sync.account.added == []

    later = DAY + datetime.timedelta(days=5)
    sync.sources.uploads[A] = [entry("soon", day=later), entry("d1a")]
    sync.run()

    assert sync.account.added == wl("soon")
    assert sync.mark(A) == later
    assert sync.seen(A) == {"soon": SEEN}


def test_mode_mark_date_evaluates_a_first_upcoming_entry_after_it_becomes_a_video(sync):
    """A channel with only an upcoming entry gets no mark, but it was read before, so it gets no second baseline."""
    sync.mode("mark_date")
    sync.rule("long", LONG, channels=[A])
    sync.sources.uploads[A] = [upcoming("first")]
    sync.run()
    assert sync.mark(A) is None
    assert sync.seen(A) == {"first": DEFERRED}

    sync.sources.uploads[A] = [entry("first")]
    sync.run()

    assert sync.account.added == wl("first")
    assert sync.mark(A) == DAY
    assert sync.seen(A) == {"first": SEEN}


def test_mode_mark_date_uses_the_day_of_the_last_read_as_the_mark_of_a_source_without_a_mark(sync):
    sync.mode("mark_date")
    sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = []
    sync.run()
    assert sync.mark(A) is None

    long_ago = DAY - datetime.timedelta(days=30)
    sync.sources.uploads[A] = [upcoming("soon"), entry("new"), entry("old", day=long_ago), entry("older", day=long_ago)]
    sync.run()

    assert sync.account.added == wl("new", "soon")
    assert sync.sources.consumed[A] == 3
    assert sync.mark(A) == DAY


def test_mode_mark_date_keeps_the_mark_of_the_feed_in_the_settings(sync):
    sync.mode("mark_date")
    sync.rule("feed", LONG)
    day2 = DAY + datetime.timedelta(days=1)
    sync.account.feed = [feed_video("f2", day=day2), feed_video("f1", day=DAY)]
    sync.run()
    with sync.sessions() as session:
        assert session.get(Setting, FEED_MARK_KEY).value == day2.isoformat()

    day3 = DAY + datetime.timedelta(days=2)
    sync.account.feed = [feed_video("f3", day=day3), feed_video("f2b", day=day2), *sync.account.feed]
    sync.run()

    assert sync.account.added == wl("f2b", "f3")
    with sync.sessions() as session:
        assert session.get(Setting, FEED_MARK_KEY).value == day3.isoformat()
    assert sync.seen(FEED_SOURCE) == {"f3": SEEN}


def test_mode_mark_date_clears_the_mark_of_a_source_that_no_rule_reads_any_more(sync):
    sync.mode("mark_date")
    channel_rule = sync.rule("channels", ANY, channels=[A, B])
    feed_rule = sync.rule("feed", ANY)
    sync.sources.uploads[A] = [entry("a1")]
    sync.sources.uploads[B] = [entry("b1", B)]
    sync.account.feed = [feed_video("f1")]
    sync.run()
    assert sync.mark(A) == DAY

    sync.unlink(channel_rule, A)
    sync.disable(feed_rule)
    sync.run()

    assert sync.mark(A) is None
    assert sync.mark(B) == DAY
    with sync.sessions() as session:
        assert session.get(Setting, FEED_MARK_KEY) is None

    # A rule that reads the channel again starts with a baseline, so the videos of the gap are not added.
    sync.link(channel_rule, A)
    sync.sources.uploads[A] = [entry("a2", day=DAY + datetime.timedelta(days=1)), entry("a1")]
    sync.run()
    assert sync.account.added == []
    assert sync.mark(A) == DAY + datetime.timedelta(days=1)


# A change of the processing mode.


def test_a_new_processing_mode_starts_each_source_with_a_baseline(sync):
    sync.rule("all", ANY, channels=[A])
    sync.rule("feed", ANY)
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.account.feed = [feed_video(f"feed{index:02}") for index in range(30)]
    sync.run()
    with sync.sessions() as session:
        assert session.get(Setting, STATE_MODE_KEY).value == {"mode": "stop_at_seen"}

    # Mode "newest 25" reads 5 old videos of each source that mode "stop at seen" never stored.
    sync.mode("newest", 25)
    sync.run()

    assert sync.account.added == []
    assert set(sync.seen(A)) == set(video_ids(old[:25]))
    assert len(sync.seen(FEED_SOURCE)) == 25
    with sync.sessions() as session:
        assert session.get(Setting, STATE_MODE_KEY).value == {"mode": "newest", "count": 25}

    sync.sources.uploads[A] = [entry("new"), *old]
    sync.run()
    assert sync.account.added == wl("new")


def test_a_new_processing_mode_clears_the_marks(sync):
    sync.mode("mark_date")
    sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = entries("old", 5)
    sync.run()
    assert sync.mark(A) == DAY

    sync.mode("stop_at_seen")
    sync.run()

    assert sync.mark(A) is None
    assert sync.account.added == []


def test_a_new_processing_mode_keeps_the_add_record_of_a_video_outside_the_new_baseline(sync):
    rule_id = sync.rule("all", ANY, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()
    sync.sources.uploads[A] = [entry("new"), *old]
    sync.run()
    assert sync.added_rows() == {("WL", "new"): rule_id}

    sync.mode("newest", 1)
    sync.sources.uploads[A] = [entry("next"), entry("new"), *old]
    sync.run()

    assert set(sync.seen(A)) == {"next"}
    assert sync.added_rows() == {("WL", "new"): rule_id}


def test_an_invalid_processing_mode_stops_the_run(sync):
    sync.mode("newest")
    sync.rule("all", ANY, channels=[A])

    with pytest.raises(SyncAborted, match="'processing_mode' is not valid: mode 'newest' needs a count"):
        sync.run()


# Several rules.


def test_a_video_goes_to_each_target_of_its_rules(sync):
    first = sync.rule("first", ANY, channels=[A])
    second = sync.rule("second", ANY, channels=[A], target="PL2")
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()

    sync.sources.uploads[A] = [entry("new"), *old]
    sync.run()

    assert sync.account.added == [("WL", "new"), ("PL2", "new")]
    assert sync.added_rows() == {("WL", "new"): first, ("PL2", "new"): second}


def test_several_rules_with_the_same_target_add_the_video_once(sync):
    first = sync.rule("first", ANY, channels=[A])
    sync.rule("second", LONG, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()

    sync.sources.uploads[A] = [entry("new"), *old]
    sync.run()

    assert sync.account.added == wl("new")
    assert sync.added_rows() == {("WL", "new"): first}
    assert sync.actions() == [(ADDED, "new", first, "WL")]


def test_a_rule_reads_only_the_videos_of_its_own_channels(sync):
    sync.rule("a", ANY, channels=[A])
    sync.rule("b", ANY, channels=[B], target="PL2")
    sync.sources.uploads[A] = entries("a", 25, A)
    sync.sources.uploads[B] = entries("b", 25, B)
    sync.run()

    sync.sources.uploads[A] = [entry("anew", A), *sync.sources.uploads[A]]
    sync.sources.uploads[B] = [entry("bnew", B), *sync.sources.uploads[B]]
    sync.run()

    assert sorted(sync.account.added) == [("PL2", "bnew"), ("WL", "anew")]


def test_a_new_rule_on_a_known_channel_acts_only_on_new_videos(sync):
    sync.rule("first", LONG, channels=[A])
    old = entries("old", 30, duration_s=60)
    sync.sources.uploads[A] = old
    sync.run()
    sync.run()

    sync.rule("short ones", {"<": [{"var": "duration_s"}, 120]}, channels=[A], target="PL2")
    sync.sources.uploads[A] = [entry("new", duration_s=60), *old]
    sync.run()

    assert sync.account.added == [("PL2", "new")]


def test_a_new_channel_of_a_rule_starts_with_a_baseline(sync):
    rule_id = sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = entries("a", 25, A)
    sync.run()

    sync.link(rule_id, B)
    sync.sources.uploads[B] = entries("b", 25, B)
    sync.run()

    assert sync.account.added == []
    assert len(sync.seen(B)) == MIN_CHECKED


def test_a_channel_that_no_rule_reads_any_more_loses_its_state_but_keeps_its_add_records(sync):
    rule_id = sync.rule("all", ANY, channels=[A, B])
    sync.sources.uploads[A] = entries("a", 25, A)
    old_b = entries("b", 25, B)
    sync.sources.uploads[B] = old_b
    sync.run()
    sync.sources.uploads[B] = [entry("bnew", B), *old_b]
    sync.run()
    assert ("WL", "bnew") in sync.added_rows()

    sync.unlink(rule_id, B)
    sync.run()

    assert sync.seen(B) == {}
    assert sync.added_rows() == {("WL", "bnew"): rule_id}
    assert sync.sources.upload_reads[-1] == A

    sync.link(rule_id, B)
    sync.run()
    assert sync.account.added == wl("bnew")
    assert len(sync.seen(B)) == MIN_CHECKED


def test_a_disabled_rule_reads_nothing(sync):
    sync.rule("off", ANY, channels=[A], enabled=False)
    sync.sources.uploads[A] = entries("a", 5)

    sync.run()

    assert sync.sources.upload_reads == []
    assert sync.seen(A) == {}


def test_a_rule_with_an_invalid_condition_is_left_out_with_an_error(sync):
    bad = sync.rule("bad", {"regex": [{"var": "title"}, "("]}, channels=[A])
    sync.rule("good", ANY, channels=[B])
    sync.sources.uploads[B] = entries("b", 5, B)

    report = sync.run()

    assert sync.actions() == [(ERROR, None, bad, None)]
    assert "Rule bad:" in report.errors[0]
    assert sync.sources.upload_reads == [B]


# Shorts.


def test_a_short_never_matches_a_rule_without_shorts(sync):
    sync.rule("no shorts", ANY, channels=[A], no_shorts=True)
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()

    sync.sources.shorts[A] = {"short", "oldshort"}
    sync.sources.uploads[A] = [entry("video", duration_s=170), entry("short", duration_s=30), *old]
    sync.run()

    assert sync.account.added == wl("video")
    assert sync.seen(A)["short"] is SEEN
    # One read of the Shorts of the channel serves each video of the run.
    assert sync.sources.shorts_reads == [(A, SHORTS_LIMIT)]


def test_the_shorts_are_not_read_when_the_condition_already_refuses_the_video(sync):
    sync.rule("long, no shorts", LONG, channels=[A], no_shorts=True)
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()

    sync.sources.uploads[A] = [entry("tiny", duration_s=30), *old]
    sync.run()

    assert sync.sources.shorts_reads == []
    assert sync.seen(A)["tiny"] is SEEN


def test_a_rule_that_allows_shorts_does_not_read_them(sync):
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()

    sync.sources.shorts[A] = {"short"}
    sync.sources.uploads[A] = [entry("short", duration_s=30), *old]
    sync.run()

    assert sync.sources.shorts_reads == []
    assert sync.account.added == wl("short")


def test_a_condition_on_is_short_reads_the_shorts(sync):
    sync.rule("only shorts", {"==": [{"var": "is_short"}, True]}, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()

    sync.sources.shorts[A] = {"short"}
    sync.sources.uploads[A] = [entry("video"), entry("short", duration_s=30), *old]
    sync.run()

    assert sync.account.added == wl("short")


def test_a_failed_read_of_the_shorts_defers_the_video_and_reports_the_channel(sync):
    sync.rule("no shorts", ANY, channels=[A], no_shorts=True)
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()

    sync.sources.shorts_errors[A] = PublicSourceError("the Shorts tab failed")
    sync.sources.uploads[A] = [entry("video"), *old]
    report = sync.run()

    assert sync.account.added == []
    assert sync.seen(A)["video"] is DEFERRED
    assert [action[0] for action in sync.actions()] == [ERROR, RunActionKind.DEFERRED]
    assert "the Shorts tab failed" in report.errors[0]
    assert [notification.event for notification in sync.sent] == [Event.CHANNEL_ERROR, Event.RUN_SUMMARY]

    del sync.sources.shorts_errors[A]
    sync.run()
    assert sync.account.added == wl("video")


# The subscriptions feed.


def test_a_rule_without_channels_reads_the_feed(sync):
    sync.rule("feed", LONG)
    sync.account.feed = [feed_video("f1"), feed_video("f0")]
    sync.run()
    assert sync.seen(FEED_SOURCE) == {"f1": SEEN, "f0": SEEN}
    assert sync.account.added == []

    sync.account.feed = [feed_video("collab", channel=None), feed_video("f2", duration_s=60), *sync.account.feed]
    sync.run()

    assert sync.account.added == wl("collab")
    (added,) = sync.action_rows()
    assert (added.action, added.video_id, added.channel_id) == (ADDED, "collab", None)


def test_a_video_of_a_channel_is_still_new_to_a_feed_rule(sync):
    channel_rule = sync.rule("channel", ANY, channels=[A])
    feed_rule = sync.rule("feed", ANY)
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.account.feed = [feed_video(video.video_id) for video in old]
    sync.run()

    sync.sources.uploads[A] = [entry("new"), *old]
    sync.account.feed = [feed_video("new"), *sync.account.feed]
    sync.run()

    assert sync.account.added == wl("new")
    assert sync.actions() == [(ADDED, "new", channel_rule, "WL"), (SKIPPED, "new", feed_rule, "WL")]
    assert sync.seen(FEED_SOURCE)["new"] is SEEN
    assert sync.seen(A)["new"] is SEEN


def test_a_feed_rule_without_shorts_defers_a_video_of_unknown_channels(sync):
    sync.rule("feed, no shorts", ANY, no_shorts=True)
    sync.account.feed = [feed_video("f0")]
    sync.run()

    sync.account.feed = [feed_video("collab", channel=None), feed_video("own", channel=B), *sync.account.feed]
    sync.run()

    assert sync.account.added == wl("own")
    assert sync.seen(FEED_SOURCE)["collab"] is DEFERRED
    assert sync.sources.shorts_reads == [(B, SHORTS_LIMIT)]


# Errors.


def test_a_channel_error_is_reported_and_the_run_goes_on(sync):
    sync.rule("all", ANY, channels=[A, B, C])
    for channel in (A, B, C):
        sync.sources.uploads[channel] = entries(channel[-1], 25, channel)
    sync.run()
    stored_b = sync.seen(B)

    sync.sources.errors[B] = PublicSourceError("HTTP Error 404")
    for channel in (A, C):
        sync.sources.uploads[channel] = [entry(f"{channel[-1]}new", channel), *sync.sources.uploads[channel]]
    report = sync.run()

    assert sorted(sync.account.added) == wl("anew", "cnew")
    assert sync.seen(B) == stored_b
    assert (ERROR, None, None, None) in sync.actions()
    assert report.errors == [f"Channel Channel {B}: HTTP Error 404"]
    channel_errors = [notification for notification in sync.sent if notification.event is Event.CHANNEL_ERROR]
    assert len(channel_errors) == 1
    assert "HTTP Error 404" in channel_errors[0].body


def test_the_channel_errors_of_a_run_go_into_one_message_before_the_summary(sync):
    sync.rule("all", ANY, channels=[A, B, C])
    for channel in (A, B, C):
        sync.sources.uploads[channel] = entries(channel[-1], 25, channel)
    sync.run()

    sync.sources.errors[B] = PublicSourceError("HTTP Error 404")
    sync.sources.errors[C] = PublicSourceError("HTTP Error 500")
    report = sync.run()

    assert [notification.event for notification in sync.sent] == [Event.CHANNEL_ERROR, Event.RUN_SUMMARY]
    channel_error, summary = sync.sent
    assert channel_error.title == "YouTube AutoManager: cannot read 2 channels"
    assert f"- Channel {B}: HTTP Error 404\n- Channel {C}: HTTP Error 500" in channel_error.body
    # The summary keeps a line for each error.
    assert report.errors == [f"Channel Channel {B}: HTTP Error 404", f"Channel Channel {C}: HTTP Error 500"]
    assert all(error in summary.body for error in report.errors)


def test_a_run_sends_each_added_video_after_its_record(sync):
    sync.playlist("WL", "Watch later")
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()
    stored = []
    send = sync.send

    def send_and_look(notification):
        if notification.event is Event.VIDEO_ADDED:
            stored.append(set(sync.added_rows()))
        return send(notification)

    sync.send = send_and_look
    sync.sources.uploads[A] = [entry("new2", title="Second"), entry("new1", title="First"), *old]
    sync.run()

    assert [notification.event for notification in sync.sent] == [
        Event.VIDEO_ADDED,
        Event.VIDEO_ADDED,
        Event.RUN_SUMMARY,
    ]
    assert [notification.title for notification in sync.sent[:2]] == [
        "YouTube AutoManager: video added to Watch later"
    ] * 2
    assert sync.sent[0].body == "Added First to the playlist Watch later.\nhttps://www.youtube.com/watch?v=new1"
    assert stored == [{("WL", "new1")}, {("WL", "new1"), ("WL", "new2")}]


def test_a_failed_add_sends_no_added_video(sync):
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()

    sync.account.add_errors[("WL", "new")] = AccountError("YouTube did not confirm the change")
    sync.sources.uploads[A] = [entry("new"), *old]
    sync.run()

    assert [notification.event for notification in sync.sent] == [Event.RUN_SUMMARY]


def test_a_page_that_fails_in_the_middle_leaves_the_channel_as_it_was(sync):
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()
    stored = sync.seen(A)

    new = entries("new", 25)
    sync.sources.uploads[A] = [*new, *old]
    sync.sources.page_errors[A] = (22, PublicSourceError("the second page failed"))
    sync.run()

    assert sync.account.added == []
    assert sync.seen(A) == stored


def test_a_failed_add_leaves_the_video_and_the_newer_ones_for_the_next_run(sync):
    sync.rule("all", ANY, channels=[A, B])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.sources.uploads[B] = entries("b", 25, B)
    sync.run()

    sync.sources.uploads[A] = [entry("new2"), entry("new1"), entry("new0"), *old]
    sync.sources.uploads[B] = [entry("bnew", B), *sync.sources.uploads[B]]
    sync.account.add_errors[("WL", "new1")] = AccountError("YouTube refused the change")
    report = sync.run()

    assert sync.account.added == wl("new0", "bnew")
    assert "new0" in sync.seen(A)
    assert "new1" not in sync.seen(A)
    assert "new2" not in sync.seen(A)
    assert "YouTube refused the change" in report.errors[0]
    # The failed source keeps its state as it is, also the rows that the clean-up would remove.
    assert len(sync.seen(A)) == MIN_CHECKED + 1

    del sync.account.add_errors[("WL", "new1")]
    sync.run()
    assert sync.account.added == wl("new0", "bnew", "new1", "new2")


def test_a_failed_second_add_of_a_video_does_not_repeat_the_first_add(sync):
    sync.rule("first", ANY, channels=[A])
    sync.rule("second", ANY, channels=[A], target="PL2")
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()

    sync.sources.uploads[A] = [entry("new"), *old]
    sync.account.add_errors[("PL2", "new")] = AccountError("YouTube refused the change")
    sync.run()
    assert "new" not in sync.seen(A)

    del sync.account.add_errors[("PL2", "new")]
    sync.run()

    assert sync.account.added == [("WL", "new"), ("PL2", "new")]
    assert sync.seen(A)["new"] is SEEN


# The mode without a session.


def deferred_details(sync):
    return [row.detail for row in sync.action_rows() if row.action is RunActionKind.DEFERRED]


def test_a_session_that_dies_during_the_run_defers_each_matching_video_and_the_run_goes_on(sync):
    sync.rule("all", ANY, channels=[A, B])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.sources.uploads[B] = entries("b", 25, B)
    sync.run()

    sync.sources.uploads[A] = [entry("new2"), entry("new1"), entry("new0"), *old]
    sync.sources.uploads[B] = [entry("bnew", B), *sync.sources.uploads[B]]
    refused = NotLoggedInError("YouTube answered as to an anonymous user")
    sync.account.add_errors[("WL", "new1")] = refused
    report = sync.run()

    assert sync.session_deaths == [refused]
    # After the refused add, the run tries no more adds.
    assert sync.account.tried == wl("new0", "new1")
    assert (sync.seen(A)["new0"], sync.seen(A)["new1"], sync.seen(A)["new2"]) == (SEEN, DEFERRED, DEFERRED)
    assert sync.seen(B)["bnew"] is DEFERRED
    assert deferred_details(sync) == [
        f"Video {video_id}: It matches, but it waits for the YouTube account. {DEAD_SESSION_TEXT}"
        for video_id in ("new1", "new2", "bnew")
    ]
    assert report.errors == [f"3 matching video(s) wait for the YouTube account: {DEAD_SESSION_TEXT}"]
    assert sync.sent[-1].event is Event.RUN_SUMMARY

    # A later run with a live session adds the waiting videos once.
    del sync.account.add_errors[("WL", "new1")]
    sync.run()

    assert sync.account.added == wl("new0", "new1", "new2", "bnew")
    assert (sync.seen(A)["new1"], sync.seen(A)["new2"], sync.seen(B)["bnew"]) == (SEEN, SEEN, SEEN)


def test_a_session_that_dies_at_the_feed_gives_the_feed_an_error_and_the_run_goes_on(sync):
    sync.rule("feed", ANY)
    sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = entries("a", 5)
    refused = NotLoggedInError("YouTube sent the request to the Google sign-in page")
    sync.account.feed_error = refused

    report = sync.run()

    assert sync.session_deaths == [refused]
    assert report.errors == [f"Cannot read the subscriptions feed: {DEAD_SESSION_TEXT}"]
    assert len(sync.seen(A)) == 5


def test_a_feed_error_is_reported_and_the_run_goes_on(sync):
    sync.rule("feed", ANY)
    sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = entries("a", 5)
    sync.account.feed_error = AccountError("yt-dlp failed to read the feed")

    report = sync.run()

    assert report.errors == ["Cannot read the subscriptions feed: yt-dlp failed to read the feed"]
    assert len(sync.seen(A)) == 5


def test_without_the_account_the_run_reads_and_evaluates_but_a_matching_video_waits(sync):
    sync.account_error = AccountUnavailable("No YouTube cookies are stored.")
    sync.rule("long", LONG, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()
    sync.sources.uploads[A] = [entry("tiny", duration_s=30), *old]
    sync.run()
    assert sync.account_opens == 0
    assert sync.seen(A)["tiny"] is SEEN

    sync.sources.uploads[A] = [entry("new1"), entry("new0"), entry("tiny", duration_s=30), *old]
    report = sync.run()

    # The run asks for the account once, and goes on without it.
    assert sync.account_opens == 1
    assert (sync.seen(A)["new0"], sync.seen(A)["new1"]) == (DEFERRED, DEFERRED)
    assert deferred_details(sync) == [
        f"Video {video_id}: It matches, but it waits for the YouTube account. No YouTube cookies are stored."
        for video_id in ("new0", "new1")
    ]
    assert report.errors == ["2 matching video(s) wait for the YouTube account: No YouTube cookies are stored."]
    assert sync.session_deaths == []

    sync.account_error = None
    sync.run()

    assert sync.account.added == wl("new0", "new1")
    assert (sync.seen(A)["new0"], sync.seen(A)["new1"]) == (SEEN, SEEN)


def test_a_feed_rule_without_the_account_gets_an_error_and_keeps_its_state(sync):
    sync.account_error = AccountUnavailable("COOKIES_KEY is not set")
    sync.rule("feed", ANY)

    report = sync.run()

    assert report.errors == ["Cannot read the subscriptions feed: COOKIES_KEY is not set"]
    assert sync.actions() == [(ERROR, None, None, None)]
    assert sync.sent[-1].event is Event.RUN_SUMMARY
    with sync.sessions() as session:
        assert session.get(Setting, FEED_READ_KEY) is None


# Notifications.


def test_the_run_summary_lists_the_added_videos_and_the_errors(sync):
    sync.rule("all", ANY, channels=[A, B])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.sources.uploads[B] = entries("b", 25, B)
    sync.playlist("WL", "Watch later")
    sync.run()

    sync.sources.uploads[A] = [entry("new", title="A new video"), *old]
    sync.sources.errors[B] = PublicSourceError("HTTP Error 404")
    sync.run()

    summary = sync.sent[-1]
    assert summary.event is Event.RUN_SUMMARY
    assert "1 video added, 1 error" in summary.title
    assert "A new video -> Playlist WL" in summary.body
    assert "https://www.youtube.com/watch?v=new" in summary.body
    assert "HTTP Error 404" in summary.body


@apprise_notify(on="testsync")
def _capture(body, title, notify_type, *args, meta, **kwargs):
    DELIVERED.append(title)
    return True


DELIVERED = []


def test_the_real_wiring_reads_through_yt_dlp_functions_and_sends_to_the_stored_targets(
    sync, db_sessions, tmp_path, monkeypatch
):
    reads = []

    def iter_channel_uploads(channel_id):
        reads.append(("uploads", channel_id))
        return iter(sync.sources.uploads[channel_id])

    def shorts_video_ids(channel_id, limit=None):
        reads.append(("shorts", channel_id, limit))
        return {"short", "short2"}

    monkeypatch.setattr(public, "iter_channel_uploads", iter_channel_uploads)
    monkeypatch.setattr(public, "shorts_video_ids", shorts_video_ids)
    with db_sessions.begin() as session:
        session.add(NotificationTarget(name="t", url="testsync://t", min_level=0, events=["run_summary"], enabled=True))
    sync.rule("no shorts", ANY, channels=[A], no_shorts=True)
    old = entries("old", 30)
    sync.sources.uploads[A] = [entry("short", duration_s=20), *old]
    settings = Settings(home=tmp_path, database_url="sqlite://", tz="UTC", cookies_key=None)
    DELIVERED.clear()

    sync.run()
    sync.sources.uploads[A] = [entry("short2", duration_s=20), entry("short", duration_s=20), *old]
    with db_sessions.begin() as session:
        run = Run(status=RunStatus.RUNNING, trigger=RunTrigger.MANUAL)
        session.add(run)
        session.flush()
        run_id = run.id
    # The video is a Short, so the run needs no account, although no cookie key is set.
    report = run_with_youtube(db_sessions, run_id, settings)

    assert reads == [("uploads", A), ("shorts", A, SHORTS_LIMIT)]
    assert report.added == []
    assert DELIVERED == ["YouTube AutoManager: 0 videos added"]


def test_a_failing_notification_does_not_fail_the_run(sync):
    sync.rule("all", ANY, channels=[A, B])
    sync.sources.errors[B] = PublicSourceError("HTTP Error 404")
    sync.sources.uploads[A] = entries("a", 5)
    sync.send_error = RuntimeError("the targets are broken")

    report = sync.run()

    assert len(report.errors) == 1
    assert [notification.event for notification in sync.sent] == [Event.CHANNEL_ERROR, Event.RUN_SUMMARY]
    assert len(sync.seen(A)) == 5
