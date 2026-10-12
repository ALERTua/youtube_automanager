import logging
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from youtube_automanager.db.models import (
    AddedVideo,
    Run,
    RunAction,
    RunActionKind,
    RunStatus,
    RunTrigger,
    SeenVideo,
    SeenVideoState,
    Setting,
)
from youtube_automanager.retention import (
    ADDED_VIDEO_KEEP_DAYS,
    DEFAULT_RETENTION_DAYS,
    RETENTION_KEY,
    CleanupResult,
    clean_history,
    delete_old_added_videos,
    delete_old_runs,
    get_retention_days,
    set_retention_days,
)
from youtube_automanager.web import app as web_app
from youtube_automanager.web.app import create_app
from youtube_automanager.web.ui.rules_page import MAX_DAYS_BACK

pytestmark = pytest.mark.usefixtures("logging_setups")

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
WAIT_SECONDS = 10
KEEP = timedelta(days=ADDED_VIDEO_KEEP_DAYS)
CHANNEL = "UCaaaaaaaaaaaaaaaaaaaaaa"


def make_run(run_id, age: timedelta, status=RunStatus.SUCCEEDED, actions=2, now=NOW):
    """A run that started ``age`` before ``now``, with ``actions`` actions."""
    started_at = now - age
    run = Run(id=run_id, started_at=started_at, status=status, trigger=RunTrigger.SCHEDULE)
    return [
        run,
        *[
            RunAction(run_id=run_id, at=started_at, action=RunActionKind.ADDED, video_id=f"v{run_id}-{number}")
            for number in range(actions)
        ],
    ]


def store(sessions, *groups):
    with sessions.begin() as session:
        for group in groups:
            # The runs go in before their actions, so the foreign key finds the run.
            session.add_all(group[:1])
            session.flush()
            session.add_all(group[1:])


def run_ids(sessions):
    with sessions() as session:
        return session.scalars(select(Run.id).order_by(Run.id)).all()


def action_counts(sessions):
    with sessions() as session:
        rows = session.execute(
            select(RunAction.run_id, func.count()).group_by(RunAction.run_id).order_by(RunAction.run_id)
        )
        return dict(rows.all())


def added_video(video_id, age: timedelta, playlist_id="WL", now=NOW):
    """The record of an add of the video to the playlist ``age`` before ``now``."""
    return AddedVideo(playlist_id=playlist_id, video_id=video_id, rule_id=None, added_at=now - age)


def seen_video(video_id, source=CHANNEL, state=SeenVideoState.SEEN):
    return SeenVideo(channel_id=source, video_id=video_id, state=state)


def added_rows(sessions):
    with sessions() as session:
        return sorted((row.playlist_id, row.video_id) for row in session.scalars(select(AddedVideo)))


def test_the_default_retention_is_90_days(db_sessions):
    assert (DEFAULT_RETENTION_DAYS, get_retention_days(db_sessions)) == (90, 90)


def test_the_retention_is_stored_and_replaced(db_sessions):
    set_retention_days(db_sessions, 30)
    assert get_retention_days(db_sessions) == 30

    set_retention_days(db_sessions, 1)

    assert get_retention_days(db_sessions) == 1
    with db_sessions() as session:
        assert session.scalars(select(Setting.key)).all() == [RETENTION_KEY]


@pytest.mark.parametrize("wrong", [0, -1, True, False, 2.5, "30", None])
def test_a_retention_below_the_minimum_or_of_another_type_is_refused(db_sessions, wrong):
    with pytest.raises(ValueError, match="1 day or more"):
        set_retention_days(db_sessions, wrong)

    assert get_retention_days(db_sessions) == 90
    with db_sessions() as session:
        assert session.get(Setting, RETENTION_KEY) is None


