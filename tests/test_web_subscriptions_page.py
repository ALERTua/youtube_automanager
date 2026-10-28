"""Tests of the subscriptions page. A fake child stands in for each child process, so no test reads YouTube."""

import asyncio
import contextlib
import io
import json
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from nicegui import ui
from nicegui.elements.mixins.label_element import LabelElement
from nicegui.elements.mixins.text_element import TextElement
from pydantic import SecretStr
from sqlalchemy import select
from ui_support import (
    Held,
    collect_deleted_elements,
    element_of,
    elements_of,
    eventually,
    moment_of,
    running_service,
    set_value,
    store,
    text_of,
)
from yt_dlp import YoutubeDL

from youtube_automanager import i18n
from youtube_automanager.account_cache import REFRESHED_AT_KEY
from youtube_automanager.channel_actions import change_subscription_once
from youtube_automanager.db.models import (
    BreakerReason,
    Channel,
    Playlist,
    Rule,
    RuleChannel,
    Schedule,
    ScheduleKind,
    Setting,
)
from youtube_automanager.i18n.setting import NAMES
from youtube_automanager.pacing import RequestBudget
from youtube_automanager.request_pause import EXIT_PAUSED, RequestPause
from youtube_automanager.runner import sync_lock_held
from youtube_automanager.short_child import EXIT_LOGGED_OUT, EXIT_UNAVAILABLE, EXIT_YOUTUBE_ERROR
from youtube_automanager.web.ui import subscriptions_page
from youtube_automanager.web.ui.common import format_time, iso_time, template_text
from youtube_automanager.web.ui.dates import today_in
from youtube_automanager.youtube.session import CookieCipher, generate_key, save_cookies

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


def row_of(user, channel_id: str, marker: str = "channels-table") -> dict:
    return next(row for row in rows_of(user, marker) if row["id"] == channel_id)


# The fields of a row that its cells show. The other fields serve the cell of the actions.
SHOWN_FIELDS = ("id", "title", "url", "thumbnail", "handle", "subscribers", "subscribed", "rules")


def shown(row: dict) -> dict:
    return {name: value for name, value in row.items() if name in SHOWN_FIELDS}


def channel_ids(user) -> list[str]:
    return [row["id"] for row in rows_of(user, "channels-table")]


async def channels_are(user, expected: list[str]) -> None:
    await eventually(lambda: channel_ids(user) == expected, f"The table of the channels with {expected}")


def template_of(user, marker: str, slot: str) -> str:
    """Return the template of a cell slot. The user simulation does not render it, so a test reads it."""
    template = element_of(user, marker).slots[slot].template
    assert template is not None
    return template


def actions_of(user, marker: str = "channels-table") -> str:
    return template_of(user, marker, "body-cell-actions")


def subscribe_row(user, channel_id: str, marker: str = "channels-table") -> None:
    user.find(marker=marker).trigger("subscribeChannel", args=channel_id)


def unsubscribe_row(user, channel_id: str, marker: str = "channels-table") -> None:
    """Press Unsubscribe in the menu of the row: the template sends the event only from that button."""
    user.find(marker=marker).trigger("unsubscribeChannel", args=channel_id)


def add_row_to_rule(user, channel_id: str, rule_id: int, marker: str = "channels-table") -> None:
    user.find(marker=marker).trigger("addChannelToRule", args=[channel_id, rule_id])


def result_of(user) -> str:
    return text_of(user, "channel-result")


def choices_of(user, channel_id: str, marker: str = "channels-table") -> list[tuple[int, str, bool, bool]]:
    return [
        (choice["id"], choice["label"], choice["reads"], choice["feed"])
        for choice in row_of(user, channel_id, marker)["choices"]
    ]


