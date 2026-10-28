"""Tests of the rule actions: the preview and the apply of a rule in their child processes, and the parent of each.

No test reads or changes YouTube: fakes stand in for the yt-dlp readers, the account client and the child process.
"""

import datetime
import json
import subprocess
import sys
import threading

import pytest
from sqlalchemy import select
from test_sync import ANY, DAY, LONG, A, B, entry, feed_video

from youtube_automanager import notify, rule_actions
from youtube_automanager.db.models import (
    Account,
    AddedVideo,
    BreakerReason,
    Channel,
    CookieVersion,
    CookieVersionState,
    Playlist,
    Rule,
    RuleChannel,
    Run,
    RunAction,
    RunActionKind,
    RunStatus,
    RunTrigger,
    SeenVideo,
    SeenVideoState,
    SessionState,
    Setting,
    utc_now,
)
from youtube_automanager.engine import account_session
from youtube_automanager.engine.rule_preview import APPLY_LIMIT, DraftRule, Mark, Preview, PreviewVideo
from youtube_automanager.engine.sync import FEED_READ_KEY, FEED_SOURCE
from youtube_automanager.pacing import PAUSE_JITTER_S, PAUSE_S, REFILL_S, RequestBudget
from youtube_automanager.request_pause import RequestPause
from youtube_automanager.rule_actions import (
    PREVIEW_TIMEOUT_SECONDS,
    AppliedRun,
    apply_command,
    apply_in_child,
    apply_once,
    preview_command,
    preview_in_child,
    preview_once,
)
from youtube_automanager.runner import BUSY_TEXT, SCHEDULED_WAIT_S, SOURCE_ROOT, SyncRunner
from youtube_automanager.settings import Settings
from youtube_automanager.short_child import (
    EXIT_LOGGED_OUT,
    EXIT_UNAVAILABLE,
    EXIT_YOUTUBE_ERROR,
    ChildBusy,
    ChildFailed,
    ChildLoggedOut,
    ChildPaused,
    ChildUnavailable,
    ChildYouTubeError,
)
from youtube_automanager.youtube import public
from youtube_automanager.youtube.account import AccountError, NotLoggedInError
from youtube_automanager.youtube.public import PublicSourceError
from youtube_automanager.youtube.session import CookieCipher, generate_key, save_cookies

SINCE = DAY - datetime.timedelta(days=7)
WAIT_SECONDS = 10
COOKIE_VALUE = "fake-sapisid"
COOKIES = (
    "# Netscape HTTP Cookie File\n"
    f".youtube.com\tTRUE\t/\tTRUE\t4000000000\tSAPISID\t{COOKIE_VALUE}\n"
    ".youtube.com\tTRUE\t/\tFALSE\t4000000000\tSIDCC\tfake-sidcc-0\n"
)


def last_stderr_line(capsys) -> str:
    return capsys.readouterr().err.strip().splitlines()[-1]


@pytest.fixture
def settings(tmp_path, db_url):
    return Settings(
        home=tmp_path, database_url=db_url, tz="UTC", ytdlp_dir=tmp_path / "yt-dlp", cookies_key=generate_key()
    )


def store(sessions, *rows):
    with sessions.begin() as session:
        session.add_all(rows)


def store_cookies(sessions, settings, text=COOKIES):
    with sessions.begin() as session:
        save_cookies(session, CookieCipher(settings.cookies_key.get_secret_value()), text)


def store_rule(sessions, rule_id=1, condition=ANY, channels=(), target="WL"):
    store(sessions, Playlist(playlist_id=target, title=f"Playlist {target}", is_own=True))
    store(
        sessions,
        *[Channel(channel_id=channel_id, title=f"Title {channel_id}", is_subscribed=True) for channel_id in channels],
    )
    store(
        sessions,
        Rule(
            id=rule_id,
            name="A rule",
            enabled=True,
            position=1,
            condition=condition,
            target_playlist_id=target,
            no_shorts=False,
        ),
    )
    store(sessions, *[RuleChannel(rule_id=rule_id, channel_id=channel_id) for channel_id in channels])
    return rule_id


def store_read(sessions, channel_id, *video_ids):
    """Store the state of a channel that a run read the day before: its videos as seen, and the time of the read."""
    with sessions.begin() as session:
        session.get(Channel, channel_id).last_checked_at = datetime.datetime(2026, 10, 11, 18, 0, tzinfo=datetime.UTC)
        session.add_all(
            SeenVideo(channel_id=channel_id, video_id=video_id, state=SeenVideoState.SEEN, upload_date=DAY)
            for video_id in video_ids
        )


