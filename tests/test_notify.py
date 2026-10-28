import logging
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import apprise
import pytest
from apprise.decorators import notify as apprise_notify
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager import notify
from youtube_automanager.db.models import NotificationTarget
from youtube_automanager.notify import (
    DEFAULT_EVENTS,
    EVENT_LEVELS,
    AddedVideo,
    ChannelFailure,
    Event,
    Level,
    Notification,
    Notifier,
    SendReport,
    Target,
)

SECRET = "s3cr3tT0ken"
SENT = []


def _record(schema, body, title, notify_type, meta):
    SENT.append({"schema": schema, "host": meta.get("host"), "title": title, "body": body, "type": notify_type})


@apprise_notify(on="testcapture")
def _capture(body, title, notify_type, *args, meta, **kwargs):
    _record("testcapture", body, title, notify_type, meta)
    return True


@apprise_notify(on="testfalse")
def _refuse(body, title, notify_type, *args, meta, **kwargs):
    _record("testfalse", body, title, notify_type, meta)
    return False


@apprise_notify(on="testraise")
def _raise(body, title, notify_type, *args, meta, **kwargs):
    _record("testraise", body, title, notify_type, meta)
    raise RuntimeError(f"the plugin broke on {meta['url']}")


class _AllRecords(logging.Handler):
    def __init__(self):
        super().__init__(level=1)
        self.lines = []

    def emit(self, record):
        self.lines.append(f"{record.name}: {logging.Formatter().format(record)}")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fails the test when any code opens a network connection."""
    attempts = []

    def refuse(self, address, *args, **kwargs):
        attempts.append(address)
        raise OSError("network access in a test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    yield
    assert attempts == []


@pytest.fixture
def sent():
    SENT.clear()
    yield SENT
    SENT.clear()


@pytest.fixture
def all_logs():
    root = logging.getLogger()
    handler = _AllRecords()
    level = root.level
    root.addHandler(handler)
    root.setLevel(1)
    yield handler.lines
    root.removeHandler(handler)
    root.setLevel(level)


def _videos(count):
    return [AddedVideo(f"Video {i}", "Watch later", f"https://www.youtube.com/watch?v=v{i:03}") for i in range(count)]


def test_each_event_has_its_level():
    assert EVENT_LEVELS == {
        Event.COOKIE_SESSION_DEAD: Level.FAILURE,
        Event.COOKIE_EXPIRY_WARNING: Level.WARNING,
        Event.RUN_SUMMARY: Level.SUCCESS,
        Event.RUN_FAILED: Level.FAILURE,
        Event.CHANNEL_ERROR: Level.WARNING,
        Event.YTDLP_UPDATE_RESULT: Level.INFO,
        Event.VIDEO_ADDED: Level.INFO,
        Event.SCHEDULER_STATE: Level.INFO,
        Event.REQUEST_BREAKER: Level.WARNING,
    }


def test_default_events_leave_out_video_added_and_scheduler_state():
    assert DEFAULT_EVENTS == set(Event) - {Event.VIDEO_ADDED, Event.SCHEDULER_STATE}


def test_a_notification_takes_its_level_from_the_event():
    assert Notification(Event.RUN_FAILED, "t", "b").level is Level.FAILURE
    assert Notification(Event.VIDEO_ADDED, "t", "b").level is Level.INFO


@pytest.mark.parametrize(
    ("level", "notify_type"),
    [
        (Level.INFO, apprise.NotifyType.INFO),
        (Level.SUCCESS, apprise.NotifyType.SUCCESS),
        (Level.WARNING, apprise.NotifyType.WARNING),
        (Level.FAILURE, apprise.NotifyType.FAILURE),
    ],
)
def test_each_level_maps_to_the_apprise_type_of_the_same_name(level, notify_type, sent):
    event = next(event for event, event_level in EVENT_LEVELS.items() if event_level is level)
    target = Target("all", "testcapture://all", events=frozenset(Event))

    report = Notifier([target]).send(Notification(event, "title", "body"))

    assert level.notify_type is notify_type
    assert report.succeeded == ("all",)
    assert [message["type"] for message in sent] == [notify_type.value]


def test_the_target_gets_the_title_and_the_body_unchanged(sent):
    notification = Notification(Event.RUN_FAILED, "Title <&>", "line 1\nline 2 <&>")

    Notifier([Target("t", "testcapture://t")]).send(notification)

    assert [(message["title"], message["body"]) for message in sent] == [("Title <&>", "line 1\nline 2 <&>")]


def test_a_target_gets_only_the_events_in_its_set(sent):
    notifier = Notifier([Target("failures", "testcapture://failures", events=frozenset({Event.RUN_FAILED}))])

    failed = notifier.send(notify.run_failed("boom"))
    summary = notifier.send(notify.run_summary(_videos(1), []))

    assert failed == SendReport(succeeded=("failures",))
    assert summary == SendReport(skipped=("failures",))
    assert [message["title"] for message in sent] == [notify.run_failed("boom").title]


@pytest.mark.parametrize("event", list(Event))
def test_a_target_gets_only_the_events_at_or_above_its_minimum_level(event, sent):
    at_least_warning = {
        Event.COOKIE_SESSION_DEAD,
        Event.COOKIE_EXPIRY_WARNING,
        Event.RUN_FAILED,
        Event.CHANNEL_ERROR,
        Event.REQUEST_BREAKER,
    }
    target = Target("t", "testcapture://t", min_level=Level.WARNING, events=frozenset(Event))

    report = Notifier([target]).send(Notification(event, "title", "body"))

    if event in at_least_warning:
        assert report == SendReport(succeeded=("t",))
        assert len(sent) == 1
    else:
        assert report == SendReport(skipped=("t",))
        assert sent == []


def test_a_disabled_target_gets_nothing(sent):
    target = Target("off", "testcapture://off", enabled=False, events=frozenset(Event))

    report = Notifier([target]).send(notify.cookie_session_dead())

    assert report == SendReport(skipped=("off",))
    assert sent == []


def test_a_new_target_gets_both_cookie_events_and_not_the_events_that_are_off_by_default(sent):
    notifier = Notifier([Target("new", "testcapture://new")])
    expires_at = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)

    assert notifier.send(notify.cookie_session_dead()).succeeded == ("new",)
    assert notifier.send(notify.cookie_expiry(3, expires_at)).succeeded == ("new",)
    assert notifier.send(notify.video_added("v", "p", "https://www.youtube.com/watch?v=x")).skipped == ("new",)
    assert notifier.send(notify.scheduler_state(paused=True)).skipped == ("new",)
    assert len(sent) == 2


def test_a_failed_or_raising_target_does_not_stop_the_others(sent):
    notifier = Notifier(
        [
            Target("false", "testfalse://false"),
            Target("raise", "testraise://raise"),
            Target("unknown", "notaschema://unknown"),
            Target("ok", "testcapture://ok"),
            Target("off", "testcapture://off", enabled=False),
        ]
    )

    report = notifier.send(notify.run_failed("boom"))

    assert report == SendReport(succeeded=("ok",), failed=("false", "raise", "unknown"), skipped=("off",))
    assert [message["schema"] for message in sent] == ["testfalse", "testraise", "testcapture"]


def test_an_exception_of_apprise_for_one_target_does_not_stop_the_others(monkeypatch, sent):
    add = apprise.Apprise.add

    def add_that_breaks(self, services, *args, **kwargs):
        if services == "testcapture://broken":
            raise RuntimeError("apprise broke")
        return add(self, services, *args, **kwargs)

    monkeypatch.setattr(apprise.Apprise, "add", add_that_breaks)
    notifier = Notifier([Target("broken", "testcapture://broken"), Target("ok", "testcapture://ok")])

    report = notifier.send(notify.run_failed("boom"))

    assert report == SendReport(succeeded=("ok",), failed=("broken",))
    assert [message["host"] for message in sent] == ["ok"]


HELD = threading.Event()


@apprise_notify(on="testhang")
def _hang(body, title, notify_type, *args, meta, **kwargs):
    _record("testhang", body, title, notify_type, meta)
    HELD.wait(10)
    return True


def test_a_target_that_does_not_answer_in_time_fails_and_the_others_still_get_the_message(monkeypatch, all_logs, sent):
    monkeypatch.setattr(notify, "DELIVER_TIMEOUT_S", 0.2)
    HELD.clear()
    notifier = Notifier([Target("slow", "testhang://slow"), Target("ok", "testcapture://ok")])

    started = time.monotonic()
    try:
        report = notifier.send(notify.run_failed("boom"))
        elapsed = time.monotonic() - started
    finally:
        HELD.set()

    assert report == SendReport(succeeded=("ok",), failed=("slow",))
    assert elapsed < 5
    assert [message["host"] for message in sent] == ["slow", "ok"]
    assert any("Cannot send run_failed to target 'slow': no result after 0.2 s" in line for line in all_logs)


def test_send_test_reaches_a_disabled_target(sent):
    target = Target("off", "testcapture://off", enabled=False, events=frozenset())

    assert Notifier.send_test(target) is True
    assert [message["host"] for message in sent] == ["off"]
    assert "off" in sent[0]["body"]


@pytest.mark.parametrize("url", ["testfalse://x", "testraise://x", "notaschema://x"])
def test_send_test_reports_a_failed_target(url, sent):
    assert Notifier.send_test(Target("bad", url)) is False


@pytest.mark.parametrize("url", ["testcapture://anything", "json://localhost/path", "workflow://example.com/abc/def"])
def test_is_valid_url_accepts_a_url_that_apprise_can_use(url):
    assert Notifier.is_valid_url(url) is True


@pytest.mark.parametrize(
    "url", ["", "testcapture", "notaschema://x", "gotify://", "https://example.com/x", "workflow://127.0.0.1/bad!!/sig"]
)
def test_is_valid_url_refuses_a_url_that_apprise_cannot_use(url):
    assert Notifier.is_valid_url(url) is False


def test_no_log_line_holds_a_part_of_a_target_url(monkeypatch, all_logs, sent):
    add = apprise.Apprise.add

    def add_that_breaks(self, services, *args, **kwargs):
        if services.startswith("testcapture://crash"):
            raise RuntimeError(f"apprise broke on {services}")
        return add(self, services, *args, **kwargs)

    monkeypatch.setattr(apprise.Apprise, "add", add_that_breaks)
    targets = [
        Target("capture", f"testcapture://user{SECRET}:pass{SECRET}@host{SECRET}/path{SECRET}?token={SECRET}"),
        Target("false", f"testfalse://host{SECRET}/path{SECRET}"),
        Target("raise", f"testraise://user{SECRET}:pass{SECRET}@host{SECRET}/path{SECRET}"),
        Target("refused", f"workflow://127.0.0.1/bad!!{SECRET}/sig{SECRET}"),
        Target("crash", f"testcapture://crash/{SECRET}"),
    ]

    report = Notifier(targets).send(notify.run_failed("boom"))
    Notifier.send_test(targets[0])
    Notifier.is_valid_url(targets[3].url)

    assert report == SendReport(succeeded=("capture",), failed=("false", "raise", "refused", "crash"))
    for name in ("capture", "false", "raise", "refused", "crash"):
        assert any(f"'{name}'" in line for line in all_logs), name
    assert [line for line in all_logs if SECRET in line] == []


def test_stored_targets_read_the_table(db_sessions):
    with db_sessions.begin() as session:
        session.add_all(
            [
                NotificationTarget(
                    name="phone",
                    url="testcapture://phone",
                    min_level=Level.WARNING,
                    events=["run_failed", "no_such_event"],
                    enabled=True,
                ),
                NotificationTarget(name="loud", url="testcapture://loud", min_level=20, events=[], enabled=False),
                NotificationTarget(
                    name="all", url="testcapture://all", min_level=-3, events=["run_summary"], enabled=True
                ),
            ]
        )

    with db_sessions() as session:
        targets = notify.stored_targets(session)

    assert targets == [
        Target("phone", "testcapture://phone", Level.WARNING, frozenset({Event.RUN_FAILED}), True),
        Target("loud", "testcapture://loud", Level.FAILURE, frozenset(), False),
        Target("all", "testcapture://all", Level.INFO, frozenset({Event.RUN_SUMMARY}), True),
    ]


def test_send_to_stored_targets_sends_to_each_target_that_takes_the_event(db_sessions, sent):
    with db_sessions.begin() as session:
        session.add_all(
            [
                NotificationTarget(name="a", url="testcapture://a", min_level=0, events=["run_failed"], enabled=True),
                NotificationTarget(name="b", url="testcapture://b", min_level=0, events=["run_summary"], enabled=True),
            ]
        )

    report = notify.send_to_stored_targets(db_sessions, notify.run_failed("boom"))

    assert report == SendReport(succeeded=("a",), skipped=("b",))
    assert [message["host"] for message in sent] == ["a"]


def test_send_to_stored_targets_never_raises(all_logs):
    def broken_sessions():
        raise RuntimeError(f"cannot connect to postgresql://user:{SECRET}@db")

    # The function stands in for a session factory whose database is gone.
    sessions = cast("sessionmaker[Session]", broken_sessions)

    assert notify.send_to_stored_targets(sessions, notify.run_failed("boom")) is None
    assert any("Cannot send run_failed: RuntimeError" in line for line in all_logs)
    assert [line for line in all_logs if SECRET in line] == []


def test_the_repr_of_a_target_hides_its_url():
    assert SECRET not in repr(Target("t", f"testcapture://{SECRET}"))


@pytest.mark.parametrize(
    ("notification", "event"),
    [
        (notify.run_summary(_videos(2), ["an error"]), Event.RUN_SUMMARY),
        (notify.run_failed("boom"), Event.RUN_FAILED),
        (notify.cookie_session_dead(), Event.COOKIE_SESSION_DEAD),
        (notify.cookie_expiry(1, datetime(2026, 10, 12, tzinfo=UTC)), Event.COOKIE_EXPIRY_WARNING),
        (notify.channel_error([ChannelFailure("Chan", "HTTP Error 404")]), Event.CHANNEL_ERROR),
        (notify.ytdlp_update("2026.09.01", "2026.10.01", ok=True), Event.YTDLP_UPDATE_RESULT),
        (notify.ytdlp_update("2026.09.01", "2026.10.01", ok=False, error_text="no space"), Event.YTDLP_UPDATE_RESULT),
        (notify.video_added("Video", "Later", "https://www.youtube.com/watch?v=x"), Event.VIDEO_ADDED),
        (notify.scheduler_state(paused=True), Event.SCHEDULER_STATE),
        (notify.scheduler_state(paused=False), Event.SCHEDULER_STATE),
        (
            notify.request_breaker_opened("HTTP 429", timedelta(hours=1), datetime(2026, 10, 12, tzinfo=UTC)),
            Event.REQUEST_BREAKER,
        ),
        (notify.request_breaker_closed(), Event.REQUEST_BREAKER),
    ],
)
def test_each_builder_gives_its_event(notification, event):
    assert notification.event is event
    assert notification.title
    assert notification.body


def test_a_channel_error_of_one_channel_names_the_channel_in_the_title():
    notification = notify.channel_error([ChannelFailure("Chan", "HTTP Error 404")])

    assert notification.title == "YouTube AutoManager: cannot read the channel Chan"
    assert notification.body == (
        "yt-dlp failed to read 1 channel. The run continued with the other channels.\n\n- Chan: HTTP Error 404"
    )


def test_a_channel_error_lists_at_most_20_channels_and_counts_the_rest():
    failures = [ChannelFailure(f"Chan {i:02}", f"error {i:02}") for i in range(23)]

    notification = notify.channel_error(failures)

    assert notification.title == "YouTube AutoManager: cannot read 23 channels"
    lines = notification.body.splitlines()
    assert lines[0] == "yt-dlp failed to read 23 channels. The run continued with the other channels."
    assert lines[2:] == [f"- Chan {i:02}: error {i:02}" for i in range(20)] + ["and 3 more"]


def test_a_channel_error_shortens_a_long_error_text():
    body = notify.channel_error([ChannelFailure("Chan", "x" * 500)]).body

    assert body.endswith("- Chan: " + "x" * (notify.MAX_ERROR_CHARS - 3) + "...")


def test_a_channel_error_needs_a_failed_channel():
    with pytest.raises(ValueError, match="at least one"):
        notify.channel_error([])


def test_run_summary_lists_at_most_20_videos_and_counts_the_rest():
    body = notify.run_summary(_videos(25), []).body

    assert [f"watch?v=v{i:03}" in body for i in range(25)] == [True] * 20 + [False] * 5
    assert "and 5 more" in body


def test_run_summary_with_20_videos_lists_them_all():
    body = notify.run_summary(_videos(20), []).body

    assert all(f"watch?v=v{i:03}" in body for i in range(20))
    assert "more" not in body


def test_run_summary_shows_each_video_with_its_playlist_and_the_errors():
    notification = notify.run_summary(_videos(2), ["Channel A: HTTP Error 404"])

    assert "2 videos added, 1 error" in notification.title
    assert "Video 1 -> Watch later" in notification.body
    assert "- Channel A: HTTP Error 404" in notification.body


def test_run_summary_without_videos_says_so():
    notification = notify.run_summary([], [])

    assert "0 videos added" in notification.title
    assert "no videos" in notification.body


@pytest.mark.parametrize(("days_left", "when"), [(3, "in 3 days"), (1, "in 1 day"), (0, "today")])
def test_cookie_expiry_names_the_days_left_and_the_time(days_left, when):
    notification = notify.cookie_expiry(days_left, datetime(2026, 10, 14, 12, 30, tzinfo=UTC))

    assert notification.title.endswith(f"expire {when}")
    assert "2026-10-14 12:30+00:00" in notification.body


def test_cookie_expiry_refuses_a_negative_number_of_days():
    with pytest.raises(ValueError, match="negative"):
        notify.cookie_expiry(-1, datetime(2026, 10, 14, tzinfo=UTC))


def test_ytdlp_update_tells_a_new_version_from_the_same_version_and_a_failure():
    updated = notify.ytdlp_update("2026.09.01", "2026.10.01", ok=True)
    same = notify.ytdlp_update("2026.10.01", "2026.10.01", ok=True)
    failed = notify.ytdlp_update("2026.09.01", "2026.10.01", ok=False, error_text="no space left")

    assert "updated to 2026.10.01" in updated.title
    assert "up to date" in same.title
    assert "failed" in failed.title
    assert "no space left" in failed.body


@pytest.mark.parametrize(
    ("image_version", "image"),
    [("2026.8.19", "the yt-dlp 2026.8.19 of the image"), (None, "the yt-dlp of the image")],
)
def test_ytdlp_update_without_an_old_version_names_the_yt_dlp_of_the_image(image_version, image):
    installed = notify.ytdlp_update(None, "2026.10.01", ok=True, image_version=image_version)
    failed = notify.ytdlp_update(None, "2026.10.01", ok=False, error_text="no space left", image_version=image_version)
    no_answer = notify.ytdlp_update(None, None, ok=False, error_text="network is down", image_version=image_version)

    assert installed.title.endswith("yt-dlp updated to 2026.10.01")
    assert installed.body == (
        f"yt-dlp 2026.10.01 is now in the data folder, which held no yt-dlp before. Until now, the service used {image}. "
        "The next run uses the new version."
    )
    assert failed.body == (
        f"yt-dlp did not update to 2026.10.01. The data folder holds no yt-dlp, so the service keeps {image}."
        "\n\nno space left"
    )
    assert no_answer.body.startswith(
        f"yt-dlp did not update. The data folder holds no yt-dlp, so the service keeps {image}."
    )
    for notification in (installed, failed, no_answer):
        assert "None" not in notification.body


def test_ytdlp_update_without_the_latest_version_keeps_the_old_one():
    notification = notify.ytdlp_update("2026.09.01", None, ok=False, error_text="network is down")

    assert (
        notification.body == "yt-dlp did not update from 2026.09.01. The service keeps 2026.09.01.\n\nnetwork is down"
    )


def test_the_opening_of_the_breaker_names_the_reason_the_pause_and_its_end_in_the_given_time_zone():
    until = datetime(2026, 10, 28, 13, 0, tzinfo=UTC).astimezone(ZoneInfo("Europe/Kyiv"))

    notification = notify.request_breaker_opened('a "try again later" page', timedelta(hours=1), until)

    assert notification.title == "YouTube AutoManager: the requests with cookies are paused"
    assert notification.body == (
        'YouTube answered a request with the cookies of the account with a "try again later" page. The service sends '
        "no request with the cookies for 1 hour, until 2026-10-28 15:00 EET. Until then, each action with the account "
        "fails at once without a request to YouTube. The reads of public channels go on."
    )
    assert notification.level is Level.WARNING


@pytest.mark.parametrize(
    ("pause", "text"),
    [
        (timedelta(hours=6), "for 6 hours"),
        (timedelta(hours=24), "for 24 hours"),
    ],
)
def test_the_opening_of_the_breaker_gives_the_pause_in_hours(pause, text):
    notification = notify.request_breaker_opened("HTTP 429", pause, datetime(2026, 10, 28, tzinfo=UTC))

    assert f" {text}, until 2026-10-28 00:00 UTC." in notification.body


def test_the_end_of_the_breaker_says_that_the_requests_with_cookies_work_again():
    notification = notify.request_breaker_closed()

    assert notification.title == "YouTube AutoManager: the requests with cookies work again"
    assert notification.body.endswith("The requests with cookies work again.")
    assert notification.level is Level.WARNING
