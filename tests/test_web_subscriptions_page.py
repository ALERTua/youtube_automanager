"""Tests of the subscriptions page. A fake child stands in for each child process, so no test reads YouTube."""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from nicegui import ui
from nicegui.elements.mixins.label_element import LabelElement
from nicegui.elements.mixins.text_element import TextElement
from sqlalchemy import select
from ui_support import element_of, elements_of, eventually, running_service, set_value, store, text_of

from youtube_automanager import i18n
from youtube_automanager.account_cache import REFRESHED_AT_KEY
from youtube_automanager.db.models import Channel, Playlist, Rule, RuleChannel, Schedule, ScheduleKind, Setting
from youtube_automanager.runner import sync_lock_held
from youtube_automanager.short_child import EXIT_LOGGED_OUT, EXIT_UNAVAILABLE, EXIT_YOUTUBE_ERROR
from youtube_automanager.web.ui.dates import today_in

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("logging_setups", "no_logged_errors")]

ALPHA = "UCaaaaaaaaaaaaaaaaaaaaaa"
BRAVO = "UCbbbbbbbbbbbbbbbbbbbbbb"
CHARLIE = "UCcccccccccccccccccccccc"
DELTA = "UCdddddddddddddddddddddd"
FOUND = "UCffffffffffffffffffffff"
TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+")
# How long a test waits for a click that must change nothing. A click that does change something shows it within ms.
SETTLE_SECONDS = 0.3
BUSY_TEXT = "Another task works with YouTube now"
COUNTS = {
    "channels_new": 1,
    "channels_updated": 2,
    "channels_unsubscribed": 3,
    "playlists_new": 4,
    "playlists_updated": 5,
}


def channel(channel_id: str, title: str, handle: str | None = None, *, subscribed: bool = True, **values) -> Channel:
    return Channel(channel_id=channel_id, title=title, handle=handle, is_subscribed=subscribed, **values)


def rule(rule_id: int, name: str, *, enabled: bool = True) -> Rule:
    return Rule(
        id=rule_id,
        name=name,
        enabled=enabled,
        position=rule_id,
        condition={"==": [1, 1]},
        target_playlist_id="WL",
        no_shorts=False,
    )


def watch_later() -> Playlist:
    return Playlist(playlist_id="WL", title="Watch later", is_own=True)


def found(channel_id: str, title: str, handle: str | None = None, count: int | None = None, thumbnail=None) -> dict:
    return {
        "channel_id": channel_id,
        "title": title,
        "handle": handle,
        "subscriber_count": count,
        "thumbnail_url": thumbnail,
    }


def stderr_line(value) -> str:
    return json.dumps(value) + "\n"


@pytest.fixture
def stored_channels(sessions):
    """Four stored channels, and two rules that read two of them. The second rule is off."""
    store(
        sessions,
        watch_later(),
        channel(ALPHA, "alpha news", "@AlphaNews"),
        channel(BRAVO, "Bravo Cooking", None, subscribed=False),
        channel(CHARLIE, "Charlie", "@charlie_tv"),
        channel(DELTA, "Delta Science", "@deltascience", subscribed=False),
        rule(1, "Science"),
        rule(2, "Cooking", enabled=False),
    )
    store(
        sessions,
        RuleChannel(rule_id=1, channel_id=DELTA),
        RuleChannel(rule_id=2, channel_id=BRAVO),
        RuleChannel(rule_id=1, channel_id=BRAVO),
    )


def rows_of(user, marker: str) -> list[dict]:
    return element_of(user, marker).rows


def channel_ids(user) -> list[str]:
    return [row["id"] for row in rows_of(user, "channels-table")]


async def channels_are(user, expected: list[str]) -> None:
    await eventually(lambda: channel_ids(user) == expected, f"The table of the channels with {expected}")


def dialog_shows(user, channel_id: str) -> bool:
    links = elements_of(user, "channel-link")
    return element_of(user, "channel-dialog").value and len(links) == 1 and links[0].props["href"].endswith(channel_id)


async def open_channel(user, channel_id: str, marker: str = "channels-table") -> None:
    user.find(marker=marker).trigger("rowClick", args=channel_id)
    await eventually(lambda: dialog_shows(user, channel_id), f"The open dialog of channel {channel_id}")


def stored_channel(sessions, channel_id: str) -> Channel | None:
    with sessions() as session:
        return session.get(Channel, channel_id)


def rule_links(sessions) -> set[tuple[int, str]]:
    with sessions() as session:
        return {(link.rule_id, link.channel_id) for link in session.scalars(select(RuleChannel))}