def store_run(sessions, trigger=RunTrigger.RULE_APPLY):
    with sessions.begin() as session:
        run = Run(status=RunStatus.RUNNING, trigger=trigger)
        session.add(run)
        session.flush()
        return run.id


class FakePublic:
    """Replaces the anonymous reads of yt-dlp. A test fills ``uploads`` and ``shorts``, and an error in ``uploads``
    is raised at the read."""

    def __init__(self):
        self.uploads = {}
        self.shorts = {}
        self.reads = []

    def iter_channel_uploads(self, channel_id):
        self.reads.append(channel_id)
        if isinstance(self.uploads.get(channel_id), Exception):
            raise self.uploads[channel_id]
        return iter(self.uploads.get(channel_id, []))

    def shorts_video_ids(self, channel_id, limit=None):
        self.reads.append(("shorts", channel_id, limit))
        return set(self.shorts.get(channel_id, ()))


@pytest.fixture
def fake_public(monkeypatch):
    fake = FakePublic()
    monkeypatch.setattr(public, "iter_channel_uploads", fake.iter_channel_uploads)
    monkeypatch.setattr(public, "shorts_video_ids", fake.shorts_video_ids)
    return fake


class ClientState:
    """The state of each fake account client: the feed, the errors, and the record of the clients and the adds."""

    def __init__(self):
        self.created = 0
        self.check_error = None
        self.feed = []
        self.feed_error = None
        self.add_errors = {}
        self.added = []


@pytest.fixture
def fake_client(monkeypatch):
    """Replaces the account client of the account session. A test sets the feed and the errors on the state."""
    state = ClientState()

    class FakeClient:
        def __init__(self, text, *, budget):
            state.created += 1
            self.text = text

        def check_session(self):
            if state.check_error is not None:
                raise state.check_error

        def use_cookies(self, text):
            self.text = text

        def cookies_text(self):
            return self.text.replace("fake-sidcc-0", "fake-sidcc-1")

        def iter_subscription_feed(self):
            if state.feed_error is not None:
                raise state.feed_error
            return iter(state.feed)

        def add_video(self, playlist_id, video_id):
            error = state.add_errors.get(video_id)
            if error is not None:
                raise error
            state.added.append((playlist_id, video_id))

    monkeypatch.setattr(account_session, "AccountClient", FakeClient)
    return state


def preview_value(*videos, counts=None, errors=(), cut=()):
    if counts is None:
        counts = {}
        for video in videos:
            counts[video.mark] = counts.get(video.mark, 0) + 1
    return Preview(tuple(videos), counts, tuple(errors), tuple(cut)).to_value()


EMPTY_PREVIEW = Preview((), {}).to_value()


def video(video_id="v1", mark=Mark.WILL_ADD, day=DAY):
    return PreviewVideo(video_id, f"Video {video_id}", "Alpha", day, mark)


# The commands.


def test_the_commands_name_the_child_and_keep_each_value_one_argument():
    draft = DraftRule({"regex": [{"var": "title"}, "Пиріг -x", "i"]}, (A,), True)

    assert preview_command(SINCE, rule_id=3, draft=draft) == [
        sys.executable,
        "-m",
        "youtube_automanager",
        "preview-rule",
        "--since=2026-10-05",
        "--rule-id=3",
        "--draft=" + draft.to_json(),
    ]
    assert draft.to_json().isascii()
    assert DraftRule.from_json(draft.to_json()) == draft
    full = DraftRule(draft.condition, (A,), True, "PLx", SINCE)
    assert DraftRule.from_json(full.to_json()) == full
    assert preview_command(SINCE, rule_id=3)[-1] == "--rule-id=3"
    assert preview_command(None, draft=draft)[3:] == ["preview-rule", "--draft=" + draft.to_json()]
    assert apply_command(7, 3, SINCE) == [
        sys.executable,
        "-m",
        "youtube_automanager",
        "apply-rule",
        "--run-id=7",
        "--rule-id=3",
        "--since=2026-10-05",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        "{}",
        '{"condition": {}, "channel_ids": "UCx", "no_shorts": false}',
        '{"condition": {}, "channel_ids": [], "no_shorts": 0}',
        # The draft of an older version, without the target playlist and the day.
        '{"condition": {}, "channel_ids": [], "no_shorts": false}',
        '{"condition": {}, "channel_ids": [], "no_shorts": false, "playlist_id": 1, "catch_up_since": null}',
        '{"condition": {}, "channel_ids": [], "no_shorts": false, "playlist_id": null, "catch_up_since": "May"}',
        '{"condition": {}, "channel_ids": [], "no_shorts": false, "playlist_id": null, "catch_up_since": 20261005}',
    ],
)
def test_a_text_that_is_not_a_draft_is_refused(text):
    with pytest.raises(ValueError, match="not a draft of a rule"):
        DraftRule.from_json(text)