# The cookies of the account for the real body of a child, and the text of a pause on the page.
PAUSE_COOKIES = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t4000000000\tSAPISID\tfake-sapisid\n"
PAUSE_TEMPLATE = (
    "YouTube limited the requests with the cookies of the account (HTTP 429, too many requests). The service sends "
    "no such request until {time}."
)


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
    assert [column["label"] for column in table.columns] == ["Channel", "Handle", "Subscribed", "Rules", "Actions"]
    assert [shown(row) for row in table.rows] == [
        {
            "id": ALPHA,
            "title": "alpha news",
            "url": f"https://www.youtube.com/channel/{ALPHA}",
            "thumbnail": "",
            "handle": "@AlphaNews",
            "subscribed": "Yes",
            "rules": "",
        },
        {
            "id": BRAVO,
            "title": "Bravo Cooking",
            "url": f"https://www.youtube.com/channel/{BRAVO}",
            "thumbnail": "",
            "handle": "",
            "subscribed": "No",
            "rules": "Science, Cooking (off)",
        },
        {
            "id": CHARLIE,
            "title": "Charlie",
            "url": f"https://www.youtube.com/channel/{CHARLIE}",
            "thumbnail": "",
            "handle": "@charlie_tv",
            "subscribed": "Yes",
            "rules": "",
        },
        {
            "id": DELTA,
            "title": "Delta Science",
            "url": f"https://www.youtube.com/channel/{DELTA}",
            "thumbnail": "",
            "handle": "@deltascience",
            "subscribed": "No",
            "rules": "Science",
        },
    ]
    assert text_of(user, "channels-summary") == "4 of 4 channels"
    await user.should_not_see(marker="no-channels")
    assert result_of(user) == ""


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
    # The browser puts its form of the time into the translated sentence.
    assert moment_of(user, "lists-refreshed") == ("2026-10-02T18:05:00+03:00", "Europe/Kyiv")
    assert element_of(user, "lists-refreshed").props["data-time-template"] == "Lists refreshed: {time}"
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
    assert [column["label"] for column in table.columns] == [
        "Channel",
        "Handle",
        "Subscribers",
        "Subscribed",
        "Actions",
    ]
    assert [shown(row) for row in table.rows] == [
        {
            "id": FOUND,
            "title": "Found Channel",
            "url": f"https://www.youtube.com/channel/{FOUND}",
            "thumbnail": "https://yt3.example/found.jpg",
            "handle": "@found",
            "subscribers": "6,320,000",
            "subscribed": "No",
        },
        {
            "id": CHARLIE,
            "title": "Charlie on YouTube",
            "url": f"https://www.youtube.com/channel/{CHARLIE}",
            "thumbnail": "",
            "handle": "@charlie_tv",
            "subscribers": "",
            "subscribed": "Yes",
        },
        {
            "id": BRAVO,
            "title": "Bravo",
            "url": f"https://www.youtube.com/channel/{BRAVO}",
            "thumbnail": "",
            "handle": "",
            "subscribers": "12",
            "subscribed": "No",
        },
    ]
    # The stored state decides the button of a found channel.
    assert [row["is_subscribed"] for row in table.rows] == [False, True, False]
    assert commands(fake_starter) == [["find-channels", "--query=cooking & co"]]
    # The page has two tables of rows, so the head script finds the button of a row by the scope of its table.
    assert table.props["data-row-scope"] == "found"
    assert element_of(user, "channels-table").props["data-row-scope"] == "channels"
    assert "'found-add-' + props.row.id" in actions_of(user, "found-table")
    assert "'channels-add-' + props.row.id" in actions_of(user)


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


# The actions of a row.


async def test_the_title_of_a_channel_is_a_link_to_youtube_and_a_row_opens_nothing(user, stored_channels, fake_starter):
    await user.open("/subscriptions")
    await search_for(user, fake_starter, found(FOUND, "Found Channel", "@found", 1, "https://yt3.example/f.jpg"))

    for marker in ("channels-table", "found-table"):
        table = element_of(user, marker)
        template = template_of(user, marker, "body-cell-title")
        # The avatar stays in front of the title, and the title opens the channel in a new tab.
        assert template.index("<q-avatar") < template.index('<a :href="props.row.url" target="_blank"')
        assert 'rel="noopener noreferrer"' in template
        # The click on the link does not reach the row.
        assert "@click.stop" in template
        # A click on a row opens nothing, and a row does not look like a button.
        events = {listener.type for listener in table._event_listeners.values()}
        assert {"subscribeChannel", "unsubscribeChannel", "addChannelToRule"} <= events
        assert {"rowClick", "rowButton"} & events == set()
        assert "cursor-pointer" not in table.classes
    assert row_of(user, FOUND, "found-table")["url"] == f"https://www.youtube.com/channel/{FOUND}"
    assert row_of(user, ALPHA)["url"] == f"https://www.youtube.com/channel/{ALPHA}"