def commands(fake_starter) -> list[list[str]]:
    return [command[3:] for command, _env in fake_starter.calls]


# The page and the stored list.


async def test_the_subscriptions_page_is_the_second_page_of_the_drawer(user):
    await user.open("/")

    user.find(marker="nav:/subscriptions").click()

    await user.should_see(marker="channels-table")


async def test_a_new_service_has_no_channels_and_no_refresh(user, fake_starter):
    await user.open("/subscriptions")

    assert text_of(user, "lists-refreshed") == "The lists were never refreshed."
    assert text_of(user, "channels-summary") == "0 of 0 channels"
    assert text_of(user, "no-channels") == "No channel is stored yet. Press Refresh lists."
    assert channel_ids(user) == []
    # A page load starts no child.
    assert fake_starter.calls == []


async def test_the_stored_channels_come_by_title_with_their_state_and_their_rules(user, stored_channels):
    await user.open("/subscriptions")

    table = element_of(user, "channels-table")
    assert [column["label"] for column in table.columns] == ["Channel", "Handle", "Subscribed", "Rules"]
    assert table.rows == [
        {
            "id": ALPHA,
            "title": "alpha news",
            "thumbnail": "",
            "handle": "@AlphaNews",
            "subscribed": "Yes",
            "rules": "",
        },
        {
            "id": BRAVO,
            "title": "Bravo Cooking",
            "thumbnail": "",
            "handle": "",
            "subscribed": "No",
            "rules": "Science, Cooking (off)",
        },
        {
            "id": CHARLIE,
            "title": "Charlie",
            "thumbnail": "",
            "handle": "@charlie_tv",
            "subscribed": "Yes",
            "rules": "",
        },
        {
            "id": DELTA,
            "title": "Delta Science",
            "thumbnail": "",
            "handle": "@deltascience",
            "subscribed": "No",
            "rules": "Science",
        },
    ]
    assert text_of(user, "channels-summary") == "4 of 4 channels"
    await user.should_not_see(marker="no-channels")


async def test_the_time_of_the_last_refresh_shows_in_the_time_zone_of_the_schedule(
    nicegui_globals, settings, sessions, fake_starter
):
    store(
        sessions,
        Schedule(enabled=True, kind=ScheduleKind.CRON, timezone="America/New_York"),
        Setting(key=REFRESHED_AT_KEY, value=datetime(2026, 1, 5, 1, 0, tzinfo=UTC).isoformat()),
    )

    async with running_service(settings, fake_starter) as user:
        await user.open("/subscriptions")

        assert text_of(user, "lists-refreshed") == "Lists refreshed: 2026-01-04 20:00 EST"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", [ALPHA, BRAVO, CHARLIE, DELTA]),
        ("science", [DELTA]),
        ("ALPHA", [ALPHA]),
        # A handle matches with and without its "@", in any case.
        ("charlie_TV", [CHARLIE]),
        ("@charlie", [CHARLIE]),
        ("@alphanews", [ALPHA]),
        ("  cooking ", [BRAVO]),
        ("@", [ALPHA, BRAVO, CHARLIE, DELTA]),
        ("nothing like this", []),
    ],
)
async def test_the_text_filter_reads_the_title_and_the_handle(user, stored_channels, text, expected):
    await user.open("/subscriptions")

    set_value(user, "filter-text", text)

    await channels_are(user, expected)
    assert text_of(user, "channels-summary") == f"{len(expected)} of 4 channels"


async def test_a_cleared_text_filter_shows_each_channel(user, stored_channels):
    await user.open("/subscriptions")
    user.find(marker="filter-text").type("delta")
    await channels_are(user, [DELTA])

    user.find(marker="filter-text").clear()

    await channels_are(user, [ALPHA, BRAVO, CHARLIE, DELTA])


async def test_a_filter_without_matches_says_so(user, stored_channels):
    await user.open("/subscriptions")

    set_value(user, "filter-text", "nothing like this")

    await eventually(lambda: element_of(user, "no-channels").visible, "The message that no channel matches")
    assert text_of(user, "no-channels") == "No channel matches the filters."
    assert text_of(user, "channels-summary") == "0 of 4 channels"


async def test_the_subscription_filter(user, stored_channels):
    await user.open("/subscriptions")

    user.find(marker="filter-subscribed").click()
    user.find("Not subscribed").click()
    await channels_are(user, [BRAVO, DELTA])

    set_value(user, "filter-subscribed", "yes")
    await channels_are(user, [ALPHA, CHARLIE])
    set_value(user, "filter-subscribed", "any")
    await channels_are(user, [ALPHA, BRAVO, CHARLIE, DELTA])