def test_the_timeout_of_a_preview_is_shorter_than_the_wait_of_a_scheduled_run():
    assert PREVIEW_TIMEOUT_SECONDS < SCHEDULED_WAIT_S


def test_a_scheduled_run_waits_longer_than_the_adds_of_a_full_apply():
    # Each add sends two requests with the cookies, and one request waits at most for one token of the budget, or for
    # the longest pause after an answer.
    assert APPLY_LIMIT * 2 * max(REFILL_S, PAUSE_S + PAUSE_JITTER_S) < SCHEDULED_WAIT_S


# The body of the preview child.


def test_the_preview_of_a_channel_rule_needs_no_cookies_and_writes_its_result_as_the_last_line_of_stderr(
    settings, db_sessions, fake_public, fake_client, capsys
):
    rule_id = store_rule(db_sessions, condition=LONG, channels=[A])
    fake_public.uploads[A] = [
        entry("long"),
        entry("short", duration_s=30),
        entry("old", day=SINCE - datetime.timedelta(days=1)),
    ]

    assert preview_once(settings.model_copy(update={"cookies_key": None}), SINCE, rule_id) == 0

    result = Preview.from_value(json.loads(last_stderr_line(capsys)))
    assert [(item.video_id, item.channel_title, item.mark) for item in result.videos] == [
        ("long", f"Title {A}", Mark.WILL_ADD)
    ]
    assert fake_client.created == 0
    assert fake_public.reads == [A]


def test_the_preview_of_a_draft_compares_it_with_the_saved_rule(settings, db_sessions, fake_public, capsys):
    rule_id = store_rule(db_sessions, condition=LONG, channels=[A])
    fake_public.uploads[A] = [entry("long"), entry("short", duration_s=30)]

    assert preview_once(settings, SINCE, rule_id, DraftRule(ANY, (A,), False)) == 0

    result = Preview.from_value(json.loads(last_stderr_line(capsys)))
    assert [(item.video_id, item.mark) for item in result.videos] == [("short", Mark.MATCHES_NOW)]


def test_the_preview_of_a_rule_that_is_gone_gives_its_error_without_a_read(settings, fake_public, capsys):
    assert preview_once(settings, SINCE, 42) == 0

    assert Preview.from_value(json.loads(last_stderr_line(capsys))) == Preview(
        (), {}, ("The rule does not exist any more.",)
    )
    assert fake_public.reads == []


def test_the_preview_of_a_feed_rule_checks_the_session_reads_the_feed_and_saves_the_cookies(
    settings, db_sessions, fake_public, fake_client, capsys
):
    store_cookies(db_sessions, settings)
    rule_id = store_rule(db_sessions)
    fake_client.feed = [feed_video("f1", B)]

    assert preview_once(settings, SINCE, rule_id) == 0

    result = Preview.from_value(json.loads(last_stderr_line(capsys)))
    assert [(item.video_id, item.mark) for item in result.videos] == [("f1", Mark.WILL_ADD)]
    with db_sessions() as session:
        assert session.scalars(select(Account)).one().session_state is SessionState.OK
        states = [version.state for version in session.scalars(select(CookieVersion).order_by(CookieVersion.id))]
    assert states == [CookieVersionState.GOOD, CookieVersionState.UNCHECKED]
    assert fake_public.reads == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (NotLoggedInError("YouTube answered as to an anonymous user"), EXIT_LOGGED_OUT),
        (AccountError("yt-dlp failed to read the feed:\n HTTP Error 500"), EXIT_YOUTUBE_ERROR),
    ],
)
def test_a_feed_that_fails_to_read_ends_the_preview_with_the_code_of_its_kind(
    settings, db_sessions, fake_public, fake_client, capsys, error, code
):
    store_cookies(db_sessions, settings)
    rule_id = store_rule(db_sessions)
    fake_client.feed_error = error

    assert preview_once(settings, SINCE, rule_id) == code

    assert last_stderr_line(capsys) == " ".join(str(error).split())


