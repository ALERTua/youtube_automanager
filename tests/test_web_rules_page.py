import asyncio
import copy
import json
import re
import threading
import time
from datetime import UTC, datetime

import pytest
from nicegui import ui
from nicegui.elements.mixins.label_element import LabelElement
from nicegui.elements.mixins.text_element import TextElement
from sqlalchemy import select
from test_live_rules import (
    EPICURIOUS,
    EPICURIOUS_LEVELS_RULE,
    HOME_CHANNELS,
    HOME_RULE,
    WATCH_LATER_CHANNELS,
    WATCH_LATER_RULE,
)
from test_sync import entry, feed_video
from ui_support import element_of, elements_of, eventually, running_service, set_value, store, text_of

from youtube_automanager import i18n
from youtube_automanager.db.models import (
    AddedVideo,
    Channel,
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
)
from youtube_automanager.engine.rules import MATCH_ALL
from youtube_automanager.runner import sync_lock_held
from youtube_automanager.web.ui import rules_page
from youtube_automanager.web.ui.dates import today_in
from youtube_automanager.web.ui.rule_form import LIVE_STATUSES

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]

A = "UCaaaaaaaaaaaaaaaaaaaaaa"
B = "UCbbbbbbbbbbbbbbbbbbbbbb"
C = "UCcccccccccccccccccccccc"
TITLE = {"var": "title"}
DURATION = {"var": "duration_s"}
LONG = {">=": [DURATION, 120]}
LEVELS = {"regex": [TITLE, "Levels of", "i"]}
# 12:00 UTC is 15:00 in Kyiv on this day.
CHANGED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
# How long a test waits for a click that must change nothing. A click that does change something shows it within ms.
SETTLE_SECONDS = 0.3


@pytest.fixture
def db_sessions(sessions):
    """The sync harness of ``conftest`` works on the database of the page."""
    return sessions


def channel(channel_id: str, title: str, handle: str | None = None, *, subscribed: bool = True) -> Channel:
    return Channel(channel_id=channel_id, title=title, handle=handle, is_subscribed=subscribed)


def playlist(playlist_id: str, title: str, *, own: bool = True) -> Playlist:
    return Playlist(playlist_id=playlist_id, title=title, is_own=own)


def rule(
    rule_id: int,
    name: str,
    condition=MATCH_ALL,
    *,
    target: str = "WL",
    enabled: bool = True,
    no_shorts: bool = False,
    position: int | None = None,
) -> Rule:
    return Rule(
        id=rule_id,
        name=name,
        enabled=enabled,
        position=rule_id if position is None else position,
        condition=copy.deepcopy(condition),
        target_playlist_id=target,
        no_shorts=no_shorts,
        created_at=CHANGED_AT,
        updated_at=CHANGED_AT,
    )


def links(rule_id: int, *channel_ids: str) -> list[RuleChannel]:
    return [RuleChannel(rule_id=rule_id, channel_id=channel_id) for channel_id in channel_ids]


@pytest.fixture
def account_copy(sessions):
    """The stored copy of the account: three channels, one of them not subscribed, and three playlists."""
    store(
        sessions,
        channel(A, "Alpha", "@alpha"),
        channel(B, "Beta"),
        channel(C, "Gamma", "@gamma", subscribed=False),
        playlist("WL", "Watch later"),
        playlist("PLhome", "Home"),
        playlist("PLsaved", "Saved from another channel", own=False),
    )


def stored_rules(sessions) -> list[Rule]:
    with sessions() as session:
        return session.scalars(select(Rule).order_by(Rule.id)).all()


def stored_rule(sessions, rule_id: int) -> Rule | None:
    with sessions() as session:
        return session.get(Rule, rule_id)


def rule_channels(sessions, rule_id: int) -> list[str]:
    with sessions() as session:
        return sorted(session.scalars(select(RuleChannel.channel_id).where(RuleChannel.rule_id == rule_id)))


def table_rows(user) -> list[dict]:
    tables = elements_of(user, "rules-table")
    return tables[0].rows if tables else []


def names(user) -> list[str]:
    return [row["name"] for row in table_rows(user)]


async def names_are(user, expected: list[str]) -> None:
    await eventually(lambda: names(user) == expected, f"The table with {expected}")


async def open_editor(user, rule_id: int | None = None) -> None:
    if rule_id is None:
        user.find(marker="new-rule").click()
    else:
        user.find(marker="rules-table").trigger("rowClick", args=rule_id)
    await eventually(lambda: elements_of(user, "editor"), "The editor")


async def save(user, message: str = "The rule is saved.") -> None:
    user.find(marker="editor-save").click()
    await user.should_see(message)
    await eventually(lambda: not elements_of(user, "editor"), "The close of the editor")


async def error_is(user, text: str) -> None:
    await eventually(lambda: text_of(user, "editor-error") == text, f"The error {text!r}")


def options(user, marker: str) -> dict:
    return element_of(user, marker).options


async def choose(user, marker: str, value) -> None:
    """Set the field or the comparison of a condition, and wait until the condition editor shows the new inputs."""
    old = element_of(user, marker)
    if old.value == value:
        return
    set_value(user, marker, value)
    await eventually(lambda: element_of(user, marker) is not old, f"The new inputs after {marker}")