async def test_the_rules_filter(user, stored_channels):
    await user.open("/subscriptions")

    set_value(user, "filter-rules", "yes")
    await channels_are(user, [BRAVO, DELTA])
    set_value(user, "filter-rules", "no")
    await channels_are(user, [ALPHA, CHARLIE])
    set_value(user, "filter-rules", "any")
    await channels_are(user, [ALPHA, BRAVO, CHARLIE, DELTA])


async def test_the_filters_work_together(user, stored_channels):
    await user.open("/subscriptions")

    set_value(user, "filter-subscribed", "no")
    set_value(user, "filter-rules", "yes")
    set_value(user, "filter-text", "science")

    await channels_are(user, [DELTA])


async def test_the_table_has_pages_of_50_channels(user, sessions):
    store(sessions, *[channel(f"UC{number:022d}", f"Channel {number:03d}") for number in range(120)])

    await user.open("/subscriptions")

    table = element_of(user, "channels-table")
    assert len(table.rows) == 120
    assert table.pagination == {"rowsPerPage": 50}


# The refresh of the lists.


async def test_the_refresh_of_the_lists_shows_the_counts_the_new_time_and_the_new_channels(
    user, sessions, fake_starter
):
    await user.open("/subscriptions")
    # The refresh child writes the database. The fake child does not, so the test writes what the child would write.
    store(
        sessions,
        channel(ALPHA, "Alpha"),
        Setting(key=REFRESHED_AT_KEY, value=datetime(2026, 10, 2, 15, 5, tzinfo=UTC).isoformat()),
    )
    fake_starter.plan.append({"stderr": stderr_line(COUNTS)})

    user.find(marker="refresh-lists").click()

    await eventually(lambda: channel_ids(user) == [ALPHA], "The new channel in the table")
    assert text_of(user, "refresh-lists-result") == (
        "The lists are refreshed. Channels: 1 new, 2 changed, 3 no longer subscribed. Playlists: 4 new, 5 changed."
    )
    # Kyiv is at UTC+3 until 25 October 2026.
    assert text_of(user, "lists-refreshed") == "Lists refreshed: 2026-10-02 18:05 EEST"
    assert commands(fake_starter) == [["refresh-account"]]


@pytest.mark.parametrize(
    ("child", "expected"),
    [
        (
            {"exit_code": EXIT_UNAVAILABLE, "stderr": "no YouTube cookies are stored\n"},
            "The lists did not refresh. The YouTube account is not available: no YouTube cookies are stored",
        ),
        (
            {"exit_code": EXIT_LOGGED_OUT, "stderr": "YouTube refused the session\n"},
            (
                "The lists did not refresh. YouTube does not accept the stored cookies any more (YouTube refused the "
                "session). Export new YouTube cookies from your browser and upload them to the service."
            ),
        ),
        (
            {"exit_code": EXIT_YOUTUBE_ERROR, "stderr": "HTTP Error 429\n"},
            "The lists did not refresh. YouTube did not give the expected answer: HTTP Error 429",
        ),
        (
            {"exit_code": 1, "stderr": "Traceback (most recent call last):\nValueError: secret\n"},
            "The lists did not refresh. The service could not finish the work: The refresh process exited with code 1.",
        ),
    ],
)
async def test_a_failed_refresh_of_the_lists_shows_its_cause(user, fake_starter, child, expected):
    await user.open("/subscriptions")
    fake_starter.plan.append(child)

    user.find(marker="refresh-lists").click()

    await eventually(lambda: text_of(user, "refresh-lists-result") == expected, "The cause of the failed refresh")
    assert "text-negative" in element_of(user, "refresh-lists-result").classes
    assert text_of(user, "lists-refreshed") == "The lists were never refreshed."


async def test_a_refresh_of_the_lists_during_a_run_starts_nothing_and_says_so(user, fake_starter):
    await user.open("/subscriptions")

    with sync_lock_held():
        user.find(marker="refresh-lists").click()
        await eventually(lambda: BUSY_TEXT in text_of(user, "refresh-lists-result"), "The busy text")

    assert fake_starter.calls == []


# The search.