def test_the_preview_of_a_feed_rule_without_cookies_ends_with_the_unavailable_code(
    settings, db_sessions, fake_public, fake_client, capsys
):
    assert preview_once(settings, SINCE, draft=DraftRule(ANY, (), False)) == EXIT_UNAVAILABLE

    assert "no YouTube cookies are stored" in last_stderr_line(capsys)


def test_the_preview_of_the_next_run_lists_only_the_new_videos_of_a_channel_that_a_run_read(
    settings, db_sessions, fake_public, capsys
):
    store_rule(db_sessions, condition=LONG, channels=[A])
    store_read(db_sessions, A, "old long")
    fake_public.uploads[A] = [entry("new long"), entry("new short", duration_s=30), entry("old long")]

    assert preview_once(settings, None, draft=DraftRule(LONG, (A,), False)) == 0

    result = Preview.from_value(json.loads(last_stderr_line(capsys)))
    # The settings of the test store no cookies, so the run would defer the match.
    assert [(item.video_id, item.mark) for item in result.videos] == [("new long", Mark.WAITS_FOR_ACCOUNT)]
    assert (result.baseline_sources, result.mode_changed) == ((), False)
    assert fake_public.reads == [A]


def test_the_preview_of_the_next_run_names_a_channel_without_state_and_reads_nothing(
    settings, db_sessions, fake_public, capsys
):
    store_rule(db_sessions, channels=[A])
    fake_public.uploads[A] = [entry("new")]

    assert preview_once(settings, None, draft=DraftRule(ANY, (A,), False)) == 0

    result = Preview.from_value(json.loads(last_stderr_line(capsys)))
    assert (result.videos, result.baseline_sources) == ((), (f"Title {A}",))
    assert fake_public.reads == []


def test_the_preview_of_the_next_run_of_a_feed_rule_reads_the_feed_with_the_account(
    settings, db_sessions, fake_public, fake_client, capsys
):
    store_cookies(db_sessions, settings)
    store_rule(db_sessions)
    with db_sessions.begin() as session:
        session.add(Setting(key=FEED_READ_KEY, value="2026-10-11T18:00:00+00:00"))
        session.add(SeenVideo(channel_id=FEED_SOURCE, video_id="f0", state=SeenVideoState.SEEN, upload_date=DAY))
    fake_client.feed = [feed_video("f1", B), feed_video("f0", B)]

    assert preview_once(settings, None, draft=DraftRule(ANY, (), False)) == 0

    result = Preview.from_value(json.loads(last_stderr_line(capsys)))
    assert [(item.video_id, item.mark) for item in result.videos] == [("f1", Mark.WILL_ADD)]
    assert fake_client.created == 1


def test_a_channel_that_fails_to_read_is_an_error_of_the_preview_and_not_of_the_child(
    settings, db_sessions, fake_public, capsys
):
    rule_id = store_rule(db_sessions, channels=[A])
    fake_public.uploads[A] = PublicSourceError("yt-dlp failed to read the uploads")

    assert preview_once(settings, SINCE, rule_id) == 0

    result = Preview.from_value(json.loads(last_stderr_line(capsys)))
    assert result.errors == (f"Channel Title {A}: yt-dlp failed to read the uploads",)


# The body of the apply child.


def test_the_apply_child_adds_the_matching_videos_and_saves_the_cookies(
    settings, db_sessions, fake_public, fake_client, capsys
):
    store_cookies(db_sessions, settings)
    rule_id = store_rule(db_sessions, condition=LONG, channels=[A])
    run_id = store_run(db_sessions)
    fake_public.uploads[A] = [entry("new"), entry("short", duration_s=30), entry("old", day=SINCE)]

    assert apply_once(run_id, rule_id, SINCE, settings) == 0

    assert fake_client.added == [("WL", "old"), ("WL", "new")]
    with db_sessions() as session:
        assert session.get(Run, run_id).added_count == 2
        assert {row.video_id for row in session.scalars(select(AddedVideo))} == {"old", "new"}
        assert session.scalars(select(Account)).one().session_state is SessionState.OK
        assert len(session.scalars(select(CookieVersion)).all()) == 2


def test_an_apply_without_cookies_ends_with_the_cause_and_adds_nothing(
    settings, db_sessions, fake_public, fake_client, capsys
):
    rule_id = store_rule(db_sessions, channels=[A])
    run_id = store_run(db_sessions)
    fake_public.uploads[A] = [entry("new")]

    assert apply_once(run_id, rule_id, SINCE, settings) == 1

    assert last_stderr_line(capsys) == (
        "The apply needs the YouTube account. The service needs the YouTube account, but no YouTube cookies are "
        "stored. Upload the YouTube cookies."
    )
    assert fake_public.reads == []
    assert fake_client.added == []


