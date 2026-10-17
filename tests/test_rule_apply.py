"""Tests of the preview and the apply of one rule with the test database, fake sources and a fake account.

No test reads YouTube.
"""

import dataclasses
import datetime

import pytest
from sqlalchemy import select
from test_sync import ANY, DAY, LONG, A, B, C, entries, entry, feed_video, upcoming, video_ids, wl

from youtube_automanager.db.models import (
    AddedVideo,
    Channel,
    Run,
    RunActionKind,
    RunStatus,
    RunTrigger,
    SeenVideo,
    SeenVideoState,
    Setting,
)
from youtube_automanager.engine.account_session import DEAD_SESSION_TEXT, AccountUnavailable
from youtube_automanager.engine.modes import MIN_CHECKED
from youtube_automanager.engine.rule_apply import (
    ApplyAborted,
    RuleRefused,
    RuleVersion,
    apply_with_youtube,
    draft_version,
    load_request,
    next_run_preview,
    preview,
)
from youtube_automanager.engine.rule_apply import RuleApply as Apply
from youtube_automanager.engine.rule_preview import (
    APPLY_LIMIT,
    PREVIEW_LIMIT,
    READ_LIMIT,
    DraftRule,
    Mark,
)
from youtube_automanager.engine.sync import (
    FEED_READ_KEY,
    FEED_TITLE,
    PROCESSING_MODE_KEY,
    SHORTS_LIMIT,
    STATE_MODE_KEY,
)
from youtube_automanager.notify import Event
from youtube_automanager.settings import Settings
from youtube_automanager.youtube.account import AccountError, NotLoggedInError
from youtube_automanager.youtube.public import PublicSourceError

SINCE = DAY - datetime.timedelta(days=7)
NOT_SHORT = {"!": [{"var": "is_short"}]}
ADDED = RunActionKind.ADDED
ERROR = RunActionKind.ERROR


def day(days_back):
    return DAY - datetime.timedelta(days=days_back)


def draft(condition=ANY, channels=(), no_shorts=False):
    return DraftRule(condition, tuple(channels), no_shorts)


class Harness:
    """The preview and the apply on the database and the fakes of the sync harness."""

    def __init__(self, sync):
        self.sync = sync
        self.sessions = sync.sessions
        self.sources = sync.sources
        self.account = sync.account
        self.feed_reads = 0
        self.run_id = None

    def read_feed(self):
        self.feed_reads += 1
        return self.account.iter_subscription_feed()

    def preview(self, rule_id=None, draft=None, since=SINCE):
        request = load_request(self.sessions, rule_id, draft)
        return preview(
            self.sessions,
            request,
            since,
            read_uploads=self.sources.read_uploads,
            read_shorts=self.sources.read_shorts,
            read_feed=self.read_feed,
        )

    def next_run(self, rule_id=None, draft=None):
        request = load_request(self.sessions, rule_id, draft)
        return next_run_preview(
            self.sessions,
            request,
            read_uploads=self.sources.read_uploads,
            read_shorts=self.sources.read_shorts,
            read_feed=self.read_feed,
        )

    def apply(self, rule_id, since=SINCE):
        with self.sessions.begin() as session:
            run = Run(status=RunStatus.RUNNING, trigger=RunTrigger.RULE_APPLY)
            session.add(run)
            session.flush()
            self.run_id = run.id
        self.sync.run_id = self.run_id
        self.sync.sent.clear()
        return Apply(
            self.sessions,
            self.run_id,
            rule_id,
            since,
            read_uploads=self.sources.read_uploads,
            read_shorts=self.sources.read_shorts,
            open_account=self.sync.open_account,
            send=self.sync.send,
            session_died=self.sync.session_deaths.append,
            clock=lambda: self.sync.now,
        ).run()

    def added(self, playlist_id="WL"):
        with self.sessions() as session:
            return {
                row.video_id: row.rule_id
                for row in session.scalars(select(AddedVideo).where(AddedVideo.playlist_id == playlist_id))
            }

    def store_added(self, video_id, playlist_id="WL", rule_id=None):
        self.sync.playlist(playlist_id)
        with self.sessions.begin() as session:
            session.add(AddedVideo(playlist_id=playlist_id, video_id=video_id, rule_id=rule_id))


@pytest.fixture
def rules(sync):
    return Harness(sync)


def marks(result):
    return [(video.video_id, video.mark) for video in result.videos]


# The read.