async def test_rules_page_is_in_the_drawer(user):
    await user.open("/")

    user.find(marker="nav:/rules").click()

    await user.should_see(marker="new-rule")


async def test_a_new_service_has_no_rules(user):
    await user.open("/rules")

    assert text_of(user, "no-rules") == "No rule yet. The button New rule creates one."
    await user.should_not_see(marker="rules-table")
    await user.should_not_see(marker="editor")


@pytest.fixture
def three_rules(sessions, account_copy):
    store(
        sessions,
        rule(1, "Levels of Epicurious", {"and": [LEVELS, LONG]}, target="PLhome", no_shorts=True, position=2),
        rule(2, "Everything from the feed", enabled=False, position=1),
        rule(3, "Hand-written", LONG, target="PLsaved", position=3),
    )
    store(sessions, *links(1, B, A), *links(3, C))


async def test_the_table_shows_each_rule_in_the_order_of_the_evaluation(user, three_rules):
    await user.open("/rules")

    table = element_of(user, "rules-table")
    assert [column["label"] for column in table.columns] == [
        "Name",
        "Enabled",
        "Channels",
        "Playlist",
        "No Shorts",
        "Condition",
        "Changed",
    ]
    assert table.rows == [
        {
            "id": 2,
            "name": "Everything from the feed",
            "enabled": "No",
            "channels": "Subscriptions feed",
            "playlist": "Watch later",
            "no_shorts": "No",
            "condition": "Each video",
            # Kyiv is at UTC+3 on this day.
            "updated": "2026-10-01 15:00 EEST",
        },
        {
            "id": 1,
            "name": "Levels of Epicurious",
            "enabled": "Yes",
            "channels": "Alpha, Beta",
            "playlist": "Home",
            "no_shorts": "Yes",
            "condition": 'Title matches the pattern "Levels of" and Duration is at least 2:00',
            "updated": "2026-10-01 15:00 EEST",
        },
        {
            "id": 3,
            "name": "Hand-written",
            "enabled": "Yes",
            "channels": "Gamma",
            "playlist": "Saved from another channel",
            "no_shorts": "No",
            "condition": "Custom JSON condition",
            "updated": "2026-10-01 15:00 EEST",
        },
    ]


async def test_the_time_of_the_change_follows_the_time_zone_of_the_schedule(
    nicegui_globals, settings, sessions, fake_starter
):
    store(
        sessions,
        Schedule(enabled=True, kind=ScheduleKind.CRON, timezone="America/New_York"),
        playlist("WL", "Watch later"),
    )
    store(sessions, rule(1, "A rule"))

    async with running_service(settings, fake_starter) as user:
        await user.open("/rules")

        assert table_rows(user)[0]["updated"] == "2026-10-01 08:00 EDT"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("levels", ["Levels of Epicurious"]),
        ("ALPHA", ["Levels of Epicurious"]),
        ("@gam", ["Hand-written"]),
        ("gamma", ["Hand-written"]),
        ("  feed ", ["Everything from the feed"]),
        ("e", ["Everything from the feed", "Levels of Epicurious", "Hand-written"]),
    ],
)
async def test_the_text_filter_finds_the_name_the_channel_title_and_the_handle(user, three_rules, text, expected):
    await user.open("/rules")

    user.find(marker="filter-text").type(text)

    await names_are(user, expected)


async def test_the_text_filter_does_not_read_the_texts_that_the_page_makes(user, three_rules):
    await user.open("/rules")

    set_value(user, "filter-text", "subscriptions")

    await eventually(lambda: text_of(user, "no-match") == "No rule matches the filters.", "The message")
    await user.should_not_see(marker="rules-table")
    user.find(marker="filter-text").clear()
    await names_are(user, ["Everything from the feed", "Levels of Epicurious", "Hand-written"])


async def test_the_playlist_filter_offers_the_target_of_each_rule(user, three_rules):
    await user.open("/rules")

    assert list(options(user, "filter-playlist").items()) == [
        ("any", "All playlists"),
        ("PLhome", "Home"),
        ("PLsaved", "Saved from another channel"),
        ("WL", "Watch later"),
    ]
    user.find(marker="filter-playlist").click()
    user.find("Home").click()
    await names_are(user, ["Levels of Epicurious"])
    set_value(user, "filter-playlist", "WL")
    await names_are(user, ["Everything from the feed"])
    set_value(user, "filter-playlist", "any")
    await names_are(user, ["Everything from the feed", "Levels of Epicurious", "Hand-written"])


async def test_the_state_filter(user, three_rules):
    await user.open("/rules")

    set_value(user, "filter-state", "enabled")
    await names_are(user, ["Levels of Epicurious", "Hand-written"])
    set_value(user, "filter-state", "disabled")
    await names_are(user, ["Everything from the feed"])
    set_value(user, "filter-state", "any")
    await names_are(user, ["Everything from the feed", "Levels of Epicurious", "Hand-written"])


async def test_the_filters_work_together(user, three_rules):
    await user.open("/rules")

    set_value(user, "filter-state", "enabled")
    set_value(user, "filter-text", "a")
    set_value(user, "filter-playlist", "PLsaved")
    await names_are(user, ["Hand-written"])

    set_value(user, "filter-state", "disabled")
    await eventually(lambda: elements_of(user, "no-match"), "The message that no rule matches")