def test_an_apply_of_a_rule_that_is_gone_ends_with_the_cause(settings, db_sessions, fake_public, fake_client, capsys):
    store_cookies(db_sessions, settings)
    run_id = store_run(db_sessions)

    assert apply_once(run_id, 42, SINCE, settings) == 1

    assert last_stderr_line(capsys) == "The rule does not exist any more."


def test_an_apply_without_its_run_row_ends_at_once(settings, db_sessions, fake_public, fake_client):
    store_cookies(db_sessions, settings)
    rule_id = store_rule(db_sessions, channels=[A])

    assert apply_once(99, rule_id, SINCE, settings) == 1

    assert fake_client.created == 0


def test_a_failed_add_writes_an_error_and_the_child_still_ends_well(
    settings, db_sessions, fake_public, fake_client, capsys
):
    store_cookies(db_sessions, settings)
    rule_id = store_rule(db_sessions, channels=[A])
    run_id = store_run(db_sessions)
    fake_public.uploads[A] = [entry("v2"), entry("v1")]
    fake_client.add_errors["v1"] = AccountError("YouTube did not confirm the change")

    assert apply_once(run_id, rule_id, SINCE, settings) == 0

    assert fake_client.added == []
    with db_sessions() as session:
        actions = session.scalars(select(RunAction).where(RunAction.run_id == run_id)).all()
        assert [(action.action, action.video_id) for action in actions] == [(RunActionKind.ERROR, "v1")]
        assert session.get(Run, run_id).added_count == 0


# The parent of a preview.


@pytest.fixture
def runner(db_sessions, db_url, tmp_path, fake_starter):
    sent = []
    runner = SyncRunner(
        db_sessions,
        ytdlp_dir=tmp_path / "yt-dlp",
        database_url=db_url,
        child_timeout_s=WAIT_SECONDS,
        start=fake_starter,
        lock=threading.Lock(),
        send=sent.append,
    )
    runner.sent = sent
    return runner


def test_the_preview_gives_the_rule_and_the_draft_to_the_child_and_reads_the_result(runner, fake_starter):
    value = preview_value(video("v1"), video("v2", Mark.UNDECIDED, None), errors=["a channel failed"], cut=["Alpha"])
    fake_starter.plan.append({"stderr": "a warning\n" + json.dumps(value) + "\n\n"})
    draft = DraftRule(ANY, (A,), False)

    result = preview_in_child(runner, SINCE, rule_id=3, draft=draft)

    assert result == Preview.from_value(value)
    assert result.videos[1].upload_date is None
    assert (result.errors, result.cut_sources) == (("a channel failed",), ("Alpha",))
    ((command, _env),) = fake_starter.calls
    assert command == preview_command(SINCE, rule_id=3, draft=draft)


def test_the_preview_of_the_next_run_gives_no_day_to_the_child_and_reads_its_sources(runner, fake_starter):
    value = Preview((), {}, baseline_sources=("Beta",), mode_changed=True).to_value()
    fake_starter.plan.append({"stderr": json.dumps(value) + "\n"})
    draft = DraftRule(ANY, (A, B), False)

    result = preview_in_child(runner, None, rule_id=3, draft=draft)

    assert (result.baseline_sources, result.mode_changed) == (("Beta",), True)
    ((command, _env),) = fake_starter.calls
    assert command == preview_command(None, rule_id=3, draft=draft)
    assert not any(part.startswith("--since") for part in command)