@pytest.mark.parametrize("stored", [0, -5, True, 2.5, "30", [30], {"days": 30}])
def test_a_wrong_stored_retention_gives_the_default_and_a_warning(db_sessions, caplog, stored):
    store(db_sessions, [Setting(key=RETENTION_KEY, value=stored)])

    with caplog.at_level(logging.WARNING):
        assert get_retention_days(db_sessions) == 90

    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert RETENTION_KEY in caplog.text


def test_a_run_older_than_the_retention_goes_with_its_actions(db_sessions):
    store(
        db_sessions,
        make_run(1, timedelta(days=31)),
        make_run(2, timedelta(days=30, seconds=1)),
        make_run(3, timedelta(days=29)),
        make_run(4, timedelta(days=1)),
    )

    result = delete_old_runs(db_sessions, 30, now=NOW)

    assert result == CleanupResult(runs=2, actions=4)
    assert run_ids(db_sessions) == [3, 4]
    assert action_counts(db_sessions) == {3: 2, 4: 2}


def test_a_run_that_started_exactly_at_the_limit_stays(db_sessions):
    store(db_sessions, make_run(1, timedelta(days=30)), make_run(2, timedelta(days=30, microseconds=1)))

    delete_old_runs(db_sessions, 30, now=NOW)

    assert run_ids(db_sessions) == [1]


def test_a_run_in_progress_stays_however_old_it_is(db_sessions):
    store(
        db_sessions,
        make_run(1, timedelta(days=500), RunStatus.RUNNING),
        make_run(2, timedelta(days=500), RunStatus.FAILED),
        make_run(3, timedelta(days=500), RunStatus.SUCCEEDED),
    )

    result = delete_old_runs(db_sessions, 30, now=NOW)

    assert result == CleanupResult(runs=2, actions=4)
    assert run_ids(db_sessions) == [1]
    assert action_counts(db_sessions) == {1: 2}


def test_the_cleanup_of_an_empty_history_deletes_nothing(db_sessions):
    assert delete_old_runs(db_sessions, 30, now=NOW) == CleanupResult(runs=0, actions=0)