async def test_the_playlist_filter_goes_back_to_all_when_its_playlist_has_no_rule_any_more(user, three_rules):
    await user.open("/rules")
    set_value(user, "filter-playlist", "PLsaved")
    await names_are(user, ["Hand-written"])

    await open_editor(user, 3)
    set_value(user, "rule-playlist", "WL")
    await save(user)

    # The page reads the rules again after the close of the editor.
    await eventually(lambda: element_of(user, "filter-playlist").value == "any", "The filter of all playlists")
    assert "PLsaved" not in options(user, "filter-playlist")
    await names_are(user, ["Everything from the feed", "Levels of Epicurious", "Hand-written"])


async def test_a_rule_made_on_the_page_adds_the_matching_new_videos_in_the_next_runs(
    user, sessions, account_copy, sync
):
    await user.open("/rules")
    await open_editor(user)
    assert text_of(user, "editor-title") == "New rule"

    user.find(marker="rule-name").type("Levels of Alpha")
    set_value(user, "rule-channels", [A])
    user.find(marker="rule-playlist").click()
    user.find("Home").click()
    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "value-0"), "The first condition")
    user.find(marker="value-0").type("levels of")
    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "field-1"), "The second condition")
    set_value(user, "field-1", "duration_s")
    await eventually(lambda: elements_of(user, "minutes-1"), "The input of the minutes")
    set_value(user, "minutes-1", 2.0)
    await save(user)

    (stored,) = stored_rules(sessions)
    assert (stored.name, stored.enabled, stored.position, stored.target_playlist_id, stored.no_shorts) == (
        "Levels of Alpha",
        True,
        1,
        "PLhome",
        False,
    )
    assert stored.condition == {"and": [{"regex": [TITLE, "levels of", "i"]}, {">=": [DURATION, 120]}]}
    assert rule_channels(sessions, stored.id) == [A]
    await names_are(user, ["Levels of Alpha"])

    # The first run of a new source only records the videos that are there.
    old = [entry("soup", A, title="4 Levels of Soup")]
    sync.sources.uploads[A] = old
    sync.run()
    assert sync.account.added == []
    sync.sources.uploads[A] = [
        entry("pizza", A, title="4 Levels of Pizza"),
        entry("tea", A, title="3 Levels of Tea", duration_s=60),
        entry("pasta", A, title="Pasta"),
        *old,
    ]
    sync.run()

    assert sync.account.added == [("PLhome", "pizza")]
    assert sync.added_rows() == {("PLhome", "pizza"): stored.id}


async def test_a_rule_without_channels_and_conditions_takes_each_new_video_of_the_feed(
    user, sessions, account_copy, sync
):
    await user.open("/rules")
    await open_editor(user)
    await user.should_see(marker="rule-feed-hint")
    assert text_of(user, "no-conditions") == "Without conditions, the rule takes each video of its channels."

    set_value(user, "rule-name", "All of the feed")
    set_value(user, "rule-playlist", "WL")
    await save(user)

    (stored,) = stored_rules(sessions)
    assert stored.condition == MATCH_ALL
    assert rule_channels(sessions, stored.id) == []
    await names_are(user, ["All of the feed"])
    row = table_rows(user)[0]
    assert (row["channels"], row["condition"]) == ("Subscriptions feed", "Each video")

    sync.account.feed = [feed_video("first", A)]
    sync.run()
    assert sync.account.added == []
    sync.account.feed = [feed_video("second", B), feed_video("first", A)]
    sync.run()

    assert sync.account.added == [("WL", "second")]


@pytest.mark.parametrize(
    ("tree", "channels", "target"),
    [
        pytest.param(HOME_RULE, HOME_CHANNELS, "PLhome", id="home"),
        pytest.param(EPICURIOUS_LEVELS_RULE, [EPICURIOUS], "PLhome", id="epicurious levels"),
        pytest.param(WATCH_LATER_RULE, WATCH_LATER_CHANNELS, "WL", id="watch later"),
    ],
)
async def test_each_live_rule_opens_in_the_visual_editor_and_saves_back_an_equal_tree(
    user, sessions, tree, channels, target
):
    store(
        sessions,
        *[channel(channel_id, f"Channel {index}") for index, channel_id in enumerate(channels)],
        playlist("WL", "Watch later"),
        playlist("PLhome", "@Home"),
    )
    store(sessions, rule(1, "Live rule", tree, target=target))
    store(sessions, *links(1, *channels))
    await user.open("/rules")

    await open_editor(user, 1)

    await user.should_not_see(marker="rule-json")
    assert element_of(user, "group-kind").value == "all"
    assert element_of(user, "operator-0").value == "in"
    assert element_of(user, "value-0").value == channels
    assert set(element_of(user, "rule-channels").value) == set(channels)
    await save(user)
    assert stored_rule(sessions, 1).condition == tree
    assert rule_channels(sessions, 1) == sorted(channels)