def test_a_long_preview_comes_whole(runner, fake_starter):
    """The runner keeps only the end of a long stderr as the error text, but the result line comes in full."""
    videos = [video(f"v{index:03}") for index in range(200)]
    line = json.dumps(preview_value(*videos, counts={Mark.WILL_ADD: 250}))
    assert len(line) > 10000
    fake_starter.plan.append({"stderr": line + "\n"})

    result = preview_in_child(runner, SINCE, rule_id=1)

    assert (len(result.videos), result.more) == (200, 50)


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"videos": [], "counts": {}},
        {"videos": [], "counts": {}, "errors": [], "cut_sources": []},
        {**EMPTY_PREVIEW, "extra": 1},
        {**EMPTY_PREVIEW, "videos": "x"},
        {**EMPTY_PREVIEW, "counts": {"other": 1}},
        {**EMPTY_PREVIEW, "counts": {"will_add": True}},
        {**EMPTY_PREVIEW, "counts": {"will_add": -1}},
        {**EMPTY_PREVIEW, "errors": [1]},
        {**EMPTY_PREVIEW, "cut_sources": "Alpha"},
        {**EMPTY_PREVIEW, "baseline_sources": "Alpha"},
        {**EMPTY_PREVIEW, "baseline_sources": [1]},
        {**EMPTY_PREVIEW, "mode_changed": 1},
        {**EMPTY_PREVIEW, "mode_changed": None},
        preview_value(video(), counts={}),
        {**preview_value(video()), "videos": [{**video().to_value(), "mark": "other"}]},
        {**preview_value(video()), "videos": [{**video().to_value(), "upload_date": "yesterday"}]},
        {**preview_value(video()), "videos": [{**video().to_value(), "upload_date": 5}]},
        {**preview_value(video()), "videos": [{**video().to_value(), "video_id": ""}]},
        {**preview_value(video()), "videos": [{**video().to_value(), "title": None}]},
        {**preview_value(video()), "videos": [{**video().to_value(), "extra": 1}]},
        preview_value(*[video(f"v{index}") for index in range(201)]),
    ],
)
def test_an_exit_code_0_without_a_preview_is_a_process_error(runner, fake_starter, value):
    fake_starter.plan.append({"stderr": "" if value is None else json.dumps(value) + "\n"})

    with pytest.raises(ChildFailed, match="ended without the result of the preview"):
        preview_in_child(runner, SINCE, rule_id=1)


@pytest.mark.parametrize(
    ("exit_code", "error_class"),
    [(EXIT_UNAVAILABLE, ChildUnavailable), (EXIT_LOGGED_OUT, ChildLoggedOut), (EXIT_YOUTUBE_ERROR, ChildYouTubeError)],
)
def test_each_failure_exit_code_of_a_preview_gives_its_own_error(runner, fake_starter, exit_code, error_class):
    fake_starter.plan.append({"exit_code": exit_code, "stderr": "a warning\nthe cause\n"})

    with pytest.raises(error_class, match="^the cause$"):
        preview_in_child(runner, SINCE, rule_id=1)


def test_a_preview_while_a_run_is_active_starts_nothing_and_is_busy(runner, fake_starter):
    run_id = runner.claim(RunTrigger.MANUAL)
    try:
        with pytest.raises(ChildBusy, match=BUSY_TEXT):
            preview_in_child(runner, SINCE, rule_id=1)
    finally:
        runner.execute(run_id)

    assert len(fake_starter.calls) == 1


def test_a_preview_gets_its_timeout(runner, fake_starter, monkeypatch):
    monkeypatch.setattr(rule_actions, "PREVIEW_TIMEOUT_SECONDS", 0.05)
    fake_starter.plan.append({"blocking": True})

    with pytest.raises(ChildFailed, match="The preview did not end within 0.05 s"):
        preview_in_child(runner, SINCE, rule_id=1)

    assert fake_starter.children[0].killed


def test_a_preview_needs_a_rule_or_a_draft(runner, fake_starter):
    with pytest.raises(ValueError, match="a preview needs a saved rule, a draft or both"):
        preview_in_child(runner, SINCE)

    assert fake_starter.calls == []


# The parent of an apply.


def runs(sessions):
    with sessions() as session:
        return session.scalars(select(Run).order_by(Run.id)).all()


def test_an_apply_is_a_run_with_its_own_trigger_and_child(db_sessions, db_url, tmp_path, fake_starter):
    def start(command, env, input_text):
        # The child of the run writes the count of the added videos.
        with db_sessions.begin() as session:
            session.get(Run, int(command[4].removeprefix("--run-id="))).added_count = 2
        return fake_starter(command, env, input_text)

    runner = SyncRunner(
        db_sessions,
        ytdlp_dir=tmp_path / "yt-dlp",
        database_url=db_url,
        child_timeout_s=WAIT_SECONDS,
        start=start,
        lock=threading.Lock(),
    )

    applied = apply_in_child(runner, db_sessions, 3, SINCE)

    (run,) = runs(db_sessions)
    assert applied == AppliedRun(run.id, RunStatus.SUCCEEDED, 2, None, 0)
    assert (run.trigger, run.status, run.finished_at is not None) == (RunTrigger.RULE_APPLY, RunStatus.SUCCEEDED, True)
    ((command, _env),) = fake_starter.calls
    assert command == apply_command(run.id, 3, SINCE)
    assert not runner.is_active()