async def test_a_search_shows_the_found_channels_with_their_stored_state(user, stored_channels, fake_starter):
    fake_starter.plan.append(
        {
            "stderr": stderr_line(
                [
                    found(FOUND, "Found Channel", "@found", 6320000, "https://yt3.example/found.jpg"),
                    found(CHARLIE, "Charlie on YouTube", "@charlie_tv", None),
                    found(BRAVO, "Bravo", None, 12),
                ]
            )
        }
    )
    await user.open("/subscriptions")

    user.find(marker="search-input").type("cooking & co")
    user.find(marker="search").click()

    await eventually(lambda: elements_of(user, "found-table"), "The table of the found channels")
    table = element_of(user, "found-table")
    assert [column["label"] for column in table.columns] == ["Channel", "Handle", "Subscribers", "Subscribed"]
    assert table.rows == [
        {
            "id": FOUND,
            "title": "Found Channel",
            "thumbnail": "https://yt3.example/found.jpg",
            "handle": "@found",
            "subscribers": "6,320,000",
            "subscribed": "No",
        },
        {
            "id": CHARLIE,
            "title": "Charlie on YouTube",
            "thumbnail": "",
            "handle": "@charlie_tv",
            "subscribers": "",
            "subscribed": "Yes",
        },
        {"id": BRAVO, "title": "Bravo", "thumbnail": "", "handle": "", "subscribers": "12", "subscribed": "No"},
    ]
    assert commands(fake_starter) == [["find-channels", "--query=cooking & co"]]


async def test_the_enter_key_starts_the_search(user, fake_starter):
    fake_starter.plan.append({"stderr": stderr_line([])})
    await user.open("/subscriptions")
    user.find(marker="search-input").type("@misha_katsurin")

    user.find(marker="search-input").trigger("keydown.enter")

    await eventually(lambda: elements_of(user, "no-found"), "The message that YouTube found nothing")
    assert text_of(user, "no-found") == "YouTube found no channel for this text."
    assert commands(fake_starter) == [["find-channels", "--query=@misha_katsurin"]]


@pytest.mark.parametrize("query", ["", "   "])
async def test_an_empty_search_starts_no_child(user, fake_starter, query):
    await user.open("/subscriptions")
    set_value(user, "search-input", query)

    user.find(marker="search").click()

    await eventually(lambda: elements_of(user, "search-error"), "The message about the empty search")
    assert text_of(user, "search-error") == "Enter a name, an @handle, a channel ID or a channel URL."
    assert fake_starter.calls == []


@pytest.mark.parametrize(
    ("child", "expected"),
    [
        (
            {"exit_code": EXIT_YOUTUBE_ERROR, "stderr": "yt-dlp failed to read the search: HTTP Error 429\n"},
            (
                "The search failed. YouTube did not give the expected answer: yt-dlp failed to read the search: HTTP Error "
                "429"
            ),
        ),
        (
            {"stderr": "not a list\n"},
            (
                "The search failed. The service could not finish the work: The channel search process ended without the "
                "found channels."
            ),
        ),
    ],
)
async def test_a_failed_search_shows_its_cause_and_hides_the_old_results(user, fake_starter, child, expected):
    fake_starter.plan[:] = [{"stderr": stderr_line([found(FOUND, "Found")])}, child]
    await user.open("/subscriptions")
    set_value(user, "search-input", "found")
    user.find(marker="search").click()
    await eventually(lambda: elements_of(user, "found-table"), "The first results")

    user.find(marker="search").click()

    await eventually(lambda: elements_of(user, "search-error"), "The cause of the failed search")
    assert text_of(user, "search-error") == expected
    assert elements_of(user, "found-table") == []


async def test_a_search_during_a_run_starts_nothing_and_says_so(user, fake_starter):
    await user.open("/subscriptions")
    set_value(user, "search-input", "science")

    with sync_lock_held():
        user.find(marker="search").click()
        await eventually(lambda: elements_of(user, "search-error"), "The busy text")

    assert text_of(user, "search-error").startswith(f"The search failed. {BUSY_TEXT}")
    assert fake_starter.calls == []


async def search_for(user, fake_starter, *channels: dict) -> None:
    fake_starter.plan.append({"stderr": stderr_line(list(channels))})
    set_value(user, "search-input", "query")
    user.find(marker="search").click()
    await eventually(lambda: elements_of(user, "found-table"), "The table of the found channels")


# The dialog of a channel.