async def test_a_condition_that_the_visual_editor_cannot_show_opens_as_json(user, sessions, account_copy):
    store(sessions, rule(1, "Hand-written", LONG))
    await user.open("/rules")
    await open_editor(user, 1)

    await user.should_not_see(marker="group-kind")
    assert json.loads(element_of(user, "rule-json").value) == LONG
    set_value(user, "rule-json", "{")
    user.find(marker="editor-save").click()
    await error_is(
        user,
        "The condition is not valid JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
    )
    set_value(user, "rule-json", '{"var": "channel_title"}')
    user.find(marker="editor-save").click()
    await error_is(user, "The field 'channel_title' is not a video field")
    assert stored_rule(sessions, 1).condition == LONG

    shown = {"and": [{"<": [DURATION, 60]}]}
    set_value(user, "rule-json", json.dumps(shown))
    await save(user)

    assert stored_rule(sessions, 1).condition == shown
    await open_editor(user, 1)
    await user.should_not_see(marker="rule-json")
    assert element_of(user, "operator-0").value == "<"
    assert (element_of(user, "minutes-0").value, element_of(user, "seconds-0").value) == (1, 0)


async def test_an_empty_group_that_matches_no_video_opens_as_json(user, sessions, account_copy):
    store(sessions, rule(1, "Matches nothing", {"and": []}))
    await user.open("/rules")

    await open_editor(user, 1)

    assert json.loads(element_of(user, "rule-json").value) == {"and": []}
    await save(user)
    assert stored_rule(sessions, 1).condition == {"and": []}


async def test_a_rule_that_takes_each_video_opens_as_an_empty_group(user, sessions, account_copy):
    store(sessions, rule(1, "Everything"))
    await user.open("/rules")

    await open_editor(user, 1)

    await user.should_not_see(marker="rule-json")
    await user.should_see(marker="no-conditions")
    await user.should_not_see(marker="field-0")
    assert element_of(user, "group-kind").value == "all"


async def test_a_pattern_that_does_not_compile_shows_its_error_and_stores_nothing(user, sessions, account_copy):
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-name", "Broken")
    set_value(user, "rule-playlist", "WL")
    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "value-0"), "The condition")

    set_value(user, "value-0", "Levels (of")
    user.find(marker="editor-save").click()

    await error_is(user, "Title: The pattern does not compile: missing ), unterminated subpattern at position 7")
    assert stored_rules(sessions) == []
    set_value(user, "value-0", r"Levels \(of")
    await save(user)
    assert stored_rules(sessions)[0].condition == {"and": [{"regex": [TITLE, r"Levels \(of", "i"]}]}


async def test_an_empty_pattern_stops_the_save(user, sessions, account_copy):
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-name", "Empty pattern")
    set_value(user, "rule-playlist", "WL")
    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "value-0"), "The condition")

    user.find(marker="editor-save").click()

    await error_is(user, "Title: Enter a pattern.")
    assert stored_rules(sessions) == []


@pytest.mark.parametrize(("minutes", "seconds"), [(1.5, None), (None, 60.0)])
async def test_a_duration_without_whole_minutes_and_seconds_stops_the_save(
    user, sessions, account_copy, minutes, seconds
):
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-name", "Long")
    set_value(user, "rule-playlist", "WL")
    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "field-0"), "The condition")
    set_value(user, "field-0", "duration_s")
    await eventually(lambda: elements_of(user, "minutes-0"), "The input of the minutes")

    set_value(user, "minutes-0", minutes)
    set_value(user, "seconds-0", seconds)
    user.find(marker="editor-save").click()

    await error_is(user, "Duration: Enter whole minutes, 0 or more, and whole seconds from 0 to 59.")
    assert stored_rules(sessions) == []


async def test_a_rebuild_of_the_editor_keeps_a_wrong_duration(user, account_copy):
    await user.open("/rules")
    await open_editor(user)
    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "field-0"), "The condition")
    set_value(user, "field-0", "duration_s")
    await eventually(lambda: elements_of(user, "minutes-0"), "The input of the minutes")
    set_value(user, "minutes-0", 1.5)

    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "field-1"), "The second condition")

    assert (element_of(user, "minutes-0").value, element_of(user, "seconds-0").value) == (1.5, None)


@pytest.mark.parametrize(
    ("name", "playlist_id", "error"),
    [
        ("", "WL", "Enter a name."),
        ("   ", "WL", "Enter a name."),
        ("A rule", None, "Choose a target playlist."),
    ],
)
async def test_a_rule_needs_a_name_and_a_target_playlist(user, sessions, account_copy, name, playlist_id, error):
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-name", name)
    set_value(user, "rule-playlist", playlist_id)

    user.find(marker="editor-save").click()

    await error_is(user, error)
    assert stored_rules(sessions) == []
    await user.should_see(marker="editor")


async def test_without_a_stored_playlist_the_editor_says_where_the_lists_come_from(user, sessions):
    await user.open("/rules")
    await user.should_see(marker="refresh-lists")
    await open_editor(user)

    assert text_of(user, "rule-no-playlists") == (
        "No playlist is stored yet. Close the editor and refresh the lists with the button Refresh lists at the top of "
        "the page."
    )
    assert options(user, "rule-playlist") == {}
    assert options(user, "rule-channels") == {}
    set_value(user, "rule-name", "A rule")
    user.find(marker="editor-save").click()
    await error_is(user, "Choose a target playlist.")


