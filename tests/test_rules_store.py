import subprocess
import sys
import threading
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from ui_support import store

from youtube_automanager.db.models import Channel, Playlist, Rule, RuleChannel, Schedule, ScheduleKind
from youtube_automanager.engine.rules import MATCH_ALL, RuleError
from youtube_automanager.rules_store import (
    RuleDraft,
    delete_rule,
    read_stored,
    save_rule,
    validate_draft,
    validate_name,
    validate_playlist,
)
from youtube_automanager.runner import SOURCE_ROOT, RunAlreadyActive, SyncRunner

A = "UCaaaaaaaaaaaaaaaaaaaaaa"
B = "UCbbbbbbbbbbbbbbbbbbbbbb"
LEVELS = {"regex": [{"var": "title"}, "Levels of", "i"]}
CHANGED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def draft(
    *,
    name: str = "Levels",
    enabled: bool = True,
    channel_ids: tuple[str, ...] = (A,),
    playlist_id: str = "WL",
    no_shorts: bool = False,
    condition: Any = LEVELS,
    catch_up_since: date | None = None,
) -> RuleDraft:
    return RuleDraft(name, enabled, channel_ids, playlist_id, no_shorts, condition, catch_up_since)


@pytest.fixture
def account(db_sessions):
    """The playlists and the channels of the account copy. The titles come in the opposite order to the ids."""
    store(
        db_sessions,
        Playlist(playlist_id="WL", title="Watch Later", is_own=True),
        Playlist(playlist_id="PLhome", title="Home", is_own=True),
        Channel(channel_id=A, title="Zeta", handle="@zeta", is_subscribed=True),
        Channel(channel_id=B, title="alpha", handle=None, is_subscribed=False),
    )
    return db_sessions


def rule_row(rule_id: int, name: str, position: int, **columns) -> Rule:
    return Rule(
        id=rule_id,
        name=name,
        enabled=True,
        position=position,
        condition=MATCH_ALL,
        target_playlist_id="WL",
        no_shorts=False,
        created_at=CHANGED_AT,
        updated_at=CHANGED_AT,
        **columns,
    )


def channels_of(sessions, rule_id: int) -> list[str]:
    with sessions() as session:
        return sorted(session.scalars(select(RuleChannel.channel_id).where(RuleChannel.rule_id == rule_id)))


# The checks before a save.


def test_the_name_is_stripped_and_an_empty_name_is_refused():
    assert validate_name("  Levels ") == "Levels"
    for empty in (None, "", "   "):
        with pytest.raises(ValueError, match=r"^Enter a name\.$"):
            validate_name(empty)


def test_an_empty_target_playlist_is_refused():
    assert validate_playlist("WL") == "WL"
    for empty in (None, ""):
        with pytest.raises(ValueError, match=r"^Choose a target playlist\.$"):
            validate_playlist(empty)


def test_a_valid_draft_passes_the_check():
    validate_draft(draft())
    validate_draft(draft(channel_ids=(), condition=MATCH_ALL))


def test_the_check_of_a_draft_goes_in_the_order_name_playlist_condition():
    with pytest.raises(ValueError, match=r"^Enter a name\.$"):
        validate_draft(draft(name=" ", playlist_id="", condition=[]))
    with pytest.raises(ValueError, match=r"^Choose a target playlist\.$"):
        validate_draft(draft(playlist_id="", condition=[]))
    with pytest.raises(RuleError, match="The root of a rule must be an object, not list"):
        validate_draft(draft(condition=[]))


def test_the_check_of_a_draft_refuses_a_condition_with_a_field_that_is_not_a_video_field():
    with pytest.raises(ValueError, match="The field 'nothing' is not a video field"):
        validate_draft(draft(condition={"==": [{"var": "nothing"}, 1]}))