async def test_a_click_on_a_stored_channel_opens_its_dialog(user, stored_channels):
    await user.open("/subscriptions")
    assert element_of(user, "channel-dialog").value is False

    await open_channel(user, BRAVO)

    assert text_of(user, "channel-title") == "Bravo Cooking"
    assert text_of(user, "channel-handle") == "No handle"
    assert text_of(user, "channel-subscribed") == "No"
    assert text_of(user, "channel-rules") == "Science, Cooking (off)"
    assert element_of(user, "channel-link").props["href"] == f"https://www.youtube.com/channel/{BRAVO}"
    assert elements_of(user, "channel-subscribe")
    assert elements_of(user, "channel-unsubscribe") == []
    assert elements_of(user, "channel-subscribers") == []

    await open_channel(user, ALPHA)
    assert (text_of(user, "channel-title"), text_of(user, "channel-handle")) == ("alpha news", "@AlphaNews")
    assert text_of(user, "channel-rules") == "No rule reads this channel."
    assert elements_of(user, "channel-unsubscribe")


async def test_a_click_on_a_found_channel_opens_its_dialog_with_the_found_data(user, stored_channels, fake_starter):
    await user.open("/subscriptions")
    await search_for(user, fake_starter, found(FOUND, "Found Channel", "@found", 1234, "https://yt3.example/a.jpg"))

    await open_channel(user, FOUND, "found-table")

    assert text_of(user, "channel-title") == "Found Channel"
    assert text_of(user, "channel-handle") == "@found"
    assert text_of(user, "channel-subscribers") == "1,234"
    assert text_of(user, "channel-subscribed") == "No"
    assert element_of(user, "channel-thumbnail").props["src"] == "https://yt3.example/a.jpg"


async def test_the_close_button_closes_the_dialog(user, stored_channels):
    await user.open("/subscriptions")
    await open_channel(user, ALPHA)

    user.find(marker="channel-close").click()

    await eventually(lambda: element_of(user, "channel-dialog").value is False, "The closed dialog")


# Add to rule.


async def choose_rule(user, rule_id: int) -> None:
    user.find(marker="channel-add-to-rule").click()
    await eventually(lambda: elements_of(user, "rule-select"), "The choice of a rule")
    set_value(user, "rule-select", rule_id)
    await eventually(lambda: element_of(user, "rule-save").enabled, "The save button")


async def test_a_found_channel_goes_into_a_rule_with_a_new_channel_row(user, sessions, fake_starter):
    store(sessions, watch_later(), rule(1, "Science"), channel(ALPHA, "Alpha"))
    store(sessions, RuleChannel(rule_id=1, channel_id=ALPHA))
    await user.open("/subscriptions")
    await search_for(user, fake_starter, found(FOUND, "Found Channel", "@found", 99, "https://yt3.example/f.jpg"))
    await open_channel(user, FOUND, "found-table")

    await choose_rule(user, 1)
    # The rule has a channel, so it does not read the subscriptions feed, and only the fact of the older videos shows.
    assert text_of(user, "fact-catch-up") == (
        "The next run adds the videos of this channel from the last 7 days that the rule matches. After that, the "
        "rule adds each new video of the channel. To add older videos, open the rule and change Also add videos from."
    )
    assert elements_of(user, "fact-feed") == []
    user.find(marker="rule-save").click()

    await eventually(lambda: (1, FOUND) in rule_links(sessions), "The link of the rule and the channel")
    with sessions() as session:
        assert session.get(Rule, 1).catch_up_since == today_in("Europe/Kyiv") - timedelta(days=7)
    row = stored_channel(sessions, FOUND)
    assert (row.title, row.handle, row.thumbnail_url, row.is_subscribed) == (
        "Found Channel",
        "@found",
        "https://yt3.example/f.jpg",
        False,
    )
    assert (row.mark_date, row.last_checked_at) == (None, None)
    await user.should_see("Rule Science now reads Found Channel.")
    await channels_are(user, [ALPHA, FOUND])
    assert rows_of(user, "channels-table")[1]["rules"] == "Science"
    # The dialog rebuilds a moment after the table, so the test waits for its text.
    await eventually(lambda: text_of(user, "channel-rules") == "Science", "The rules of the channel in the dialog")
    # The page writes the database itself, and starts no child for it.
    assert commands(fake_starter) == [["find-channels", "--query=query"]]