async def test_the_editor_offers_the_stored_channels_and_the_own_playlists(user, account_copy):
    await user.open("/rules")
    await open_editor(user)

    await user.should_not_see(marker="rule-no-playlists")
    assert list(options(user, "rule-playlist").items()) == [("PLhome", "Home"), ("WL", "Watch later")]
    assert list(options(user, "rule-channels").items()) == [
        (A, "Alpha (@alpha)"),
        (B, "Beta"),
        (C, "Gamma (@gamma), not subscribed"),
    ]
    assert text_of(user, "rule-new-videos-hint") == (
        "A run acts only on new videos. It does not go back to the videos that are already on the channels of the "
        "rule, also after a change or when you turn the rule on again. To add older videos of a saved rule, use Apply "
        "to videos from a date."
    )
    assert element_of(user, "rule-enabled").value is True
    assert element_of(user, "rule-no-shorts").value is False
    await user.should_not_see(marker="editor-delete")


async def test_the_hint_about_the_feed_shows_only_without_channels(user, account_copy):
    await user.open("/rules")
    await open_editor(user)

    assert text_of(user, "rule-feed-hint") == (
        "Without channels, the rule reads the subscriptions feed. The feed needs the YouTube account."
    )
    set_value(user, "rule-channels", [C])
    await user.should_not_see(marker="rule-feed-hint")
    set_value(user, "rule-channels", [])
    await user.should_see(marker="rule-feed-hint")


async def test_a_rule_keeps_a_target_playlist_that_is_not_an_own_playlist_any_more(user, three_rules):
    await user.open("/rules")

    await open_editor(user, 3)

    assert element_of(user, "rule-playlist").value == "PLsaved"
    assert list(options(user, "rule-playlist").items()) == [
        ("PLhome", "Home"),
        ("WL", "Watch later"),
        ("PLsaved", "Saved from another channel"),
    ]


async def test_a_change_of_a_rule_is_stored(user, sessions, account_copy):
    store(sessions, rule(1, "Old name", {"and": [LEVELS]}, position=4), rule(2, "Other", position=7))
    store(sessions, *links(1, A))
    await user.open("/rules")
    await open_editor(user, 1)
    assert text_of(user, "editor-title") == "Change the rule"
    assert element_of(user, "rule-name").value == "Old name"
    assert element_of(user, "rule-channels").value == [A]
    assert element_of(user, "rule-playlist").value == "WL"
    assert element_of(user, "value-0").value == "Levels of"
    assert element_of(user, "case-0").value is False

    set_value(user, "rule-name", "New name")
    set_value(user, "rule-channels", [B, C])
    set_value(user, "rule-playlist", "PLhome")
    user.find(marker="rule-enabled").click()
    user.find(marker="rule-no-shorts").click()
    set_value(user, "value-0", "Pizza")
    user.find(marker="case-0").click()
    await save(user)

    stored = stored_rule(sessions, 1)
    assert (stored.name, stored.enabled, stored.no_shorts, stored.target_playlist_id, stored.position) == (
        "New name",
        False,
        True,
        "PLhome",
        4,
    )
    assert stored.condition == {"and": [{"regex": [TITLE, "Pizza"]}]}
    assert rule_channels(sessions, 1) == [B, C]
    assert stored.created_at == CHANGED_AT
    assert stored.updated_at > CHANGED_AT
    assert stored_rule(sessions, 2).updated_at == CHANGED_AT
    await names_are(user, ["New name", "Other"])
    row = table_rows(user)[0]
    assert (row["enabled"], row["no_shorts"], row["channels"], row["playlist"], row["condition"]) == (
        "No",
        "Yes",
        "Beta, Gamma",
        "Home",
        'Title matches the pattern "Pizza" (case-sensitive)',
    )
    assert row["updated"] != "2026-10-01 15:00 EEST"


async def test_a_change_of_the_channels_alone_changes_the_time_of_the_change(user, sessions, account_copy):
    store(sessions, rule(1, "A rule"))
    store(sessions, *links(1, A))
    await user.open("/rules")
    await open_editor(user, 1)

    set_value(user, "rule-channels", [A, B])
    await save(user)

    assert rule_channels(sessions, 1) == [A, B]
    assert stored_rule(sessions, 1).updated_at > CHANGED_AT


async def test_a_disabled_rule_turns_on_in_the_editor(user, sessions, account_copy):
    store(sessions, rule(1, "Off", enabled=False))
    await user.open("/rules")
    assert table_rows(user)[0]["enabled"] == "No"
    await open_editor(user, 1)
    assert element_of(user, "rule-enabled").value is False

    user.find(marker="rule-enabled").click()
    await save(user)

    assert stored_rule(sessions, 1).enabled is True
    await eventually(lambda: table_rows(user)[0]["enabled"] == "Yes", "The new state in the table")


async def test_cancel_closes_the_editor_and_keeps_the_rule(user, sessions, account_copy):
    store(sessions, rule(1, "Kept", {"and": [LEVELS]}))
    await user.open("/rules")
    await open_editor(user, 1)
    set_value(user, "rule-name", "Changed")
    set_value(user, "value-0", "Changed")
    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "field-1"), "The second condition")

    user.find(marker="editor-cancel").click()

    await eventually(lambda: not elements_of(user, "editor"), "The close of the editor")
    stored = stored_rule(sessions, 1)
    assert (stored.name, stored.condition, stored.updated_at) == ("Kept", {"and": [LEVELS]}, CHANGED_AT)
    await open_editor(user, 1)
    assert element_of(user, "rule-name").value == "Kept"
    assert element_of(user, "value-0").value == "Levels of"
    await user.should_not_see(marker="field-1")


