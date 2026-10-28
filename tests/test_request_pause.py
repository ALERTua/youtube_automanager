"""Tests of the pause of the requests with cookies as a child gives it to the web process."""

import datetime
import json

import pytest

from youtube_automanager.db.models import BreakerReason
from youtube_automanager.i18n import set_language
from youtube_automanager.request_pause import EXIT_PAUSED, RequestPause
from youtube_automanager.short_child import ChildPaused, ChildYouTubeError
from youtube_automanager.web.ui.account_lists import child_error_text, child_pause

UNTIL = datetime.datetime(2026, 10, 28, 13, 0, tzinfo=datetime.UTC)


def test_the_line_holds_the_reason_the_end_in_utc_and_the_time_zone():
    in_kyiv = UNTIL.astimezone(datetime.timezone(datetime.timedelta(hours=2)))

    line = RequestPause(BreakerReason.BOT_CHECK, in_kyiv, "Europe/Kyiv").to_line()

    assert json.loads(line) == {"reason": "bot_check", "until": "2026-10-28T13:00:00+00:00", "zone": "Europe/Kyiv"}
    assert "\n" not in line


@pytest.mark.parametrize("reason", list(BreakerReason))
def test_a_line_gives_the_same_pause_back(reason):
    pause = RequestPause(reason, UNTIL, "America/New_York")

    assert RequestPause.from_line(pause.to_line()) == pause


@pytest.mark.parametrize(
    "line",
    [
        None,
        "",
        "The session check failed",
        "[]",
        '"text"',
        '{"until": "2026-10-28T13:00:00+00:00", "zone": "UTC"}',
        '{"reason": "captcha", "until": "2026-10-28T13:00:00+00:00", "zone": "UTC"}',
        '{"reason": "http_429", "until": "tomorrow", "zone": "UTC"}',
        '{"reason": "http_429", "until": 5, "zone": "UTC"}',
        '{"reason": "http_429", "until": "2026-10-28T13:00:00", "zone": "UTC"}',
        '{"reason": "http_429", "until": "2026-10-28T13:00:00+00:00", "zone": "Mars/Base"}',
        '{"reason": "http_429", "until": "2026-10-28T13:00:00+00:00", "zone": "Europe/Kyiv?"}',
        '{"reason": "http_429", "until": "2026-10-28T13:00:00+00:00", "zone": 5}',
        '{"reason": "http_429", "until": "2026-10-28T13:00:00+00:00"}',
    ],
    ids=[
        "none",
        "empty",
        "text",
        "list",
        "string",
        "no reason",
        "unknown reason",
        "no time",
        "number as time",
        "time without zone",
        "unknown zone",
        "zone that is no file name",
        "number as zone",
        "no zone",
    ],
)
def test_a_line_that_is_not_a_pause_gives_none(line):
    assert RequestPause.from_line(line) is None


def test_the_text_names_the_end_in_the_time_zone_of_the_service_and_the_reason():
    pause = RequestPause(BreakerReason.HTTP_429, UNTIL, "Europe/Kyiv")

    assert str(pause) == (
        "The service sends no request with the cookies of the account until 2026-10-28 15:00 EET, because YouTube "
        "answered a request with HTTP 429 (too many requests)."
    )


def test_the_exit_code_of_a_pause_differs_from_the_codes_of_the_other_failures():
    from youtube_automanager.short_child import EXIT_LOGGED_OUT, EXIT_UNAVAILABLE, EXIT_YOUTUBE_ERROR

    assert EXIT_PAUSED not in {0, 1, 2, EXIT_UNAVAILABLE, EXIT_LOGGED_OUT, EXIT_YOUTUBE_ERROR}


@pytest.mark.parametrize(
    ("reason", "english", "ukrainian"),
    [
        (BreakerReason.HTTP_429, "HTTP 429, too many requests", "HTTP 429, забагато запитів"),
        (BreakerReason.TRY_AGAIN_LATER, 'a "try again later" page', "сторінка «спробуйте пізніше»"),
        (BreakerReason.BOT_CHECK, "a bot check", "перевірка на бота"),
    ],
)
def test_the_page_text_of_a_pause_names_its_reason_and_keeps_the_end_for_the_browser(
    real_uk_catalog, reason, english, ukrainian
):
    error = ChildPaused(RequestPause(reason, UNTIL, "UTC"))

    assert child_error_text(error) == (
        f"YouTube limited the requests with the cookies of the account ({english}). The service sends no such request "
        "until {time}."
    )
    set_language("uk")
    assert child_error_text(error) == (
        f"YouTube обмежив запити з cookies акаунта ({ukrainian}). Сервіс не надсилає таких запитів до {{time}}."
    )


def test_only_a_child_that_stopped_at_a_pause_gives_a_pause():
    pause = RequestPause(BreakerReason.HTTP_429, UNTIL, "UTC")

    assert child_pause(ChildPaused(pause)) == pause
    assert child_pause(ChildYouTubeError("HTTP Error 503")) is None
    assert child_pause(None) is None