async def test_a_rule_without_channels_tells_that_it_stops_to_read_the_feed(user, sessions, fake_starter):
    store(sessions, watch_later(), rule(1, "Feed rule"))
    await user.open("/subscriptions")
    await search_for(user, fake_starter, found(FOUND, "Found Channel"))
    await open_channel(user, FOUND, "found-table")
    user.find(marker="channel-add-to-rule").click()
    await eventually(lambda: elements_of(user, "rule-select"), "The choice of a rule")
    # Before a choice, the save is off, and the feed fact does not show.
    assert element_of(user, "rule-save").enabled is False
    assert elements_of(user, "fact-feed") == []
    assert elements_of(user, "fact-catch-up")

    set_value(user, "rule-select", 1)

    await eventually(lambda: elements_of(user, "fact-feed"), "The fact about the subscriptions feed")
    assert text_of(user, "fact-feed") == (
        "This rule has no channels now, so it reads the subscriptions feed. After this change, it reads only its "
        "channels, and not the subscriptions feed."
    )
    user.find(marker="rule-save").click()
    await eventually(lambda: rule_links(sessions) == {(1, FOUND)}, "The link of the rule and the channel")


async def test_a_found_channel_that_is_stored_keeps_its_row(user, stored_channels, sessions, fake_starter):
    await user.open("/subscriptions")
    await search_for(user, fake_starter, found(ALPHA, "A new title", "@new", 5, "https://yt3.example/new.jpg"))
    await open_channel(user, ALPHA, "found-table")

    await choose_rule(user, 1)
    user.find(marker="rule-save").click()

    await eventually(lambda: (1, ALPHA) in rule_links(sessions), "The link of the rule and the channel")
    row = stored_channel(sessions, ALPHA)
    assert (row.title, row.handle, row.thumbnail_url, row.is_subscribed) == ("alpha news", "@AlphaNews", None, True)


async def test_a_stored_channel_goes_into_a_rule_and_the_rules_that_read_it_are_not_offered(
    user, stored_channels, sessions
):
    store(sessions, rule(3, "Third"))
    await user.open("/subscriptions")
    await open_channel(user, BRAVO)
    user.find(marker="channel-add-to-rule").click()
    await eventually(lambda: elements_of(user, "rule-select"), "The choice of a rule")

    # Rules 1 and 2 already read Bravo.
    assert element_of(user, "rule-select").options == {3: "Third"}
    set_value(user, "rule-select", 3)
    await eventually(lambda: elements_of(user, "fact-feed"), "The fact about the subscriptions feed")
    user.find(marker="rule-save").click()

    await eventually(lambda: (3, BRAVO) in rule_links(sessions), "The link of the rule and the channel")
    await eventually(lambda: text_of(user, "channel-rules") == "Science, Cooking (off), Third", "The new rules")
    await user.open("/subscriptions")
    await open_channel(user, BRAVO)
    user.find(marker="channel-add-to-rule").click()
    await eventually(lambda: elements_of(user, "no-rule-choice"), "The message that each rule reads the channel")
    assert text_of(user, "no-rule-choice") == "Each rule already reads this channel."


async def test_a_disabled_rule_is_offered_with_its_mark(user, sessions):
    store(sessions, watch_later(), rule(1, "Paused rule", enabled=False), channel(ALPHA, "Alpha"))
    await user.open("/subscriptions")
    await open_channel(user, ALPHA)

    user.find(marker="channel-add-to-rule").click()

    await eventually(lambda: elements_of(user, "rule-select"), "The choice of a rule")
    assert element_of(user, "rule-select").options == {1: "Paused rule (off)"}


async def test_without_rules_the_dialog_says_to_create_one(user, sessions):
    store(sessions, channel(ALPHA, "Alpha"))
    await user.open("/subscriptions")
    await open_channel(user, ALPHA)

    user.find(marker="channel-add-to-rule").click()

    await eventually(lambda: elements_of(user, "no-rule-choice"), "The message without rules")
    assert text_of(user, "no-rule-choice") == "No rule exists yet. Create a rule first."
    assert element_of(user, "rule-save").enabled is False
    user.find(marker="rule-back").click()
    await eventually(lambda: elements_of(user, "channel-add-to-rule"), "The details of the channel")


async def test_a_rule_that_was_deleted_after_the_page_showed_it_adds_nothing(user, stored_channels, sessions):
    await user.open("/subscriptions")
    await open_channel(user, ALPHA)
    await choose_rule(user, 1)

    with sessions.begin() as session:
        session.delete(session.get(Rule, 1))
    user.find(marker="rule-save").click()

    await user.should_see("Rule Science does not exist any more.")
    assert {link for link in rule_links(sessions) if link[1] == ALPHA} == set()
    await eventually(lambda: text_of(user, "channel-rules") == "No rule reads this channel.", "The fresh rules")


# Subscribe and unsubscribe.