def test_a_failed_apply_keeps_its_cause_as_the_error_of_the_run_and_sends_the_failure(
    runner, fake_starter, db_sessions
):
    fake_starter.plan.append({"exit_code": 1, "stderr": "The rule does not exist any more.\n"})

    applied = apply_in_child(runner, db_sessions, 3, SINCE)

    assert (applied.status, applied.error_text, applied.error_count) == (
        RunStatus.FAILED,
        "The rule does not exist any more.",
        0,
    )
    (sent,) = runner.sent
    assert sent.event is notify.Event.RUN_FAILED


@pytest.mark.parametrize("zone", ["UTC", "Europe/Kyiv"])
def test_an_apply_during_the_pause_of_the_requests_writes_no_run_and_starts_nothing(
    runner, fake_starter, db_sessions, zone
):
    paused = RequestBudget(db_sessions, send=lambda notification: None).open_breaker(BreakerReason.BOT_CHECK)

    with pytest.raises(ChildPaused) as raised:
        apply_in_child(runner, db_sessions, 3, SINCE, default_timezone=zone)

    assert raised.value.pause == RequestPause(BreakerReason.BOT_CHECK, paused.pause.until, zone)
    assert runs(db_sessions) == []
    assert fake_starter.calls == []
    assert not runner.is_active()


def test_an_apply_after_the_end_of_the_pause_runs(runner, fake_starter, db_sessions):
    budget = RequestBudget(db_sessions, clock=lambda: utc_now() - datetime.timedelta(hours=7))
    budget.open_breaker(BreakerReason.BOT_CHECK)

    applied = apply_in_child(runner, db_sessions, 3, SINCE)

    assert applied.status is RunStatus.SUCCEEDED
    assert len(fake_starter.calls) == 1


def test_an_apply_while_the_lock_is_taken_writes_no_run_and_starts_nothing(runner, fake_starter, db_sessions):
    fake_starter.plan.append({"blocking": True, "stderr": json.dumps(preview_value()) + "\n"})
    worker = threading.Thread(target=lambda: preview_in_child(runner, SINCE, rule_id=1), daemon=True)
    worker.start()
    assert fake_starter.child_started.wait(WAIT_SECONDS)

    with pytest.raises(ChildBusy, match=BUSY_TEXT):
        apply_in_child(runner, db_sessions, 3, SINCE)

    fake_starter.children[0].finish(0)
    worker.join(WAIT_SECONDS)
    assert runs(db_sessions) == []
    assert len(fake_starter.calls) == 1


def test_a_run_waits_for_an_active_apply(runner, fake_starter, db_sessions):
    fake_starter.plan.append({"blocking": True})
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(apply_in_child(runner, db_sessions, 3, SINCE)), daemon=True)
    worker.start()
    assert fake_starter.child_started.wait(WAIT_SECONDS)

    assert runner.claim(RunTrigger.SCHEDULE, wait_s=0.05) is None

    fake_starter.children[0].finish(0)
    worker.join(WAIT_SECONDS)
    assert outcome[0].status is RunStatus.SUCCEEDED
    run_id = runner.claim(RunTrigger.SCHEDULE)
    assert run_id is not None
    runner.abandon(run_id, "The test needs no child.")


def test_an_apply_tells_the_id_of_its_run_before_the_child_starts(runner, fake_starter, db_sessions):
    told = []

    applied = apply_in_child(
        runner, db_sessions, 3, SINCE, lambda run_id: told.append((run_id, len(fake_starter.calls)))
    )

    assert told == [(applied.run_id, 0)]
    assert len(fake_starter.calls) == 1


def test_a_failed_listener_of_the_run_id_does_not_stop_the_apply(runner, fake_starter, db_sessions, caplog):
    def on_claimed(run_id):
        raise RuntimeError("the event loop is closed")

    applied = apply_in_child(runner, db_sessions, 3, SINCE, on_claimed)

    assert applied.status is RunStatus.SUCCEEDED
    assert not runner.is_active()
    assert f"Run {applied.run_id}: the caller of the apply cannot learn the id of the run" in caplog.text


def test_a_stop_of_the_service_ends_an_active_apply_as_an_interrupted_run(runner, fake_starter, db_sessions):
    fake_starter.plan.append({"blocking": True})
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(apply_in_child(runner, db_sessions, 3, SINCE)), daemon=True)
    worker.start()
    assert fake_starter.child_started.wait(WAIT_SECONDS)

    assert runner.stop(timeout=WAIT_SECONDS) is True

    worker.join(WAIT_SECONDS)
    assert fake_starter.children[0].terminated
    assert (outcome[0].status, outcome[0].error_text) == (RunStatus.FAILED, "The service stopped during the run.")