def test_the_read_stops_at_the_first_dated_entry_before_the_day(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = [
        entry("today"),
        entry("first day", day=SINCE),
        entry("day before", day=day(8)),
        entry("older", day=day(1)),
    ]

    result = rules.preview(rule_id)

    assert marks(result) == [("today", Mark.WILL_ADD), ("first day", Mark.WILL_ADD)]
    assert sync.sources.consumed[A] == 3
    assert result.cut_sources == ()


def test_an_entry_without_a_date_never_stops_the_read(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = [entry("new"), entry("no date", day=None), upcoming("soon"), entry("old", day=day(2))]

    result = rules.preview(rule_id)

    assert sorted(marks(result)) == [
        ("new", Mark.WILL_ADD),
        ("no date", Mark.WILL_ADD),
        ("old", Mark.WILL_ADD),
        ("soon", Mark.UNDECIDED),
    ]
    assert sync.sources.consumed[A] == 4


def test_the_read_of_a_source_stops_after_its_limit_and_the_preview_names_the_source(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A, B])
    sync.sources.uploads[A] = entries("a", READ_LIMIT + 20)
    sync.sources.uploads[B] = entries("b", 3, B)

    result = rules.preview(rule_id)

    assert sync.sources.consumed[A] == READ_LIMIT
    assert result.cut_sources == (f"Channel {A}",)
    assert result.count(Mark.WILL_ADD) == READ_LIMIT + 3


def test_a_read_that_meets_the_day_at_its_last_entry_is_not_cut(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = [*entries("a", READ_LIMIT - 1), entry("old", day=day(8)), entry("older", day=day(9))]

    result = rules.preview(rule_id)

    assert sync.sources.consumed[A] == READ_LIMIT
    assert result.cut_sources == ()
    assert result.total == READ_LIMIT - 1


def test_the_preview_lists_its_limit_of_videos_and_counts_the_rest(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = entries("a", PREVIEW_LIMIT + 30)

    result = rules.preview(rule_id)

    assert len(result.videos) == PREVIEW_LIMIT
    assert result.videos[0].video_id == "a00"
    assert (result.total, result.more) == (PREVIEW_LIMIT + 30, 30)


def test_the_videos_of_all_sources_come_newest_first_and_each_video_once(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A, B])
    sync.sources.uploads[A] = [entry("a1", day=day(1)), entry("a2", day=day(1)), entry("a3", day=day(3))]
    sync.sources.uploads[B] = [upcoming("b0", B), entry("b1", B, day=day(0)), entry("b2", B, day=day(2))]
    # A video of two channels together comes in the uploads of each channel.
    sync.sources.uploads[B].append(entry("a3", A, day=day(3)))

    result = rules.preview(rule_id)

    assert [video.video_id for video in result.videos] == ["b0", "b1", "a1", "a2", "b2", "a3"]


def test_a_channel_that_fails_to_read_gives_an_error_and_the_other_channels_still_count(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A, B])
    sync.channel(B, "Bravo")
    sync.sources.uploads[A] = [entry("a0")]
    sync.sources.errors[B] = PublicSourceError("yt-dlp failed to read the uploads")

    result = rules.preview(rule_id)

    assert marks(result) == [("a0", Mark.WILL_ADD)]
    assert result.errors == (f"Channel Channel {B}: yt-dlp failed to read the uploads",)


def test_a_rule_without_channels_reads_the_feed_and_a_rule_with_channels_does_not(sync, rules):
    feed_rule = sync.rule("feed", ANY)
    channel_rule = sync.rule("channel", ANY, channels=[A])
    sync.account.feed = [feed_video("f0", B), feed_video("f1", C, day=day(9))]
    sync.sources.uploads[A] = [entry("a0")]

    assert marks(rules.preview(channel_rule)) == [("a0", Mark.WILL_ADD)]
    assert rules.feed_reads == 0
    assert marks(rules.preview(feed_rule)) == [("f0", Mark.WILL_ADD)]
    assert rules.feed_reads == 1
    assert sync.sources.upload_reads == [A]


def test_a_failed_read_of_the_feed_ends_the_preview_with_its_error(sync, rules):
    rule_id = sync.rule("feed", ANY)
    sync.account.feed_error = NotLoggedInError("YouTube answered as to an anonymous user")

    with pytest.raises(NotLoggedInError):
        rules.preview(rule_id)


def test_the_channel_of_a_video_shows_by_its_stored_title(sync, rules):
    rule_id = sync.rule("feed", ANY)
    sync.channel(A, "Alpha")
    sync.account.feed = [
        feed_video("stored", A),
        dataclasses.replace(feed_video("unknown", B), channel_title="Bravo"),
        feed_video("together", None),
    ]

    result = rules.preview(rule_id)

    assert [(video.video_id, video.channel_title) for video in result.videos] == [
        ("stored", "Alpha"),
        ("unknown", "Bravo"),
        ("together", ""),
    ]


# The evaluation and the Shorts.


def test_a_rule_without_shorts_reads_the_shorts_of_a_channel_once_and_only_when_it_must(sync, rules):
    rule_id = sync.rule("long, no Shorts", LONG, channels=[A, B], no_shorts=True)
    sync.sources.uploads[A] = [
        entry("short", duration_s=40),
        entry("long1"),
        entry("long2"),
        entry("tiny", duration_s=5),
    ]
    sync.sources.uploads[B] = [entry("b-short", B, duration_s=50)]
    sync.sources.shorts[A] = {"long2"}

    result = rules.preview(rule_id)

    # A video under 120 s does not match whether or not it is a Short, so it needs no read of the Shorts.
    assert sync.sources.shorts_reads == [(A, SHORTS_LIMIT)]
    assert marks(result) == [("long1", Mark.WILL_ADD)]


def test_a_rule_that_reads_is_short_gets_the_shorts_only_for_an_unknown_verdict(sync, rules):
    rule_id = sync.rule("Shorts only", {"==": [{"var": "is_short"}, True]}, channels=[A])
    sync.sources.uploads[A] = [entry("s1"), entry("v1")]
    sync.sources.shorts[A] = {"s1"}

    result = rules.preview(rule_id)

    assert marks(result) == [("s1", Mark.WILL_ADD)]
    assert sync.sources.shorts_reads == [(A, SHORTS_LIMIT)]


def test_a_rule_without_a_need_for_is_short_reads_no_shorts(sync, rules):
    rule_id = sync.rule("long", LONG, channels=[A])
    sync.sources.uploads[A] = [entry("short", duration_s=30), entry("long")]

    rules.preview(rule_id)

    assert sync.sources.shorts_reads == []


def test_shorts_that_fail_to_read_leave_the_video_undecided_with_an_error(sync, rules):
    rule_id = sync.rule("no Shorts", ANY, channels=[A], no_shorts=True)
    sync.sources.uploads[A] = [entry("v1"), entry("v2")]
    sync.sources.shorts_errors[A] = PublicSourceError("no answer")

    result = rules.preview(rule_id)

    assert marks(result) == [("v1", Mark.UNDECIDED), ("v2", Mark.UNDECIDED)]
    assert result.errors == (f"Channel Channel {A}: Cannot read the Shorts of the channel: no answer",)
    assert sync.sources.shorts_reads == [(A, SHORTS_LIMIT)]


# The marks of the preview of a saved rule.


def test_the_preview_of_a_saved_rule_marks_each_video_as_the_apply_treats_it(sync, rules):
    rule_id = sync.rule("long", LONG, channels=[A])
    rules.store_added("added")
    rules.store_added("in another playlist", playlist_id="PLother")
    sync.sources.uploads[A] = [
        upcoming("soon"),
        entry("live", duration_s=None, live_status="is_live"),
        entry("new"),
        entry("added"),
        entry("in another playlist"),
        entry("no duration", duration_s=None),
        entry("short", duration_s=30),
    ]

    result = rules.preview(rule_id)

    assert marks(result) == [
        ("soon", Mark.UNDECIDED),
        ("live", Mark.UNDECIDED),
        ("new", Mark.WILL_ADD),
        ("added", Mark.ALREADY_ADDED),
        ("in another playlist", Mark.WILL_ADD),
        ("no duration", Mark.UNDECIDED),
    ]
    assert result.counts == {Mark.UNDECIDED: 3, Mark.WILL_ADD: 2, Mark.ALREADY_ADDED: 1}
    assert result.errors == ()


def test_a_preview_writes_nothing(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = [entry("new")]

    rules.preview(rule_id)

    assert rules.added() == {}
    assert sync.seen(A) == {}
    assert sync.account.tried == []
    assert sync.channel_row(A).last_checked_at is None


# The preview of a draft.


def test_the_preview_of_a_new_rule_lists_the_videos_that_it_matches_or_cannot_decide(sync, rules):
    sync.channel(A)
    sync.sources.uploads[A] = [entry("long"), entry("short", duration_s=30), entry("no duration", duration_s=None)]
    rules.store_added("long")

    result = rules.preview(draft=draft(LONG, [A]))

    assert marks(result) == [("long", Mark.MATCHES), ("no duration", Mark.UNDECIDED)]


def test_the_preview_of_a_new_rule_with_no_shorts_gets_the_shorts_check(sync, rules):
    sync.channel(A)
    sync.sources.uploads[A] = [entry("short"), entry("video")]
    sync.sources.shorts[A] = {"short"}

    result = rules.preview(draft=draft(ANY, [A], no_shorts=True))

    assert marks(result) == [("video", Mark.MATCHES)]


def test_a_draft_with_a_condition_that_is_not_valid_is_refused(sync, rules):
    with pytest.raises(RuleRefused, match="The condition is not valid: The field 'channel_title' is not a video field"):
        rules.preview(draft=draft({"var": "channel_title"}, [A]))


def test_a_saved_rule_that_is_gone_is_refused(sync, rules):
    with pytest.raises(RuleRefused, match="The rule does not exist any more."):
        rules.preview(42)


def test_the_preview_of_a_change_lists_only_the_videos_whose_verdict_changes(sync, rules):
    rule_id = sync.rule("long", LONG, channels=[A])
    sync.sources.uploads[A] = [
        entry("both", duration_s=600),
        entry("only saved", duration_s=200),
        entry("only draft", duration_s=50),
        entry("neither", duration_s=100),
        entry("unknown", duration_s=None),
    ]
    changed = {"or": [{">=": [{"var": "duration_s"}, 300]}, {"<": [{"var": "duration_s"}, 60]}]}

    result = rules.preview(rule_id, draft(changed, [A]))

    assert marks(result) == [("only saved", Mark.NO_LONGER_MATCHES), ("only draft", Mark.MATCHES_NOW)]


def test_a_draft_that_changes_nothing_lists_nothing(sync, rules):
    rule_id = sync.rule("long", LONG, channels=[A], no_shorts=True)
    sync.sources.uploads[A] = [entry("v1"), entry("v2", duration_s=30)]

    result = rules.preview(rule_id, draft(LONG, [A], no_shorts=True))

    assert result.videos == ()
    assert result.counts == {}


def test_a_change_of_the_channels_compares_the_videos_of_both_versions(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A, B])
    sync.sources.uploads[A] = [entry("a0")]
    sync.sources.uploads[B] = [entry("b0", B)]
    sync.sources.uploads[C] = [entry("c0", C)]
    sync.channel(C)

    result = rules.preview(rule_id, draft(ANY, [A, C]))

    assert sorted(marks(result)) == [("b0", Mark.NO_LONGER_MATCHES), ("c0", Mark.MATCHES_NOW)]
    assert sorted(sync.sources.upload_reads) == [A, B, C]


def test_a_change_from_the_feed_to_a_channel_reads_both(sync, rules):
    rule_id = sync.rule("feed", ANY)
    sync.account.feed = [feed_video("a0", A), feed_video("b0", B)]
    sync.sources.uploads[A] = [entry("a0"), entry("a1")]

    result = rules.preview(rule_id, draft(ANY, [A]))

    assert sorted(marks(result)) == [("a1", Mark.MATCHES_NOW), ("b0", Mark.NO_LONGER_MATCHES)]
    assert rules.feed_reads == 1


def test_a_change_of_no_shorts_marks_the_shorts(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = [entry("short"), entry("video")]
    sync.sources.shorts[A] = {"short"}

    result = rules.preview(rule_id, draft(ANY, [A], no_shorts=True))

    assert marks(result) == [("short", Mark.NO_LONGER_MATCHES)]


def test_the_version_of_a_draft_is_built_as_a_saved_rule(sync):
    version = draft_version(draft(LONG, [A, B], no_shorts=True), rule_id=3)

    assert version == RuleVersion(version.spec, frozenset({A, B}))
    assert version.spec.tree == {"and": [LONG, NOT_SHORT]}
    assert (version.spec.id, version.spec.needs_is_short) == (3, True)
    assert draft_version(draft(LONG)).source_keys == frozenset({"feed"})


# The preview of the next run.


def test_the_preview_of_the_next_run_lists_what_the_next_run_evaluates(sync, rules):
    sync.rule("long", LONG, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()
    sync.sources.uploads[A] = [upcoming("soon"), entry("new long"), entry("new short", duration_s=60), *old]

    result = rules.next_run(draft=draft(LONG, [A]))

    # Each old video matches too, but the run saw it before, so the preview leaves it out.
    assert marks(result) == [("soon", Mark.UNDECIDED), ("new long", Mark.MATCHES)]
    assert (result.baseline_sources, result.mode_changed, result.errors, result.cut_sources) == ((), False, (), ())
    sync.run()
    assert sync.account.added == wl("new long")
    assert sync.seen(A)["soon"] is SeenVideoState.DEFERRED


def test_the_preview_of_the_next_run_reads_only_the_window_of_the_mode(sync, rules):
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()
    sync.sources.uploads[A] = [entry("new"), *old]

    result = rules.next_run(draft=draft(ANY, [A]))

    assert marks(result) == [("new", Mark.MATCHES)]
    assert sync.sources.consumed[A] == MIN_CHECKED


def test_the_preview_of_the_next_run_checks_a_deferred_video_again(sync, rules):
    sync.rule("all", LONG, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = [upcoming("soon"), *old]
    sync.run()
    assert sync.seen(A)["soon"] is SeenVideoState.DEFERRED

    result = rules.next_run(draft=draft(ANY, [A]))

    assert marks(result) == [("soon", Mark.MATCHES)]


def test_mode_newest_previews_the_newest_entries_that_the_run_did_not_see(sync, rules):
    sync.mode("newest", 3)
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 10)
    sync.sources.uploads[A] = old
    sync.run()
    sync.sources.uploads[A] = [*entries("new", 5), *old]

    result = rules.next_run(draft=draft(ANY, [A]))

    assert marks(result) == [("new00", Mark.MATCHES), ("new01", Mark.MATCHES), ("new02", Mark.MATCHES)]
    sync.run()
    assert sync.account.added == wl("new02", "new01", "new00")


def test_mode_mark_date_previews_the_videos_since_the_mark(sync, rules):
    sync.mode("mark_date")
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 5, day=day(3))
    sync.sources.uploads[A] = old
    sync.run()
    sync.sources.uploads[A] = [entry("new", day=day(1)), *old]

    result = rules.next_run(draft=draft(ANY, [A]))

    assert marks(result) == [("new", Mark.MATCHES)]
    sync.run()
    assert sync.account.added == wl("new")


def test_a_source_without_state_is_a_baseline_of_the_next_run_and_the_preview_does_not_read_it(sync, rules):
    sync.rule("all", ANY, channels=[A])
    old = entries("a", 25, A)
    sync.sources.uploads[A] = old
    sync.run()
    sync.channel(B, "Beta")
    sync.sources.uploads[A] = [entry("new", A), *old]
    sync.sources.uploads[B] = entries("b", 25, B)
    sync.sources.upload_reads.clear()

    result = rules.next_run(draft=draft(ANY, [A, B]))

    assert marks(result) == [("new", Mark.MATCHES)]
    assert result.baseline_sources == ("Beta",)
    assert sync.sources.upload_reads == [A]


def test_a_new_rule_on_a_new_channel_previews_only_the_baseline(sync, rules):
    sync.channel(A, "Alpha")
    sync.sources.uploads[A] = entries("a", 25, A)

    result = rules.next_run(draft=draft(ANY, [A]))

    assert (result.videos, result.counts, result.baseline_sources) == ((), {}, ("Alpha",))
    assert sync.sources.upload_reads == []


def test_after_a_change_of_the_mode_the_next_run_starts_each_source_again(sync, rules):
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()
    sync.mode("newest", 25)
    sync.sources.uploads[A] = [entry("new"), *old]
    sync.sources.upload_reads.clear()

    result = rules.next_run(draft=draft(ANY, [A]))

    assert (result.videos, result.mode_changed, result.baseline_sources) == ((), True, (f"Channel {A}",))
    assert sync.sources.upload_reads == []
    sync.run()
    assert sync.account.added == []


def test_a_first_record_of_the_mode_keeps_the_state(sync, rules):
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()
    with sync.sessions.begin() as session:
        session.delete(session.get(Setting, STATE_MODE_KEY))
    sync.sources.uploads[A] = [entry("new"), *old]

    result = rules.next_run(draft=draft(ANY, [A]))

    assert (marks(result), result.mode_changed) == ([("new", Mark.MATCHES)], False)


def test_an_invalid_processing_mode_gives_its_error_and_reads_nothing(sync, rules):
    sync.rule("all", ANY, channels=[A])
    sync.setting(PROCESSING_MODE_KEY, {"mode": "unknown"})

    result = rules.next_run(draft=draft(ANY, [A]))

    (error,) = result.errors
    assert error.startswith("The setting 'processing_mode' is not valid: unknown mode 'unknown'")
    assert sync.sources.upload_reads == []


def test_the_feed_of_the_next_run_reads_its_window_with_the_account(sync, rules):
    sync.rule("feed", ANY)
    old = [feed_video(f"old{index:02}") for index in range(30)]
    sync.account.feed = old
    sync.run()
    sync.account.feed = [feed_video("new"), *old]

    result = rules.next_run(draft=draft(ANY))

    assert marks(result) == [("new", Mark.MATCHES)]
    assert rules.feed_reads == 1


def test_a_feed_without_state_is_a_baseline_and_needs_no_read(sync, rules):
    result = rules.next_run(draft=draft(ANY))

    assert result.baseline_sources == (FEED_TITLE,)
    assert rules.feed_reads == 0


def test_the_preview_of_the_next_run_of_a_change_lists_the_new_videos_whose_verdict_changes(sync, rules):
    rule_id = sync.rule("long", LONG, channels=[A])
    # The old videos are short, so the change would match each of them, but the run saw them before.
    old = entries("old", 30, duration_s=60)
    sync.sources.uploads[A] = old
    sync.run()
    sync.sources.uploads[A] = [entry("new long"), entry("new short", duration_s=60), *old]

    result = rules.next_run(rule_id, draft({"<": [{"var": "duration_s"}, 120]}, [A]))

    assert marks(result) == [("new long", Mark.NO_LONGER_MATCHES), ("new short", Mark.MATCHES_NOW)]


def test_a_channel_that_fails_to_read_in_the_next_run_gives_an_error_and_the_other_channels_still_count(sync, rules):
    sync.rule("all", ANY, channels=[A, B])
    sync.sources.uploads[A] = entries("a", 25, A)
    sync.sources.uploads[B] = entries("b", 25, B)
    sync.run()
    sync.sources.uploads[A] = [entry("new", A), *entries("a", 25, A)]
    sync.sources.errors[B] = PublicSourceError("no answer")

    result = rules.next_run(draft=draft(ANY, [A, B]))

    assert marks(result) == [("new", Mark.MATCHES)]
    assert result.errors == (f"Channel Channel {B}: no answer",)


def test_the_preview_of_the_next_run_writes_nothing(sync, rules):
    sync.rule("all", ANY, channels=[A])
    old = entries("old", 30)
    sync.sources.uploads[A] = old
    sync.run()
    sync.sources.uploads[A] = [entry("new"), *old]
    before = sync.seen_rows(A)

    rules.next_run(draft=draft(ANY, [A]))

    assert sync.seen_rows(A).keys() == before.keys()
    assert sync.account.added == []


# The apply.


def test_the_apply_adds_the_matching_videos_the_oldest_first_and_records_each_add(sync, rules):
    rule_id = sync.rule("long", LONG, channels=[A, B], target="PLhome")
    sync.sources.uploads[A] = [entry("a-new", day=day(0)), entry("a-short", duration_s=30), entry("a-old", day=day(5))]
    sync.sources.uploads[B] = [entry("b-mid", B, day=day(3)), entry("b-too-old", B, day=day(10))]

    report = rules.apply(rule_id)

    assert sync.account.added == [("PLhome", "a-old"), ("PLhome", "b-mid"), ("PLhome", "a-new")]
    assert rules.added("PLhome") == {"a-old": rule_id, "b-mid": rule_id, "a-new": rule_id}
    assert sync.actions(rules.run_id) == [
        (ADDED, "a-old", rule_id, "PLhome"),
        (ADDED, "b-mid", rule_id, "PLhome"),
        (ADDED, "a-new", rule_id, "PLhome"),
    ]
    assert [row.detail for row in sync.action_rows(rules.run_id)] == ["Video a-old", "Video b-mid", "Video a-new"]
    run = sync.run_row()
    assert (run.added_count, run.seen_count, run.trigger) == (3, 0, RunTrigger.RULE_APPLY)
    assert (len(report.added), report.errors, report.left) == (3, [], 0)


def test_the_apply_writes_no_state_of_the_sync(sync, rules):
    channel_rule = sync.rule("channel", ANY, channels=[A])
    feed_rule = sync.rule("feed", ANY)
    sync.sources.uploads[A] = [entry("a0"), upcoming("soon")]
    sync.account.feed = [feed_video("f0", B)]

    rules.apply(channel_rule)
    rules.apply(feed_rule)

    assert sync.account.added == [("WL", "a0"), ("WL", "f0")]
    with sync.sessions() as session:
        assert session.scalars(select(SeenVideo)).all() == []
        assert session.scalars(select(Setting.key)).all() == []
        assert session.get(Channel, A).mark_date is None
        assert session.get(Channel, A).last_checked_at is None
    assert sync.run_row().seen_count == 0
    with sync.sessions() as session:
        assert session.get(Setting, FEED_READ_KEY) is None


def test_the_apply_skips_the_added_videos_and_never_adds_an_upcoming_or_undecided_one(sync, rules):
    rule_id = sync.rule("long", LONG, channels=[A])
    rules.store_added("added")
    sync.sources.uploads[A] = [
        upcoming("soon"),
        entry("live", duration_s=900, day=None, live_status="is_live"),
        entry("no duration", duration_s=None),
        entry("added"),
        entry("new"),
    ]

    rules.apply(rule_id)

    assert sync.account.tried == [("WL", "new")]
    assert sync.actions(rules.run_id) == [(ADDED, "new", rule_id, "WL")]


def test_a_second_apply_adds_nothing_after_the_sync_window_moved_past_the_added_videos(sync, rules):
    rule_id = sync.rule("long", LONG, channels=[A])
    applied = entries("applied", 3)
    sync.sources.uploads[A] = applied
    rules.apply(rule_id)
    assert sync.account.added == wl("applied02", "applied01", "applied00")
    sync.run()
    assert set(sync.seen(A)) == set(video_ids(applied))

    # The sync reads short videos that the rule does not match, and its window moves past the applied videos.
    later = entries("later", 25, duration_s=60)
    sync.sources.uploads[A] = [*later, *applied]
    sync.run()
    assert set(sync.seen(A)) == set(video_ids([*later, applied[0]]))
    assert sync.account.added == wl("applied02", "applied01", "applied00")

    report = rules.apply(rule_id)

    assert sync.account.added == wl("applied02", "applied01", "applied00")
    assert (report.added, report.errors) == ([], [])
    assert rules.added() == dict.fromkeys(video_ids(applied), rule_id)


def test_one_apply_adds_at_most_its_limit_and_the_next_apply_adds_the_rest(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A])
    videos = entries("v", APPLY_LIMIT + 30)
    sync.sources.uploads[A] = videos

    first = rules.apply(rule_id)

    oldest = [video.video_id for video in reversed(videos)]
    assert [video_id for _playlist, video_id in sync.account.added] == oldest[:APPLY_LIMIT]
    assert (len(first.added), first.left) == (APPLY_LIMIT, 30)
    assert sync.run_row().added_count == APPLY_LIMIT

    second = rules.apply(rule_id)

    assert [video_id for _playlist, video_id in sync.account.added] == oldest
    assert (len(second.added), second.left) == (30, 0)
    assert len(rules.added()) == APPLY_LIMIT + 30


def test_the_apply_sends_each_added_video_and_the_summary_of_the_run(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A], target="PLhome")
    sync.sources.uploads[A] = [entry("v2"), entry("v1")]

    rules.apply(rule_id)

    first, second, summary = sync.sent
    assert (first.event, second.event, summary.event) == (Event.VIDEO_ADDED, Event.VIDEO_ADDED, Event.RUN_SUMMARY)
    assert first.title == "YouTube AutoManager: video added to Playlist PLhome"
    assert first.body == "Added Video v1 to the playlist Playlist PLhome.\nhttps://www.youtube.com/watch?v=v1"
    assert "https://www.youtube.com/watch?v=v2" in second.body
    assert "Video v1 -> Playlist PLhome" in summary.body
    assert "https://www.youtube.com/watch?v=v1" in summary.body