async def test_the_cell_of_the_actions_has_a_button_for_each_action(user, stored_channels):
    await user.open("/subscriptions")

    template = actions_of(user)

    # Subscribe or Unsubscribe by the stored state, and both are off while a change runs.
    assert 'v-if="props.row.is_subscribed"' in template
    assert template.count(':disable="props.row.locked"') == 2
    assert template.count(':data-row-button="props.row.id"') == 2
    assert "$parent.$emit('subscribeChannel', props.row.id)" in template
    # Unsubscribe opens a menu with the question, and only its own Unsubscribe button sends the event. Cancel only
    # closes the menu.
    question = template.index("props.row.unsubscribe_question")
    assert template.index("<q-menu") < question < template.index("$parent.$emit('unsubscribeChannel', props.row.id)")
    cancel = next(line for line in template.splitlines() if 'label="Cancel"' in line)
    assert "v-close-popup" in cancel
    assert "$emit" not in cancel
    # The rules come from the row. A rule that reads the channel already cannot be chosen.
    assert 'v-for="choice in props.row.choices"' in template
    assert ':disable="choice.reads"' in template
    assert "$parent.$emit('addChannelToRule', props.row.id, choice.id)" in template
    assert "already reads" in template
    assert 'v-if="choice.feed"' in template
    assert (
        "This rule has no channels now, so it reads the subscriptions feed. After this change, it reads only its "
        "channels, and not the subscriptions feed." in template
    )
    assert (
        "The next run adds the videos of this channel from the last 7 days that the rule matches. After that, the "
        "rule adds each new video of the channel. To add older videos, open the rule and change Also add videos from."
        in template
    )
    assert "No rule exists yet. Create a rule first." in template
    # A row whose change runs shows a spinner and a note instead of the buttons.
    assert '<div v-if="props.row.working"' in template
    assert "<q-spinner" in template
    assert "The service works with YouTube. This takes up to a minute." in template
    # The texts of a channel and of a rule come as values of the row, and no such text runs in the template.
    assert "{{ props.row.unsubscribe_question }}" in template
    assert "{{ choice.label }}" in template


def test_a_text_of_the_cell_of_the_actions_cannot_run_code():
    assert template_text('{{ a }} <b> "c"') == "&#123;&#123; a }} &lt;b&gt; &quot;c&quot;"


async def test_the_fields_of_the_actions_follow_the_state_and_the_rules_of_a_channel(user, stored_channels, sessions):
    store(sessions, rule(3, "Feed rule"))
    await user.open("/subscriptions")

    bravo = row_of(user, BRAVO)
    assert (bravo["is_subscribed"], bravo["working"], bravo["locked"]) == (False, False, False)
    assert bravo["unsubscribe_question"] == "Unsubscribe the YouTube account from Bravo Cooking?"
    assert bravo["unsubscribe_note"] == "The rules that name this channel still read its videos."
    # Rules 1 and 2 read Bravo already. Rule 3 has no channels, so it reads the subscriptions feed.
    assert choices_of(user, BRAVO) == [
        (1, "Science", True, False),
        (2, "Cooking (off)", True, False),
        (3, "Feed rule", False, True),
    ]
    alpha = row_of(user, ALPHA)
    assert alpha["is_subscribed"] is True
    # A channel without rules needs no note about rules before the unsubscribe.
    assert alpha["unsubscribe_note"] == ""
    assert choices_of(user, ALPHA) == [
        (1, "Science", False, False),
        (2, "Cooking (off)", False, False),
        (3, "Feed rule", False, True),
    ]


async def test_without_rules_a_row_offers_no_rule(user, sessions):
    store(sessions, channel(ALPHA, "Alpha"))
    await user.open("/subscriptions")

    assert row_of(user, ALPHA)["choices"] == []
    assert '<q-item v-if="!props.row.choices.length">' in actions_of(user)


# Add to rule.


async def test_a_found_channel_goes_into_a_rule_with_a_new_channel_row(user, sessions, fake_starter):
    store(sessions, watch_later(), rule(1, "Science"), channel(ALPHA, "Alpha"))
    store(sessions, RuleChannel(rule_id=1, channel_id=ALPHA))
    await user.open("/subscriptions")
    await search_for(user, fake_starter, found(FOUND, "Found Channel", "@found", 99, "https://yt3.example/f.jpg"))
    # The rule has a channel, so it does not read the subscriptions feed.
    assert choices_of(user, FOUND, "found-table") == [(1, "Science", False, False)]

    add_row_to_rule(user, FOUND, 1, "found-table")

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
    assert result_of(user) == "Rule Science now reads Found Channel."
    await channels_are(user, [ALPHA, FOUND])
    assert rows_of(user, "channels-table")[1]["rules"] == "Science"
    # The found table reloads too, and the rule now reads the channel.
    await eventually(
        lambda: choices_of(user, FOUND, "found-table") == [(1, "Science", True, False)], "The rule in the found row"
    )
    # The page writes the database itself, and starts no child for it.
    assert commands(fake_starter) == [["find-channels", "--query=query"]]