async def test_a_new_rule_goes_to_the_end_of_the_order(user, sessions, account_copy):
    store(sessions, rule(1, "First", position=9), rule(2, "Second", position=5))
    await user.open("/rules")
    await open_editor(user)

    set_value(user, "rule-name", "Third")
    set_value(user, "rule-playlist", "WL")
    await save(user)

    assert [(stored.name, stored.position) for stored in stored_rules(sessions)] == [
        ("First", 9),
        ("Second", 5),
        ("Third", 10),
    ]
    await names_are(user, ["Second", "First", "Third"])


@pytest.fixture
def slow_save(monkeypatch):
    """Makes each save of a rule take 0.3 s. The event is set when a save starts."""
    real_save_rule = rules_page.save_rule
    started = threading.Event()

    def slow_save_rule(*arguments):
        started.set()
        time.sleep(0.3)
        return real_save_rule(*arguments)

    monkeypatch.setattr(rules_page, "save_rule", slow_save_rule)
    return started


@pytest.mark.parametrize("wait_for_the_first_save", [False, True])
async def test_a_second_click_on_save_makes_no_second_rule(
    user, sessions, account_copy, slow_save, wait_for_the_first_save
):
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-name", "Once")
    set_value(user, "rule-playlist", "WL")

    # Two clicks without a wait both reach the page before the first save starts, as a quick double click does.
    user.find(marker="editor-save").click()
    if wait_for_the_first_save:
        await eventually(slow_save.is_set, "The start of the save")
        assert element_of(user, "editor-save").enabled is False
    user.find(marker="editor-save").click()
    await user.should_see("The rule is saved.", retries=50)
    await asyncio.sleep(SETTLE_SECONDS)

    assert [stored.name for stored in stored_rules(sessions)] == ["Once"]


async def test_delete_asks_first_and_the_history_keeps_its_rule_ids(user, sessions, account_copy):
    store(sessions, rule(1, "Doomed"), rule(2, "Stays"))
    store(
        sessions,
        *links(1, A, B),
        *links(2, A),
        Run(id=1, status=RunStatus.SUCCEEDED, trigger=RunTrigger.MANUAL),
        AddedVideo(playlist_id="WL", video_id="vid1", rule_id=1),
    )
    store(sessions, RunAction(run_id=1, action=RunActionKind.ADDED, video_id="vid1", rule_id=1, playlist_id="WL"))
    await user.open("/rules")
    await open_editor(user, 1)

    user.find(marker="editor-delete").click()
    await eventually(lambda: elements_of(user, "delete-dialog"), "The question")
    assert text_of(user, "delete-question") == 'Delete the rule "Doomed"?'
    user.find(marker="delete-cancel").click()
    await eventually(lambda: not elements_of(user, "delete-dialog"), "The close of the question")
    await asyncio.sleep(SETTLE_SECONDS)
    assert stored_rule(sessions, 1) is not None
    await user.should_see(marker="editor")

    user.find(marker="editor-delete").click()
    await eventually(lambda: elements_of(user, "delete-confirm"), "The question")
    user.find(marker="delete-confirm").click()

    await user.should_see("The rule is deleted.")
    await names_are(user, ["Stays"])
    await eventually(lambda: not elements_of(user, "editor"), "The close of the editor")
    assert stored_rule(sessions, 1) is None
    assert rule_channels(sessions, 1) == []
    assert rule_channels(sessions, 2) == [A]
    with sessions() as session:
        assert session.get(AddedVideo, ("WL", "vid1")).rule_id is None
        assert session.scalars(select(RunAction.rule_id)).all() == [1]


async def test_a_delete_while_a_run_holds_the_sync_lock_keeps_the_rule(user, sessions, account_copy):
    # A run writes the rule id with each video that it adds, so the rule must not go during the run.
    store(sessions, rule(1, "Busy"))
    await user.open("/rules")
    await open_editor(user, 1)

    with sync_lock_held(timeout=0):
        user.find(marker="editor-delete").click()
        await eventually(lambda: elements_of(user, "delete-confirm"), "The question")
        user.find(marker="delete-confirm").click()
        await user.should_see("A sync run or another task with YouTube is active now. Delete the rule when it ends.")

    assert stored_rule(sessions, 1) is not None
    await user.should_see(marker="editor")


async def test_a_save_of_a_rule_that_is_gone_creates_nothing(user, sessions, account_copy):
    store(sessions, rule(1, "Gone soon"))
    await user.open("/rules")
    await open_editor(user, 1)
    with sessions.begin() as session:
        session.delete(session.get(Rule, 1))

    set_value(user, "rule-name", "Changed")
    await save(user, "The rule does not exist any more, so the page did not save it.")

    assert stored_rules(sessions) == []
    await eventually(lambda: elements_of(user, "no-rules"), "The empty table")


async def test_a_click_on_a_rule_that_is_gone_says_so(user, sessions, account_copy):
    store(sessions, rule(1, "Gone soon"), rule(2, "Stays"))
    await user.open("/rules")
    with sessions.begin() as session:
        session.delete(session.get(Rule, 1))

    user.find(marker="rules-table").trigger("rowClick", args=1)

    await user.should_see("The rule does not exist any more.")
    await names_are(user, ["Stays"])
    await user.should_not_see(marker="editor")