def test_the_actions_go_without_the_cascade_of_the_foreign_key(db_url):
    # This engine does not turn the foreign keys on, so only the explicit delete of the actions can remove them.
    engine = create_engine(db_url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    try:
        store(sessions, make_run(1, timedelta(days=100)), make_run(2, timedelta(days=1)))

        delete_old_runs(sessions, 30, now=NOW)

        assert run_ids(sessions) == [2]
        assert action_counts(sessions) == {2: 2}
    finally:
        engine.dispose()


def test_the_cleanup_uses_the_stored_retention_and_the_clock_of_now(db_sessions):
    now = datetime.now(UTC)
    store(db_sessions, make_run(1, timedelta(days=100), now=now), make_run(2, timedelta(days=80), now=now))

    assert clean_history(db_sessions) == CleanupResult(runs=1, actions=2)
    assert run_ids(db_sessions) == [2]

    set_retention_days(db_sessions, 7)

    assert clean_history(db_sessions) == CleanupResult(runs=1, actions=2)
    assert run_ids(db_sessions) == []


def test_the_cleanup_writes_a_log_line_only_when_it_deletes(db_sessions, caplog):
    with caplog.at_level(logging.INFO, logger="youtube_automanager.retention"):
        clean_history(db_sessions)
        assert caplog.records == []

        store(db_sessions, make_run(1, timedelta(days=1000)))
        clean_history(db_sessions)

    assert [record.getMessage() for record in caplog.records] == [
        "Deleted 1 run(s) and 2 action(s) that started more than 90 day(s) ago"
    ]


def test_an_added_video_stays_longer_than_an_apply_reaches_back():
    # yt-dlp dates the text "1 year ago" 364 days back, and YouTube shows that text until a video is 2 years old. An
    # apply that reaches MAX_DAYS_BACK days back thus reaches videos up to 2 years old.
    assert ADDED_VIDEO_KEEP_DAYS > MAX_DAYS_BACK
    assert ADDED_VIDEO_KEEP_DAYS > 2 * 365


def test_the_cleanup_deletes_only_an_old_added_video_that_no_source_keeps(db_sessions):
    store(
        db_sessions,
        [
            added_video("old", KEEP + timedelta(microseconds=1)),
            added_video("old", KEEP + timedelta(days=500), playlist_id="PL2"),
            added_video("at the limit", KEEP),
            added_video("young", KEEP - timedelta(days=1)),
            added_video("new", timedelta(0)),
            added_video("in a channel", KEEP + timedelta(days=500)),
            added_video("in the feed", KEEP + timedelta(days=500)),
            added_video("deferred", KEEP + timedelta(days=500)),
            seen_video("in a channel"),
            seen_video("in the feed", source="feed"),
            seen_video("deferred", state=SeenVideoState.DEFERRED),
        ],
    )

    assert delete_old_added_videos(db_sessions, now=NOW) == 2

    assert added_rows(db_sessions) == [
        ("WL", "at the limit"),
        ("WL", "deferred"),
        ("WL", "in a channel"),
        ("WL", "in the feed"),
        ("WL", "new"),
        ("WL", "young"),
    ]


def test_the_daily_cleanup_deletes_the_old_added_videos_with_a_log_line(db_sessions, caplog):
    now = datetime.now(UTC)
    store(
        db_sessions,
        [
            added_video("old", KEEP + timedelta(days=1), now=now),
            added_video("young", KEEP - timedelta(days=1), now=now),
        ],
    )

    with caplog.at_level(logging.INFO, logger="youtube_automanager.retention"):
        assert clean_history(db_sessions) == CleanupResult(runs=0, actions=0, added_videos=1)

    assert added_rows(db_sessions) == [("WL", "young")]
    assert [record.getMessage() for record in caplog.records] == [
        "Deleted 1 added video record(s) that are older than 732 day(s) and whose video no source keeps"
    ]


def wait_until(condition, what):
    deadline = time.monotonic() + WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, f"{what} did not happen within {WAIT_SECONDS} s"
        time.sleep(0.02)


def test_the_service_cleans_the_old_history_when_it_starts(settings, sessions, fake_starter, monkeypatch):
    monkeypatch.setattr(web_app, "clean_history", clean_history)
    now = datetime.now(UTC)
    store(
        sessions,
        make_run(1, timedelta(days=200), now=now),
        make_run(2, timedelta(days=89), now=now),
        make_run(3, timedelta(days=91), RunStatus.FAILED, now=now),
    )

    with TestClient(create_app(settings, start=fake_starter)):
        wait_until(lambda: run_ids(sessions) == [2], "The cleanup of the old runs")

    assert action_counts(sessions) == {2: 2}


def test_a_failing_cleanup_does_not_stop_the_service(settings, sessions, fake_starter, monkeypatch):
    called = []

    def failing_cleanup(sessions):
        called.append(sessions)
        raise RuntimeError("the cleanup failed")

    monkeypatch.setattr(web_app, "clean_history", failing_cleanup)

    with TestClient(create_app(settings, start=fake_starter)) as client:
        wait_until(lambda: called, "The start of the cleanup")
        answer = client.get("/api/status")

    assert (answer.status_code, answer.json()["scheduler_state"]) == (200, "running")


def test_the_cleanup_runs_in_one_transaction(db_url):
    engine = create_engine(db_url)
    statements = []
    event.listen(engine, "begin", lambda connection: statements.append("BEGIN"))
    event.listen(engine, "commit", lambda connection: statements.append("COMMIT"))
    sessions = sessionmaker(engine, expire_on_commit=False)
    try:
        store(sessions, make_run(1, timedelta(days=100)))
        statements.clear()

        delete_old_runs(sessions, 30, now=NOW)

        assert statements == ["BEGIN", "COMMIT"]
    finally:
        engine.dispose()