async def test_a_rule_without_channels_tells_that_it_stops_to_read_the_feed(user, sessions, fake_starter):
    store(sessions, watch_later(), rule(1, "Feed rule"))
    await user.open("/subscriptions")
    await search_for(user, fake_starter, found(FOUND, "Found Channel"))
    assert choices_of(user, FOUND, "found-table") == [(1, "Feed rule", False, True)]

    add_row_to_rule(user, FOUND, 1, "found-table")

    await eventually(lambda: rule_links(sessions) == {(1, FOUND)}, "The link of the rule and the channel")
    await eventually(
        lambda: choices_of(user, FOUND, "found-table") == [(1, "Feed rule", True, False)], "The rule in the found row"
    )


async def test_a_found_channel_that_is_stored_keeps_its_row(user, stored_channels, sessions, fake_starter):
    await user.open("/subscriptions")
    await search_for(user, fake_starter, found(ALPHA, "A new title", "@new", 5, "https://yt3.example/new.jpg"))

    add_row_to_rule(user, ALPHA, 1, "found-table")

    await eventually(lambda: (1, ALPHA) in rule_links(sessions), "The link of the rule and the channel")
    row = stored_channel(sessions, ALPHA)
    assert (row.title, row.handle, row.thumbnail_url, row.is_subscribed) == ("alpha news", "@AlphaNews", None, True)
    await user.should_see("Rule Science now reads alpha news.")


async def test_a_stored_channel_goes_into_a_rule(user, stored_channels, sessions):
    store(sessions, rule(3, "Third"))
    await user.open("/subscriptions")

    add_row_to_rule(user, BRAVO, 3)

    await eventually(lambda: (3, BRAVO) in rule_links(sessions), "The link of the rule and the channel")
    await eventually(lambda: row_of(user, BRAVO)["rules"] == "Science, Cooking (off), Third", "The new rules")
    assert [reads for _id, _label, reads, _feed in choices_of(user, BRAVO)] == [True, True, True]
    assert "text-negative" not in element_of(user, "channel-result").classes


async def test_a_rule_that_reads_the_channel_already_adds_nothing(user, stored_channels, sessions):
    await user.open("/subscriptions")

    # The menu offers no such rule, but a page that is older than the database can send it.
    add_row_to_rule(user, BRAVO, 1)

    await eventually(lambda: result_of(user) == "Rule Science already reads Bravo Cooking.", "The result")
    await user.should_see("Rule Science already reads Bravo Cooking.")


async def test_a_disabled_rule_is_offered_with_its_mark(user, sessions):
    store(sessions, watch_later(), rule(1, "Paused rule", enabled=False), channel(ALPHA, "Alpha"))

    await user.open("/subscriptions")

    assert choices_of(user, ALPHA) == [(1, "Paused rule (off)", False, True)]


async def test_a_rule_that_was_deleted_after_the_page_showed_it_adds_nothing(user, stored_channels, sessions):
    await user.open("/subscriptions")

    with sessions.begin() as session:
        session.delete(session.get(Rule, 1))
    add_row_to_rule(user, ALPHA, 1)

    await user.should_see("Rule Science does not exist any more.")
    assert result_of(user) == "Rule Science does not exist any more."
    assert "text-negative" in element_of(user, "channel-result").classes
    assert {link for link in rule_links(sessions) if link[1] == ALPHA} == set()
    await eventually(lambda: [choice[0] for choice in choices_of(user, ALPHA)] == [2], "The fresh rules")


async def test_an_unknown_channel_or_rule_adds_nothing(user, stored_channels, sessions):
    await user.open("/subscriptions")
    links = rule_links(sessions)

    add_row_to_rule(user, FOUND, 1)
    add_row_to_rule(user, ALPHA, 99)
    subscribe_row(user, FOUND)

    await asyncio.sleep(SETTLE_SECONDS)
    assert rule_links(sessions) == links
    assert result_of(user) == ""


