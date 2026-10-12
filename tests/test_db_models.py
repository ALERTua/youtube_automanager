from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from youtube_automanager.db import make_engine, make_session_factory
from youtube_automanager.db.models import (
    DEFAULT_CRON_EXPRESSION,
    Account,
    AddedVideo,
    Channel,
    NotificationTarget,
    Playlist,
    Rule,
    RuleChannel,
    Run,
    RunAction,
    RunActionKind,
    RunStatus,
    RunTrigger,
    Schedule,
    ScheduleKind,
    SeenVideo,
    SeenVideoState,
    SessionState,
    Setting,
)

MOMENT = datetime(2026, 10, 11, 12, 30, 15, 123456, tzinfo=UTC)
KYIV_SUMMER = timezone(timedelta(hours=3))


@pytest.fixture
def engine(tmp_path, new_database):
    url = new_database(tmp_path / "test.sqlite")
    engine = make_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(engine):
    return make_session_factory(engine)


def store(sessions, *rows):
    with sessions.begin() as session:
        session.add_all(rows)


def read_all(sessions, model):
    with sessions() as session:
        return session.scalars(select(model)).all()


def add_rule(sessions, rule_id=1, playlist_id="WL"):
    store(sessions, Playlist(playlist_id=playlist_id, title="Watch later", is_own=True))
    store(
        sessions,
        Rule(
            id=rule_id,
            name="Music",
            enabled=True,
            position=0,
            condition={"in": ["live", {"var": "title"}]},
            target_playlist_id=playlist_id,
            no_shorts=True,
        ),
    )


def test_postgresql_url_without_a_driver_gets_psycopg_3():
    engine = make_engine("postgresql://user:secret@localhost/youtube")

    assert engine.dialect.driver == "psycopg"
    engine.dispose()


def test_sqlite_url_gets_the_built_in_sqlite3(engine):
    assert engine.dialect.driver == "pysqlite"


def test_sqlite_connections_enforce_foreign_keys(engine):
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_account(sessions):
    store(sessions, Account(cookies=b"\x00cookie bytes", last_checked_at=MOMENT))

    (account,) = read_all(sessions, Account)

    assert account.id == 1
    assert account.channel_id is None
    assert account.cookies == b"\x00cookie bytes"
    assert account.session_state is SessionState.UNKNOWN
    assert account.last_checked_at == MOMENT
    assert account.earliest_cookie_expiry is None


def test_channel(sessions):
    store(
        sessions,
        Channel(
            channel_id="UC123",
            title="A channel",
            handle="@channel",
            thumbnail_url="https://example.com/a.jpg",
            is_subscribed=True,
            mark_date=date(2026, 10, 1),
        ),
    )

    (channel,) = read_all(sessions, Channel)

    assert (channel.channel_id, channel.title, channel.handle) == ("UC123", "A channel", "@channel")
    assert channel.is_subscribed is True
    assert channel.mark_date == date(2026, 10, 1)
    assert channel.last_checked_at is None


def test_playlist(sessions):
    store(sessions, Playlist(playlist_id="WL", title="Watch later", is_own=True, last_synced_at=MOMENT))

    (playlist,) = read_all(sessions, Playlist)

    assert (playlist.playlist_id, playlist.title, playlist.is_own) == ("WL", "Watch later", True)
    assert playlist.last_synced_at == MOMENT


def test_rule(sessions):
    before = datetime.now(UTC)
    add_rule(sessions)

    (rule,) = read_all(sessions, Rule)

    assert (rule.name, rule.enabled, rule.position, rule.no_shorts) == ("Music", True, 0, True)
    assert rule.condition == {"in": ["live", {"var": "title"}]}
    assert rule.target_playlist_id == "WL"
    assert before <= rule.created_at <= datetime.now(UTC)
    assert rule.updated_at.tzinfo is UTC