def test_the_module_imports_neither_nicegui_nor_the_web_package():
    """The API and a child process import the store, so it must not pull in the page code."""
    code = (
        "import sys\n"
        "import youtube_automanager.rules_store\n"
        "web = ('nicegui', 'youtube_automanager.web')\n"
        "loaded = sorted(name for name in sys.modules if name.startswith(web))\n"
        "assert not loaded, loaded\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=SOURCE_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr


# The read.


def test_read_stored_gives_the_rules_in_their_order_with_their_channels_sorted_by_title(account):
    store(
        account,
        rule_row(1, "Second", 2, catch_up_since=date(2026, 10, 5)),
        rule_row(2, "First", 1),
        *[RuleChannel(rule_id=1, channel_id=channel_id) for channel_id in (A, B)],
    )

    stored = read_stored(account, "Europe/Kyiv")

    assert [(rule.id, rule.name, rule.channel_ids, rule.catch_up_since) for rule in stored.rules] == [
        (2, "First", (), None),
        (1, "Second", (B, A), date(2026, 10, 5)),
    ]
    assert stored.rule(1) is stored.rules[1]
    assert stored.rule(99) is None
    assert list(stored.channels) == [B, A]
    assert stored.channel_titles == {B: "alpha", A: "Zeta"}
    assert list(stored.playlists) == ["PLhome", "WL"]
    assert (stored.playlist_title("PLhome"), stored.playlist_title("PLgone")) == ("Home", "PLgone")


def test_read_stored_takes_the_time_zone_of_the_schedule_or_the_default(account):
    assert read_stored(account, "Europe/Kyiv").timezone == "Europe/Kyiv"

    store(
        account,
        Schedule(id=1, enabled=True, kind=ScheduleKind.CRON, cron_expression="0 18 * * *", timezone="Asia/Tokyo"),
    )

    assert read_stored(account, "Europe/Kyiv").timezone == "Asia/Tokyo"


# The save.


def test_a_new_rule_goes_to_the_end_of_the_order_and_gets_its_channels(account):
    store(account, rule_row(7, "Old", 4))

    rule_id = save_rule(account, None, draft(catch_up_since=date(2026, 10, 3)))

    assert rule_id is not None
    with account() as session:
        saved = session.get(Rule, rule_id)
        assert (saved.name, saved.position, saved.target_playlist_id, saved.condition, saved.catch_up_since) == (
            "Levels",
            5,
            "WL",
            LEVELS,
            date(2026, 10, 3),
        )
    assert channels_of(account, rule_id) == [A]


def test_the_first_rule_gets_the_position_1(account):
    rule_id = save_rule(account, None, draft())

    with account() as session:
        assert session.get(Rule, rule_id).position == 1


def test_a_change_keeps_the_position_and_replaces_the_channels_and_the_time_of_change(account):
    store(account, rule_row(1, "Old", 3), RuleChannel(rule_id=1, channel_id=A))

    rule_id = save_rule(
        account, 1, draft(name="New", enabled=False, channel_ids=(B,), no_shorts=True, playlist_id="PLhome")
    )

    assert rule_id == 1
    with account() as session:
        saved = session.get(Rule, 1)
        assert (saved.name, saved.enabled, saved.position, saved.no_shorts, saved.target_playlist_id) == (
            "New",
            False,
            3,
            True,
            "PLhome",
        )
        assert saved.updated_at.replace(tzinfo=UTC) > CHANGED_AT
    assert channels_of(account, 1) == [B]


def test_a_save_that_changes_only_the_channels_counts_as_a_change(account):
    store(account, rule_row(1, "Same", 1), RuleChannel(rule_id=1, channel_id=A))

    save_rule(account, 1, draft(name="Same", condition=MATCH_ALL, channel_ids=(A, B)))

    with account() as session:
        assert session.get(Rule, 1).updated_at.replace(tzinfo=UTC) > CHANGED_AT
    assert channels_of(account, 1) == [A, B]


def test_a_save_of_a_rule_that_does_not_exist_any_more_writes_nothing(account):
    assert save_rule(account, 99, draft()) is None

    with account() as session:
        assert session.scalars(select(Rule)).all() == []
        assert session.scalars(select(RuleChannel)).all() == []


# The delete.


@pytest.fixture
def runner(db_sessions, db_url, tmp_path, fake_starter):
    return SyncRunner(
        db_sessions,
        ytdlp_dir=tmp_path / "yt-dlp",
        database_url=db_url,
        child_timeout_s=10,
        start=fake_starter,
        lock=threading.Lock(),
    )


def test_a_delete_removes_the_rule_and_its_channel_links_and_keeps_the_others(account, runner):
    store(
        account,
        rule_row(1, "Doomed", 1),
        rule_row(2, "Kept", 2),
        RuleChannel(rule_id=1, channel_id=A),
        RuleChannel(rule_id=2, channel_id=A),
    )

    delete_rule(account, runner, 1)

    with account() as session:
        assert [rule.name for rule in session.scalars(select(Rule))] == ["Kept"]
    assert (channels_of(account, 1), channels_of(account, 2)) == ([], [A])
    # The delete lets go of the lock.
    with runner.lock_held(timeout=0):
        pass


def test_a_delete_of_a_rule_that_does_not_exist_changes_nothing(account, runner):
    store(account, rule_row(1, "Kept", 1))

    delete_rule(account, runner, 99)

    with account() as session:
        assert [rule.name for rule in session.scalars(select(Rule))] == ["Kept"]


def test_a_delete_while_the_sync_lock_is_taken_refuses_at_once_and_keeps_the_rule(account, runner):
    store(account, rule_row(1, "Doomed", 1), RuleChannel(rule_id=1, channel_id=A))

    with runner.lock_held(), pytest.raises(RunAlreadyActive):
        delete_rule(account, runner, 1)

    with account() as session:
        assert [rule.name for rule in session.scalars(select(Rule))] == ["Doomed"]
    assert channels_of(account, 1) == [A]