# Subscribe and unsubscribe.


async def test_subscribe_runs_the_child_and_shows_the_new_state(user, stored_channels, sessions, fake_starter):
    await user.open("/subscriptions")
    fake_starter.plan.append({"stderr": stderr_line({"channel_id": BRAVO, "subscribed": True})})
    # The child writes the database. The fake child does not, so the test writes what the child would write.
    with sessions.begin() as session:
        session.get(Channel, BRAVO).is_subscribed = True

    subscribe_row(user, BRAVO)

    await user.should_see("Subscribed to Bravo Cooking.")
    assert commands(fake_starter) == [["subscribe", f"--channel-id={BRAVO}", "--title=Bravo Cooking"]]
    assert result_of(user) == "Subscribed to Bravo Cooking."
    assert "text-negative" not in element_of(user, "channel-result").classes
    await eventually(lambda: row_of(user, BRAVO)["subscribed"] == "Yes", "The new state in the table")
    assert row_of(user, BRAVO)["is_subscribed"] is True


async def test_subscribe_to_a_found_channel_gives_its_title_to_the_child(user, fake_starter):
    await user.open("/subscriptions")
    await search_for(user, fake_starter, found(FOUND, "Found Channel", "@found"))
    fake_starter.plan.append({"stderr": stderr_line({"channel_id": FOUND, "subscribed": True})})

    subscribe_row(user, FOUND, "found-table")

    await user.should_see("Subscribed to Found Channel.")
    assert commands(fake_starter)[-1] == ["subscribe", f"--channel-id={FOUND}", "--title=Found Channel"]


async def test_unsubscribe_runs_the_child_and_shows_the_new_state(user, stored_channels, sessions, fake_starter):
    await user.open("/subscriptions")
    fake_starter.plan.append({"stderr": stderr_line({"channel_id": ALPHA, "subscribed": False})})
    with sessions.begin() as session:
        session.get(Channel, ALPHA).is_subscribed = False

    unsubscribe_row(user, ALPHA)

    await user.should_see("Unsubscribed from alpha news.")
    assert commands(fake_starter) == [["unsubscribe", f"--channel-id={ALPHA}"]]
    assert result_of(user) == "Unsubscribed from alpha news."
    await eventually(lambda: row_of(user, ALPHA)["is_subscribed"] is False, "The new state in the table")
    assert row_of(user, ALPHA)["subscribed"] == "No"


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
async def test_a_failed_subscribe_shows_its_cause_above_the_table(user, stored_channels, fake_starter, child, expected):
    await user.open("/subscriptions")
    fake_starter.plan.append(child)

    subscribe_row(user, BRAVO)

    await eventually(lambda: result_of(user) == expected, "The cause of the failure")
    assert "text-negative" in element_of(user, "channel-result").classes
    assert row_of(user, BRAVO)["subscribed"] == "No"
    assert not user.notify.contains("Subscribed to Bravo Cooking.")


async def test_a_subscribe_during_the_pause_of_the_requests_shows_the_end_of_the_pause(
    user, settings, sessions, stored_channels, fake_starter, monkeypatch
):
    """From the real body of the child to the text above the table: the browser writes the end of the pause."""
    key = generate_key()
    with sessions.begin() as session:
        save_cookies(session, CookieCipher(key), PAUSE_COOKIES)
    paused = RequestBudget(sessions, send=lambda notification: None).open_breaker(BreakerReason.HTTP_429)
    requests = []
    monkeypatch.setattr(YoutubeDL, "urlopen", lambda ydl, request: requests.append(request.url))
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        child_settings = settings.model_copy(update={"cookies_key": SecretStr(key)})
        code = change_subscription_once(child_settings, BRAVO, subscribe=True)
    assert requests == []
    await user.open("/subscriptions")
    fake_starter.plan.append({"exit_code": code, "stderr": stderr.getvalue()})

    subscribe_row(user, BRAVO)

    template = "The subscription did not change. " + PAUSE_TEMPLATE
    zone = ZoneInfo(settings.tz)
    expected = template.format(time=format_time(paused.pause.until, zone))
    await eventually(lambda: result_of(user) == expected, "The cause of the failure")
    assert moment_of(user, "channel-result") == (iso_time(paused.pause.until, zone), settings.tz)
    assert element_of(user, "channel-result").props["data-time-template"] == template
    assert row_of(user, BRAVO)["subscribed"] == "No"