# The real child processes.


def test_the_real_preview_child_of_a_rule_that_is_gone_gives_its_error_without_a_request(db_sessions, db_url, tmp_path):
    """Starts the real `preview-rule` child: the command, the import path and the database reach the child."""
    real_runner = SyncRunner(
        db_sessions, ytdlp_dir=tmp_path / "yt-dlp", database_url=db_url, child_timeout_s=90, lock=threading.Lock()
    )

    result = preview_in_child(real_runner, SINCE, rule_id=42, draft=DraftRule(ANY, (A,), True))

    assert result == Preview((), {}, ("The rule does not exist any more.",))


def test_the_real_preview_child_of_the_next_run_names_a_channel_without_state_without_a_request(
    db_sessions, db_url, tmp_path
):
    """Starts the real `preview-rule` child without a day: the child previews the next run."""
    real_runner = SyncRunner(
        db_sessions, ytdlp_dir=tmp_path / "yt-dlp", database_url=db_url, child_timeout_s=90, lock=threading.Lock()
    )

    result = preview_in_child(real_runner, None, draft=DraftRule(ANY, (A,), False))

    assert result == Preview((), {}, baseline_sources=(A,))


def test_the_real_apply_child_without_a_cookie_key_fails_the_run_with_the_cause(
    db_sessions, db_url, tmp_path, monkeypatch
):
    """Starts the real `apply-rule` child. Without a cookie key, the session check sends no request."""
    monkeypatch.delenv("COOKIES_KEY", raising=False)
    rule_id = store_rule(db_sessions, channels=[A])
    real_runner = SyncRunner(
        db_sessions,
        ytdlp_dir=tmp_path / "yt-dlp",
        database_url=db_url,
        child_timeout_s=90,
        lock=threading.Lock(),
        send=lambda notification: None,
    )

    applied = apply_in_child(real_runner, db_sessions, rule_id, SINCE)

    assert applied.status is RunStatus.FAILED
    assert applied.error_text is not None
    assert applied.error_text.startswith("The apply needs the YouTube account.")
    assert "COOKIES_KEY is not set" in applied.error_text
    (run,) = runs(db_sessions)
    assert run.trigger is RunTrigger.RULE_APPLY


def test_the_web_process_does_not_import_yt_dlp_for_a_preview_or_an_apply():
    """The preview and the apply run in child processes, so a yt-dlp update reaches each of them."""
    preview_line = json.dumps(preview_value(video()))
    code = (
        "import datetime, io, pathlib, sys, tempfile\n"
        "from fastapi.testclient import TestClient\n"
        "from youtube_automanager.engine.rule_preview import DraftRule\n"
        "from youtube_automanager.rule_actions import apply_in_child, preview_in_child\n"
        "from youtube_automanager.settings import Settings\n"
        "from youtube_automanager.web.app import create_app\n"
        "from youtube_automanager.web.ui import mount_ui\n"
        "\n"
        "LINES = {'preview-rule': " + repr(preview_line) + ", 'apply-rule': ''}\n"
        "\n"
        "class Child:\n"
        "    returncode = 0\n"
        "    def __init__(self, line):\n"
        "        self.stderr = io.StringIO(line + '\\n')\n"
        "    def wait(self, timeout=None):\n"
        "        return 0\n"
        "    def poll(self):\n"
        "        return 0\n"
        "    def terminate(self):\n"
        "        pass\n"
        "    kill = terminate\n"
        "\n"
        "home = pathlib.Path(tempfile.mkdtemp())\n"
        "settings = Settings(home=home, database_url=f'sqlite:///{home / \"service.sqlite\"}', tz='UTC')\n"
        "app = create_app(settings, start=lambda command, env, input_text: Child(LINES[command[3]]))\n"
        "mount_ui(app)\n"
        "since = datetime.date(2026, 10, 5)\n"
        "with TestClient(app) as client:\n"
        "    page = client.get('/rules')\n"
        "    services = client.app.state.services\n"
        "    preview = preview_in_child(services.runner, since, rule_id=1, draft=DraftRule({'==': [1, 1]}, (), False))\n"
        "    applied = apply_in_child(services.runner, services.sessions, 1, since)\n"
        "assert page.status_code == 200, page.text\n"
        "assert [video.video_id for video in preview.videos] == ['v1'], preview\n"
        "assert applied.status.value == 'succeeded', applied\n"
        "assert 'yt_dlp' not in sys.modules, sorted(name for name in sys.modules if name.startswith('yt_dlp'))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=SOURCE_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