async def test_subscribe_runs_the_child_and_shows_the_new_state(user, stored_channels, sessions, fake_starter):
    await user.open("/subscriptions")
    await open_channel(user, BRAVO)
    fake_starter.plan.append({"stderr": stderr_line({"channel_id": BRAVO, "subscribed": True})})
    # The child writes the database. The fake child does not, so the test writes what the child would write.
    with sessions.begin() as session:
        session.get(Channel, BRAVO).is_subscribed = True

    user.find(marker="channel-subscribe").click()

    await user.should_see("Subscribed to Bravo Cooking.")
    assert commands(fake_starter) == [["subscribe", f"--channel-id={BRAVO}", "--title=Bravo Cooking"]]
    await eventually(lambda: text_of(user, "channel-subscribed") == "Yes", "The new state in the dialog")
    assert elements_of(user, "channel-unsubscribe")
    assert rows_of(user, "channels-table")[1]["subscribed"] == "Yes"


async def test_subscribe_to_a_found_channel_gives_its_title_to_the_child(user, fake_starter):
    await user.open("/subscriptions")
    await search_for(user, fake_starter, found(FOUND, "Found Channel", "@found"))
    await open_channel(user, FOUND, "found-table")
    fake_starter.plan.append({"stderr": stderr_line({"channel_id": FOUND, "subscribed": True})})

    user.find(marker="channel-subscribe").click()

    await user.should_see("Subscribed to Found Channel.")
    assert commands(fake_starter)[-1] == ["subscribe", f"--channel-id={FOUND}", "--title=Found Channel"]


async def test_unsubscribe_asks_first_and_then_runs_the_child(user, stored_channels, sessions, fake_starter):
    store(sessions, RuleChannel(rule_id=1, channel_id=ALPHA))
    await user.open("/subscriptions")
    await open_channel(user, ALPHA)

    user.find(marker="channel-unsubscribe").click()
    await eventually(lambda: elements_of(user, "confirm-unsubscribe"), "The question before the unsubscribe")
    assert text_of(user, "confirm-text") == "Unsubscribe the YouTube account from this channel?"
    assert text_of(user, "confirm-rules") == "The rules that name this channel still read its videos."
    assert fake_starter.calls == []

    user.find(marker="confirm-back").click()
    await eventually(lambda: elements_of(user, "channel-unsubscribe"), "The details after the back button")
    assert fake_starter.calls == []

    fake_starter.plan.append({"stderr": stderr_line({"channel_id": ALPHA, "subscribed": False})})
    with sessions.begin() as session:
        session.get(Channel, ALPHA).is_subscribed = False
    user.find(marker="channel-unsubscribe").click()
    await eventually(lambda: elements_of(user, "confirm-unsubscribe"), "The question before the unsubscribe")
    user.find(marker="confirm-unsubscribe").click()

    await user.should_see("Unsubscribed from alpha news.")
    assert commands(fake_starter) == [["unsubscribe", f"--channel-id={ALPHA}"]]
    await eventually(lambda: text_of(user, "channel-subscribed") == "No", "The new state in the dialog")


async def test_a_channel_without_rules_needs_no_note_about_rules_before_the_unsubscribe(user, stored_channels):
    await user.open("/subscriptions")
    await open_channel(user, CHARLIE)

    user.find(marker="channel-unsubscribe").click()

    await eventually(lambda: elements_of(user, "confirm-unsubscribe"), "The question before the unsubscribe")
    assert elements_of(user, "confirm-rules") == []


@pytest.mark.parametrize(
    ("child", "expected"),
    [
        (
            {"exit_code": EXIT_UNAVAILABLE, "stderr": "COOKIES_KEY is not set\n"},
            "The subscription did not change. The YouTube account is not available: COOKIES_KEY is not set",
        ),
        (
            {"exit_code": EXIT_LOGGED_OUT, "stderr": "YouTube refused the session with HTTP 401\n"},
            (
                "The subscription did not change. YouTube does not accept the stored cookies any more (YouTube refused "
                "the session with HTTP 401). Export new YouTube cookies from your browser and upload them to the service."
            ),
        ),
        (
            {"exit_code": EXIT_YOUTUBE_ERROR, "stderr": "YouTube did not confirm that it could subscribe\n"},
            (
                "The subscription did not change. YouTube did not give the expected answer: YouTube did not confirm that "
                "it could subscribe"
            ),
        ),
        (
            {"stderr": stderr_line({"channel_id": BRAVO, "subscribed": False})},
            (
                "The subscription did not change. The service could not finish the work: The subscription change process "
                "ended without the confirmation of the change."
            ),
        ),
    ],
)
async def test_a_failed_subscribe_shows_its_cause_in_the_dialog(user, stored_channels, fake_starter, child, expected):
    await user.open("/subscriptions")
    await open_channel(user, BRAVO)
    fake_starter.plan.append(child)

    user.find(marker="channel-subscribe").click()

    await eventually(lambda: elements_of(user, "channel-error"), "The cause of the failure")
    assert text_of(user, "channel-error") == expected
    assert text_of(user, "channel-subscribed") == "No"
    assert not user.notify.contains("Subscribed to Bravo Cooking.")