async def test_the_next_result_forgets_the_end_of_the_pause(user, stored_channels, sessions, fake_starter):
    pause = RequestPause(BreakerReason.BOT_CHECK, datetime(2026, 10, 28, 13, 0, tzinfo=UTC), "Europe/Kyiv")
    await user.open("/subscriptions")
    fake_starter.plan.append({"exit_code": EXIT_PAUSED, "stderr": pause.to_line() + "\n"})
    subscribe_row(user, BRAVO)
    await eventually(lambda: moment_of(user, "channel-result")[0] is not None, "The end of the pause")

    add_row_to_rule(user, ALPHA, 1)

    await eventually(lambda: result_of(user) == "Rule Science now reads alpha news.", "The next result")
    assert moment_of(user, "channel-result") == (None, None)
    assert "text-negative" not in element_of(user, "channel-result").classes


async def test_a_refresh_of_the_lists_during_the_pause_shows_the_end_of_the_pause(user, fake_starter):
    pause = RequestPause(BreakerReason.BOT_CHECK, datetime(2026, 10, 28, 13, 0, tzinfo=UTC), "Europe/Kyiv")
    await user.open("/subscriptions")
    fake_starter.plan.append({"exit_code": EXIT_PAUSED, "stderr": pause.to_line() + "\n"})

    user.find(marker="refresh-lists").click()

    template = (
        "The lists did not refresh. YouTube limited the requests with the cookies of the account (a bot check). The "
        "service sends no such request until {time}."
    )
    expected = template.format(time="2026-10-28 15:00 EET")
    await eventually(lambda: text_of(user, "refresh-lists-result") == expected, "The pause of the failed refresh")
    assert moment_of(user, "refresh-lists-result") == ("2026-10-28T15:00:00+02:00", "Europe/Kyiv")
    assert element_of(user, "refresh-lists-result").props["data-time-template"] == template


async def test_a_subscribe_during_a_run_starts_nothing_and_says_so(user, stored_channels, fake_starter):
    await user.open("/subscriptions")

    with sync_lock_held():
        subscribe_row(user, BRAVO)
        await eventually(lambda: result_of(user), "The busy text")

    assert result_of(user).startswith(f"The subscription did not change. {BUSY_TEXT}")
    assert fake_starter.calls == []


async def test_the_row_of_a_running_change_is_busy_and_the_other_buttons_are_off(user, stored_channels, fake_starter):
    await user.open("/subscriptions")
    await search_for(user, fake_starter, found(FOUND, "Found Channel"))
    fake_starter.plan.append({"blocking": True, "stderr": stderr_line({"channel_id": BRAVO, "subscribed": True})})

    subscribe_row(user, BRAVO)

    await eventually(lambda: row_of(user, BRAVO)["working"], "The busy row")
    assert [row["id"] for row in rows_of(user, "channels-table") if row["working"]] == [BRAVO]
    assert all(row["locked"] for row in rows_of(user, "channels-table"))
    await eventually(lambda: row_of(user, FOUND, "found-table")["locked"], "The found row with its buttons off")
    assert row_of(user, FOUND, "found-table")["working"] is False
    # A second action starts no second child: the sync lock lets one child run at a time.
    subscribe_row(user, FOUND, "found-table")
    unsubscribe_row(user, ALPHA)
    await asyncio.sleep(SETTLE_SECONDS)
    assert len(fake_starter.calls) == 2
    fake_starter.children[-1].finish(0)
    await user.should_see("Subscribed to Bravo Cooking.")
    await eventually(lambda: not row_of(user, BRAVO)["working"], "The end of the busy row")
    assert not any(row["locked"] for row in rows_of(user, "channels-table"))
    await eventually(lambda: not row_of(user, FOUND, "found-table")["locked"], "The found row with its buttons on")