def test_the_apply_sends_no_added_video_for_a_failed_add(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = [entry("v1")]
    sync.account.add_errors[("WL", "v1")] = AccountError("YouTube did not confirm the change")

    rules.apply(rule_id)

    assert [notification.event for notification in sync.sent] == [Event.RUN_SUMMARY]


def test_a_failed_notification_does_not_fail_the_apply(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = [entry("v1")]
    sync.send_error = RuntimeError("no network")

    report = rules.apply(rule_id)

    assert len(report.added) == 1


@pytest.mark.parametrize(
    "error",
    [
        AccountError("YouTube did not confirm the change"),
        ValueError("not a video id: 'x'"),
    ],
)
def test_a_failed_add_stops_the_apply_with_an_error(sync, rules, error):
    rule_id = sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = [entry("v3"), entry("v2"), entry("v1")]
    sync.account.add_errors[("WL", "v2")] = error

    report = rules.apply(rule_id)

    assert sync.account.tried == [("WL", "v1"), ("WL", "v2")]
    assert rules.added() == {"v1": rule_id}
    assert report.errors == [f"Cannot add video v2 to Playlist WL: {error}"]
    assert sync.actions(rules.run_id) == [(ADDED, "v1", rule_id, "WL"), (ERROR, "v2", rule_id, "WL")]
    assert "1 error" in sync.sent[-1].title


def test_a_session_that_youtube_refuses_during_the_apply_dies_and_the_apply_stops(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = [entry("v2"), entry("v1")]
    refused = NotLoggedInError("YouTube refused the session with HTTP 401")
    sync.account.add_errors[("WL", "v1")] = refused

    report = rules.apply(rule_id)

    assert sync.account.tried == [("WL", "v1")]
    assert sync.session_deaths == [refused]
    assert report.errors == [f"The apply stopped: {DEAD_SESSION_TEXT}"]


def test_an_account_that_is_not_available_stops_the_apply(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A])
    sync.sources.uploads[A] = [entry("v1")]
    sync.account_error = AccountUnavailable("No YouTube cookies are stored.")

    report = rules.apply(rule_id)

    assert sync.account.tried == []
    assert report.errors == ["The apply stopped: No YouTube cookies are stored."]


@pytest.mark.parametrize(
    ("error", "dies", "text"),
    [
        (NotLoggedInError("anonymous"), True, DEAD_SESSION_TEXT),
        (AccountError("HTTP Error 500"), False, "HTTP Error 500"),
    ],
)
def test_a_feed_that_fails_to_read_gives_an_error_and_no_add(sync, rules, error, dies, text):
    rule_id = sync.rule("feed", ANY)
    sync.account.feed_error = error

    report = rules.apply(rule_id)

    assert report.errors == [f"Cannot read {FEED_TITLE}: {text}"]
    assert sync.account.tried == []
    assert bool(sync.session_deaths) is dies


def test_a_channel_that_fails_to_read_gives_an_error_and_the_apply_goes_on(sync, rules):
    rule_id = sync.rule("all", ANY, channels=[A, B])
    sync.sources.uploads[A] = [entry("a0")]
    sync.sources.errors[B] = PublicSourceError("gone")

    report = rules.apply(rule_id)

    assert sync.account.added == [("WL", "a0")]
    assert report.errors == [f"Channel Channel {B}: gone"]
    assert sync.actions(rules.run_id) == [(ERROR, None, None, None), (ADDED, "a0", rule_id, "WL")]
    assert sync.action_rows(rules.run_id)[0].channel_id == B


def test_an_apply_of_a_rule_that_is_gone_is_aborted(sync, rules):
    with pytest.raises(ApplyAborted, match="The rule does not exist any more."):
        rules.apply(42)

    assert sync.account_opens == 0


def test_the_apply_of_a_disabled_rule_adds_its_videos(sync, rules):
    rule_id = sync.rule("off", ANY, channels=[A], enabled=False)
    sync.sources.uploads[A] = [entry("v1")]

    rules.apply(rule_id)

    assert sync.account.added == [("WL", "v1")]


# The apply with the real account session.


def test_an_apply_without_a_live_session_is_aborted_before_any_read(db_sessions, tmp_path, monkeypatch):
    settings = Settings(
        home=tmp_path, database_url="sqlite://", tz="UTC", ytdlp_dir=tmp_path / "yt-dlp", cookies_key=None
    )
    reads = []
    monkeypatch.setattr("youtube_automanager.youtube.public.iter_channel_uploads", reads.append)

    with pytest.raises(ApplyAborted, match="The apply needs the YouTube account. .*COOKIES_KEY is not set"):
        apply_with_youtube(db_sessions, 1, 1, SINCE, settings)

    assert reads == []