def test_rule_update_moves_updated_at(sessions):
    add_rule(sessions)
    with sessions.begin() as session:
        rule = session.get(Rule, 1)
        rule.updated_at = rule.created_at - timedelta(days=1)
    with sessions.begin() as session:
        session.get(Rule, 1).name = "Renamed"

    (rule,) = read_all(sessions, Rule)

    assert rule.updated_at >= rule.created_at


def test_rule_channel(sessions):
    add_rule(sessions)
    store(sessions, Channel(channel_id="UC123", title="A channel", is_subscribed=True))
    store(sessions, RuleChannel(rule_id=1, channel_id="UC123"))

    (link,) = read_all(sessions, RuleChannel)

    assert (link.rule_id, link.channel_id) == (1, "UC123")


def test_seen_video(sessions):
    store(
        sessions,
        SeenVideo(channel_id="UC123", video_id="vid1", state=SeenVideoState.DEFERRED, upload_date=date(2026, 10, 10)),
    )

    (video,) = read_all(sessions, SeenVideo)

    assert (video.channel_id, video.video_id, video.state) == ("UC123", "vid1", SeenVideoState.DEFERRED)
    assert video.upload_date == date(2026, 10, 10)
    assert video.first_seen_at.tzinfo is UTC
    assert video.last_checked_at.tzinfo is UTC


def test_added_video(sessions):
    add_rule(sessions)
    store(sessions, AddedVideo(playlist_id="WL", video_id="vid1", rule_id=1, added_at=MOMENT))

    (added,) = read_all(sessions, AddedVideo)

    assert (added.playlist_id, added.video_id, added.rule_id, added.added_at) == ("WL", "vid1", 1, MOMENT)


def test_schedule(sessions):
    store(sessions, Schedule(enabled=True, kind=ScheduleKind.CRON, timezone="Europe/Kyiv"))

    (schedule,) = read_all(sessions, Schedule)

    assert schedule.id == 1
    assert schedule.kind is ScheduleKind.CRON
    assert schedule.cron_expression == DEFAULT_CRON_EXPRESSION == "0 18 * * *"
    assert schedule.interval_s is None
    assert schedule.timezone == "Europe/Kyiv"


def test_schedule_refuses_a_second_row(sessions):
    store(sessions, Schedule(enabled=True, kind=ScheduleKind.CRON, timezone="UTC"))

    with pytest.raises(IntegrityError, match="ck_schedule_single_row"):
        store(sessions, Schedule(id=2, enabled=True, kind=ScheduleKind.INTERVAL, interval_s=600, timezone="UTC"))


def test_run(sessions):
    store(sessions, Run(status=RunStatus.FAILED, trigger=RunTrigger.MANUAL, finished_at=MOMENT, error_text="boom"))

    (run,) = read_all(sessions, Run)

    assert (run.status, run.trigger, run.error_text) == (RunStatus.FAILED, RunTrigger.MANUAL, "boom")
    assert run.started_at.tzinfo is UTC
    assert run.finished_at == MOMENT
    assert (run.added_count, run.seen_count) == (0, 0)


def test_run_action(sessions):
    store(sessions, Run(id=7, status=RunStatus.RUNNING, trigger=RunTrigger.SCHEDULE))
    store(
        sessions,
        RunAction(
            run_id=7,
            action=RunActionKind.ADDED,
            video_id="vid1",
            channel_id="UC123",
            rule_id=99,
            playlist_id="WL",
            detail="matched",
        ),
    )

    (action,) = read_all(sessions, RunAction)

    assert (action.run_id, action.action, action.rule_id, action.detail) == (7, RunActionKind.ADDED, 99, "matched")
    assert action.at.tzinfo is UTC


def test_notification_target(sessions):
    store(
        sessions,
        NotificationTarget(name="phone", url="ntfy://topic", min_level=20, events=["run_failed"], enabled=True),
    )

    (target,) = read_all(sessions, NotificationTarget)

    assert (target.name, target.url, target.min_level, target.enabled) == ("phone", "ntfy://topic", 20, True)
    assert target.events == ["run_failed"]