@pytest.mark.parametrize("subscribe", [True, False], ids=["subscribe", "unsubscribe"])
async def test_the_result_shows_when_the_found_table_redraws_itself_while_the_child_works(
    user, stored_channels, sessions, fake_starter, subscribe
):
    channel_id, title = (BRAVO, "Bravo Cooking") if subscribe else (ALPHA, "alpha news")
    message = f"Subscribed to {title}." if subscribe else f"Unsubscribed from {title}."
    await user.open("/subscriptions")
    await search_for(user, fake_starter, found(channel_id, "A found title"))
    fake_starter.plan.append(
        {"blocking": True, "stderr": stderr_line({"channel_id": channel_id, "subscribed": subscribe})}
    )
    # The child writes the database. The fake child does not, so the test writes what the child would write.
    with sessions.begin() as session:
        session.get(Channel, channel_id).is_subscribed = subscribe

    # The busy row redraws the found table and deletes the table of this handler.
    (subscribe_row if subscribe else unsubscribe_row)(user, channel_id, "found-table")

    await eventually(lambda: row_of(user, channel_id, "found-table")["working"], "The busy row")
    await collect_deleted_elements()
    fake_starter.children[-1].finish(0)
    await user.should_see(message)
    assert result_of(user) == message
    new_state = "Yes" if subscribe else "No"
    await eventually(lambda: row_of(user, channel_id, "found-table")["subscribed"] == new_state, "The found row")
    assert row_of(user, channel_id, "found-table")["working"] is False
    assert row_of(user, channel_id)["subscribed"] == new_state


async def test_the_result_of_an_add_to_a_rule_shows_when_the_found_table_redraws_itself_while_the_save_runs(
    user, stored_channels, sessions, fake_starter, monkeypatch
):
    save = Held(subscriptions_page.add_to_rule)
    monkeypatch.setattr(subscriptions_page, "add_to_rule", save)
    await user.open("/subscriptions")
    await search_for(user, fake_starter, found(ALPHA, "A found title"))
    add_row_to_rule(user, ALPHA, 2, "found-table")
    await eventually(save.entered.is_set, "The start of the save")

    # A new search redraws the found table and deletes the table of the save, which still waits for the database.
    await search_for(user, fake_starter, found(FOUND, "Found Channel"))
    await collect_deleted_elements()
    save.gate.set()

    await user.should_see("Rule Cooking now reads alpha news.")
    assert (2, ALPHA) in rule_links(sessions)
    await eventually(lambda: rows_of(user, "channels-table")[0]["rules"].startswith("Cooking"), "The table reloads")


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
    # The columns of each table, by the id of the table.
    columns = {}
    cells = set()

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
                cells.update(slot.template for name, slot in element.slots.items() if name == "body-cell-actions")
                for row in element.rows:
                    names = ("subscribed", "rules", "unsubscribe_question", "unsubscribe_note")
                    texts.extend(str(row[name]) for name in names if row.get(name))
                    texts.extend(choice["label"] for choice in row["choices"])

    async with running_service(settings, fake_starter) as user:
        await user.open("/subscriptions")
        set_value(user, "search-input", "found")
        user.find(marker="search").click()
        await eventually(lambda: elements_of(user, "found-table"), "The found channels")
        add_row_to_rule(user, BRAVO, 1)
        await eventually(lambda: result_of(user), "The result of the add to a rule")
        await eventually(lambda: row_of(user, BRAVO)["unsubscribe_note"], "The rows after the add")
        collect()

    assert "<<Subscriptions>>" in texts
    assert "<<Lists refreshed: {time}>>" not in texts
    assert "<<The lists were never refreshed.>>" in texts
    assert "<<Rule Rule one now reads Bravo title.>>" in texts
    assert "<<Rule one (off)>>" in texts
    column_labels = [label for labels in columns.values() for label in labels]
    assert len(column_labels) == 10
    assert [label for label in column_labels if not (label.startswith("<<") and label.endswith(">>"))] == []
    # Both tables have the same texts in the cell of their actions, and each such text is marked.
    assert len(cells) == 2
    english = (
        "Subscribe",
        "Unsubscribe",
        "Cancel",
        "Add to rule",
        "Add the channel",
        "already reads",
        "The service works",
        "No rule exists",
        "This rule has no channels",
        "The next run adds",
    )
    for template in cells:
        assert "&lt;&lt;Add to rule&gt;&gt;" in template
        unmarked = re.sub(r"&lt;&lt;.*?&gt;&gt;", "", template, flags=re.DOTALL)
        assert [text for text in english if text in unmarked] == []
    untranslated = [
        text
        for text in texts
        if not (text.startswith("<<") and text.endswith(">>"))
        and text not in data
        and text not in NAMES.values()
        and not TIME.fullmatch(text)
    ]
    assert untranslated == []