async def test_a_subscribe_during_a_run_starts_nothing_and_says_so(user, stored_channels, fake_starter):
    await user.open("/subscriptions")
    await open_channel(user, BRAVO)

    with sync_lock_held():
        user.find(marker="channel-subscribe").click()
        await eventually(lambda: elements_of(user, "channel-error"), "The busy text")

    assert text_of(user, "channel-error").startswith(f"The subscription did not change. {BUSY_TEXT}")
    assert fake_starter.calls == []


async def test_the_buttons_of_the_dialog_are_off_while_the_child_works(user, stored_channels, fake_starter):
    await user.open("/subscriptions")
    await open_channel(user, BRAVO)
    fake_starter.plan.append({"blocking": True, "stderr": stderr_line({"channel_id": BRAVO, "subscribed": True})})

    user.find(marker="channel-subscribe").click()

    await eventually(lambda: elements_of(user, "channel-working"), "The note that the service works")
    assert element_of(user, "channel-subscribe").enabled is False
    # A second click starts no second child.
    user.find(marker="channel-subscribe").click()
    await asyncio.sleep(SETTLE_SECONDS)
    assert len(fake_starter.calls) == 1
    fake_starter.children[0].finish(0)
    await user.should_see("Subscribed to Bravo Cooking.")
    await eventually(lambda: not elements_of(user, "channel-working"), "The end of the note")


# The translation.


async def test_each_text_of_the_subscriptions_page_goes_through_the_translation(
    nicegui_globals, settings, sessions, fake_starter, monkeypatch
):
    class Marking:
        def gettext(self, message):
            return f"<<{message}>>"

    monkeypatch.setattr(i18n, "_translations", Marking())
    store(
        sessions,
        watch_later(),
        channel(ALPHA, "Alpha title", "@alpha"),
        channel(BRAVO, "Bravo title", None, subscribed=False),
        rule(1, "Rule one", enabled=False),
    )
    store(sessions, RuleChannel(rule_id=1, channel_id=ALPHA))
    fake_starter.plan.append({"stderr": stderr_line([found(FOUND, "Found title", "@found", 1000)])})
    data = {"Alpha title", "Bravo title", "Found title", "@alpha", "@found", "1,000"}
    texts = []
    # The columns of each table, by the id of the table, because the test collects the texts of the page three times.
    columns = {}

    def collect() -> None:
        for element in user.client.elements.values():
            if isinstance(element, TextElement) and element.text:
                texts.append(element.text)
            if isinstance(element, LabelElement) and element.props.get("label"):
                texts.append(element.props["label"])
            if isinstance(element, ui.select):
                texts.extend(element.options.values() if isinstance(element.options, dict) else [])
            if isinstance(element, ui.table):
                columns[element.id] = [column["label"] for column in element.columns]
                for row in element.rows:
                    texts.extend(str(row[name]) for name in ("subscribed", "rules") if row.get(name))

    async with running_service(settings, fake_starter) as user:
        await user.open("/subscriptions")
        set_value(user, "search-input", "found")
        user.find(marker="search").click()
        await eventually(lambda: elements_of(user, "found-table"), "The found channels")
        await open_channel(user, ALPHA)
        collect()
        user.find(marker="channel-add-to-rule").click()
        await eventually(lambda: elements_of(user, "no-rule-choice"), "The add to rule view")
        collect()
        await open_channel(user, FOUND, "found-table")
        user.find(marker="channel-add-to-rule").click()
        await eventually(lambda: elements_of(user, "rule-select"), "The choice of a rule")
        collect()

    assert "<<Subscriptions>>" in texts
    assert "<<Lists refreshed: {time}>>" not in texts
    assert "<<The lists were never refreshed.>>" in texts
    column_labels = [label for labels in columns.values() for label in labels]
    assert len(column_labels) == 8
    assert [label for label in column_labels if not (label.startswith("<<") and label.endswith(">>"))] == []
    untranslated = [
        text
        for text in texts
        if not (text.startswith("<<") and text.endswith(">>")) and text not in data and not TIME.fullmatch(text)
    ]
    assert untranslated == []