async def test_the_editor_shows_the_newest_copy_of_the_account(user, sessions, account_copy):
    await user.open("/rules")
    store(sessions, channel("UCdddddddddddddddddddddd", "Delta"), playlist("PLnew", "New list"))

    await open_editor(user)

    assert options(user, "rule-channels")["UCdddddddddddddddddddddd"] == "Delta"
    assert options(user, "rule-playlist")["PLnew"] == "New list"


async def test_groups_nest_in_the_editor(user, sessions, account_copy):
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-name", "Nested")
    set_value(user, "rule-playlist", "WL")

    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "value-0"), "The first condition")
    set_value(user, "value-0", "Levels")
    user.find(marker="add-group").click()
    await eventually(lambda: elements_of(user, "group-kind-1"), "The nested group")
    assert element_of(user, "group-kind-1").value == "any"
    set_value(user, "group-kind-1", "none")
    await choose(user, "field-1-0", "is_short")
    assert element_of(user, "value-1-0").value is True
    await user.should_not_see(marker="operator-1-0")
    user.find(marker="add-condition-1").click()
    await eventually(lambda: elements_of(user, "field-1-1"), "The second condition of the nested group")
    await choose(user, "field-1-1", "live_status")
    assert list(options(user, "value-1-1")) == list(LIVE_STATUSES)
    set_value(user, "value-1-1", "is_upcoming")
    user.find(marker="add-group-1").click()
    await eventually(lambda: elements_of(user, "group-kind-1-2"), "The group in the nested group")
    set_value(user, "group-kind-1-2", "all")
    await choose(user, "field-1-2-0", "duration_s")
    await choose(user, "operator-1-2-0", "<")
    set_value(user, "minutes-1-2-0", 1.0)
    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "field-2"), "The third condition of the root")
    user.find(marker="remove-2").click()
    await eventually(lambda: not elements_of(user, "field-2"), "The removal of the third condition")
    set_value(user, "group-kind", "any")
    await save(user)

    (stored,) = stored_rules(sessions)
    assert stored.condition == {
        "or": [
            {"regex": [TITLE, "Levels", "i"]},
            {
                "!": [
                    {
                        "or": [
                            {"==": [{"var": "is_short"}, True]},
                            {"==": [{"var": "live_status"}, "is_upcoming"]},
                            {"and": [{"<": [DURATION, 60]}]},
                        ]
                    }
                ]
            },
        ]
    }


async def test_a_removed_group_takes_its_conditions_with_it(user, sessions, account_copy):
    store(sessions, rule(1, "Two groups", {"and": [{"or": [LEVELS, LONG]}, {"or": [LONG]}]}))
    await user.open("/rules")
    await open_editor(user, 1)

    user.find(marker="remove-0").click()
    await eventually(lambda: not elements_of(user, "group-kind-1"), "The removal of the first group")
    await save(user)

    assert stored_rule(sessions, 1).condition == {"and": [{"or": [LONG]}]}


async def test_a_group_without_conditions_is_left_out_at_the_save(user, sessions, account_copy):
    store(sessions, rule(1, "One condition", {"and": [LONG]}))
    await user.open("/rules")
    await open_editor(user, 1)

    user.find(marker="add-group").click()
    await eventually(lambda: elements_of(user, "remove-1-0"), "The new group")
    user.find(marker="remove-1-0").click()
    await eventually(lambda: not elements_of(user, "remove-1-0"), "The empty group")
    await user.should_see(marker="group-kind-1")
    await save(user)

    assert stored_rule(sessions, 1).condition == {"and": [LONG]}


@pytest.mark.parametrize(
    ("field", "operator", "value", "kind", "expected"),
    [
        ("title", "==", "A video", ui.input, {"==": [TITLE, "A video"]}),
        ("video_id", "!=", "vid1", ui.input, {"!=": [{"var": "video_id"}, "vid1"]}),
        ("channel_id", "in", [A, C], ui.select, {"in": [{"var": "channel_id"}, [A, C]]}),
        ("channel_id", "==", B, ui.select, {"==": [{"var": "channel_id"}, B]}),
        ("channel_id", "regex", "^UCa", ui.input, {"regex": [{"var": "channel_id"}, "^UCa", "i"]}),
        ("upload_date", "<", "2026-10-01", ui.input, {"<": [{"var": "upload_date"}, "2026-10-01"]}),
        ("live_status", "!=", "was_live", ui.select, {"!=": [{"var": "live_status"}, "was_live"]}),
        ("is_short", None, False, ui.select, {"==": [{"var": "is_short"}, False]}),
    ],
)
async def test_each_kind_of_value_has_its_input(user, sessions, account_copy, field, operator, value, kind, expected):
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-name", "One condition")
    set_value(user, "rule-playlist", "WL")
    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "field-0"), "The condition")

    await choose(user, "field-0", field)
    if operator is not None:
        await choose(user, "operator-0", operator)
    assert type(element_of(user, "value-0")) is kind
    set_value(user, "value-0", value)
    await save(user)

    assert stored_rules(sessions)[0].condition == {"and": [expected]}