def test_notification_target_name_is_unique(sessions):
    store(sessions, NotificationTarget(name="phone", url="a://", min_level=0, events=[], enabled=True))

    with pytest.raises(IntegrityError):
        store(sessions, NotificationTarget(name="phone", url="b://", min_level=0, events=[], enabled=True))


def test_setting(sessions):
    store(sessions, Setting(key="language", value={"code": "uk", "fallback": ["en"]}))

    (setting,) = read_all(sessions, Setting)

    assert (setting.key, setting.value) == ("language", {"code": "uk", "fallback": ["en"]})


def test_aware_datetime_comes_back_as_the_same_moment_in_utc(sessions, engine):
    store(
        sessions,
        Playlist(playlist_id="WL", title="Watch later", is_own=True, last_synced_at=MOMENT.astimezone(KYIV_SUMMER)),
    )

    (playlist,) = read_all(sessions, Playlist)
    with engine.connect() as connection:
        stored = connection.execute(text("SELECT last_synced_at FROM playlist")).scalar_one()

    assert playlist.last_synced_at == MOMENT
    assert playlist.last_synced_at.tzinfo is UTC
    assert stored == "2026-10-11 12:30:15.123456"


def test_naive_datetime_is_refused(sessions):
    with pytest.raises(StatementError, match="naive datetime") as caught:
        store(
            sessions, Playlist(playlist_id="WL", title="Watch later", is_own=True, last_synced_at=datetime(2026, 1, 1))
        )

    assert isinstance(caught.value.orig, ValueError)
    assert read_all(sessions, Playlist) == []


def test_enumerations_are_stored_as_their_string_values(sessions, engine):
    store(sessions, Account())

    with engine.connect() as connection:
        assert connection.execute(text("SELECT session_state FROM account")).scalar_one() == "unknown"


def test_check_constraint_refuses_an_unknown_enumeration_value(engine):
    with pytest.raises(IntegrityError, match="ck_account_session_state"), engine.begin() as connection:
        connection.execute(text("INSERT INTO account (session_state) VALUES ('banned')"))


def test_deleting_a_rule_deletes_its_channel_links(sessions):
    add_rule(sessions)
    store(sessions, Channel(channel_id="UC123", title="A channel", is_subscribed=True))
    store(sessions, RuleChannel(rule_id=1, channel_id="UC123"))

    with sessions.begin() as session:
        session.execute(delete(Rule).where(Rule.id == 1))

    assert read_all(sessions, RuleChannel) == []
    assert [channel.channel_id for channel in read_all(sessions, Channel)] == ["UC123"]


def test_deleting_a_run_deletes_its_actions(sessions):
    store(sessions, Run(id=7, status=RunStatus.SUCCEEDED, trigger=RunTrigger.SCHEDULE))
    store(sessions, RunAction(run_id=7, action=RunActionKind.SKIPPED), RunAction(run_id=7, action=RunActionKind.ERROR))

    with sessions.begin() as session:
        session.delete(session.get(Run, 7))

    assert read_all(sessions, RunAction) == []


def test_deleting_a_rule_keeps_its_added_videos_without_the_rule(sessions):
    add_rule(sessions)
    store(sessions, AddedVideo(playlist_id="WL", video_id="vid1", rule_id=1))

    with sessions.begin() as session:
        session.execute(delete(Rule).where(Rule.id == 1))

    (added,) = read_all(sessions, AddedVideo)
    assert (added.playlist_id, added.video_id, added.rule_id) == ("WL", "vid1", None)


def test_foreign_key_refuses_a_link_to_a_missing_rule(sessions):
    store(sessions, Channel(channel_id="UC123", title="A channel", is_subscribed=True))

    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        store(sessions, RuleChannel(rule_id=42, channel_id="UC123"))