async def test_the_duration_has_a_field_for_the_minutes_and_one_for_the_seconds(user, sessions, account_copy):
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-name", "Up to a minute and a half")
    set_value(user, "rule-playlist", "WL")
    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "field-0"), "The condition")
    await choose(user, "field-0", "duration_s")
    await choose(user, "operator-0", "<=")
    assert (type(element_of(user, "minutes-0")), type(element_of(user, "seconds-0"))) == (ui.number, ui.number)

    set_value(user, "minutes-0", 1.0)
    set_value(user, "seconds-0", 30.0)
    await save(user)

    assert stored_rules(sessions)[0].condition == {"and": [{"<=": [DURATION, 90]}]}
    await open_editor(user, 1)
    assert (element_of(user, "minutes-0").value, element_of(user, "seconds-0").value) == (1, 30)


async def test_the_upload_date_gets_today_from_its_button(user, sessions, account_copy):
    await user.open("/rules")
    await open_editor(user)
    set_value(user, "rule-name", "From today")
    set_value(user, "rule-playlist", "WL")
    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "field-0"), "The condition")
    await choose(user, "field-0", "upload_date")
    today = today_in("Europe/Kyiv").isoformat()

    user.find(marker="today-0").click()

    await eventually(lambda: element_of(user, "value-0").value == today, "Today as the upload date")
    await save(user)
    assert stored_rules(sessions)[0].condition == {"and": [{">=": [{"var": "upload_date"}, today]}]}


async def test_a_new_comparison_keeps_the_channel(user, account_copy):
    await user.open("/rules")
    await open_editor(user)
    user.find(marker="add-condition").click()
    await eventually(lambda: elements_of(user, "field-0"), "The condition")
    set_value(user, "field-0", "channel_id")
    await eventually(lambda: element_of(user, "operator-0").value == "in", "The channel list")
    set_value(user, "value-0", [B, A])

    set_value(user, "operator-0", "==")

    await eventually(lambda: element_of(user, "value-0").value == B, "The one channel")
    assert element_of(user, "value-0").multiple is False


def shown_texts(user) -> list[str]:
    """Each text that the page shows: the text of an element, the label of an input, each option of a select, each
    column label of a table, and each cell of a table that the page writes itself."""
    texts = []
    for element in user.client.elements.values():
        if isinstance(element, TextElement) and element.text:
            texts.append(element.text)
        if isinstance(element, LabelElement) and element.props.get("label"):
            texts.append(element.props["label"])
        if isinstance(element, ui.select) and isinstance(element.options, dict):
            texts.extend(element.options.values())
        if isinstance(element, ui.table):
            texts.extend(column["label"] for column in element.columns)
            for row in element.rows:
                texts.extend(row[name] for name in ("enabled", "no_shorts"))
                if row["id"] == 2:
                    texts.extend((row["channels"], row["condition"]))
    return texts


async def test_each_text_of_the_rules_page_goes_through_the_translation(
    nicegui_globals, settings, sessions, fake_starter, monkeypatch
):
    class Marking:
        def gettext(self, message):
            return f"<<{message}>>"

    monkeypatch.setattr(i18n, "_translations", Marking())
    condition = {
        "and": [
            LEVELS,
            {"!": [{"or": [{"==": [{"var": "channel_id"}, A]}, {"in": [{"var": "channel_id"}, [B]]}]}]},
            {"or": [LONG, {"<": [{"var": "upload_date"}, "2026-10-01"]}]},
            {"==": [{"var": "live_status"}, "was_live"]},
            {"==": [{"var": "is_short"}, False]},
            {"==": [TITLE, "Exact"]},
        ]
    }
    store(sessions, channel(A, "Alpha", "@alpha"), channel(C, "Gamma", subscribed=False), playlist("WL", "Later"))
    store(
        sessions,
        rule(1, "Rule one", condition, no_shorts=True),
        rule(2, "Feed rule", enabled=False),
        rule(3, "JSON rule", LONG),
    )
    store(sessions, *links(1, A, C))
    # The data of the page: the names of the rules, the stored titles, a channel id that the copy does not have, and
    # the values of the conditions.
    data = {"Rule one", "Feed rule", "JSON rule", "Alpha (@alpha)", "Later", B}
    time_text = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+")

    async with running_service(settings, fake_starter) as user:
        await user.open("/rules")
        await open_editor(user, 1)
        user.find(marker="editor-delete").click()
        await eventually(lambda: elements_of(user, "delete-dialog"), "The question")
        texts = shown_texts(user)
        user.find(marker="delete-cancel").click()
        await eventually(lambda: not elements_of(user, "delete-dialog"), "The close of the question")
        user.find(marker="editor-cancel").click()
        await eventually(lambda: not elements_of(user, "editor"), "The close of the editor")
        await open_editor(user, 3)
        texts.extend(shown_texts(user))

    for expected in (
        "<<Rules>>",
        "<<No Shorts>>",
        '<<Delete the rule "Rule one"?>>',
        "<<Gamma, not subscribed>>",
        "<<is not a Short>>",
        "<<Case-sensitive>>",
        "<<Minutes>>",
        "<<Seconds>>",
        "<<The visual editor cannot show this condition. Edit it as JsonLogic.>>",
    ):
        assert expected in texts
    untranslated = [
        text
        for text in texts
        if not (text.startswith("<<") and text.endswith(">>")) and text not in data and not time_text.fullmatch(text)
    ]
    assert untranslated == []
