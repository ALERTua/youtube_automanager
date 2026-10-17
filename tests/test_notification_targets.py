"""Tests of the reads and the writes of the notification targets for the settings page."""

import pytest
from sqlalchemy import select

from youtube_automanager.db.models import NotificationTarget
from youtube_automanager.notify import DEFAULT_EVENTS, Event, Level, Target
from youtube_automanager.web.ui.notification_targets import (
    APPRISE_URLS,
    EVENT_LABELS,
    LEVEL_LABELS,
    SaveOutcome,
    TargetChoices,
    TargetRecord,
    delete_target,
    keep_edits,
    load_target,
    read_targets,
    save_choices,
    save_target,
    url_scheme,
)

TOKEN = "s3cr3tT0ken"


def stored(sessions) -> list[NotificationTarget]:
    with sessions() as session:
        return list(session.scalars(select(NotificationTarget).order_by(NotificationTarget.id)))


def add(sessions, name="phone", url=f"tgram://{TOKEN}/chat", **values) -> int:
    values = {"min_level": 0, "events": ["run_failed"], "enabled": True} | values
    with sessions.begin() as session:
        row = NotificationTarget(name=name, url=url, **values)
        session.add(row)
        session.flush()
        return row.id


def test_each_event_and_each_level_has_a_label():
    assert set(EVENT_LABELS) == set(Event)
    assert set(LEVEL_LABELS) == set(Level)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"tgram://{TOKEN}/123", "tgram://…"),
        (f"workflow://host/{TOKEN}", "workflow://…"),
        (f"json+s://user:{TOKEN}@host", "json+s://…"),
        (f"{TOKEN}", "…"),
        (f"://{TOKEN}", "…"),
        (f"mailto:{TOKEN}@example.com", "…"),
        (f"{'x' * 40}://{TOKEN}", "…"),
        ("", "…"),
    ],
)
def test_the_scheme_of_a_url_shows_no_other_part_of_it(url, expected):
    assert url_scheme(url) == expected


def test_a_new_target_gets_the_default_events_the_lowest_level_and_the_switch_on(db_sessions):
    assert save_target(db_sessions, None, "Phone", f"tgram://{TOKEN}/1") is SaveOutcome.SAVED

    (row,) = stored(db_sessions)
    assert (row.name, row.url, row.min_level, row.enabled) == ("Phone", f"tgram://{TOKEN}/1", 0, True)
    assert row.events == [event.value for event in Event if event in DEFAULT_EVENTS]


def test_a_new_target_needs_a_url(db_sessions):
    with pytest.raises(ValueError):
        save_target(db_sessions, None, "Phone", None)

    assert stored(db_sessions) == []


def test_a_change_without_a_url_keeps_the_stored_url(db_sessions):
    target_id = add(db_sessions)

    assert save_target(db_sessions, target_id, "Desk", None) is SaveOutcome.SAVED
    assert [(row.name, row.url) for row in stored(db_sessions)] == [("Desk", f"tgram://{TOKEN}/chat")]

    assert save_target(db_sessions, target_id, "Desk", "json://localhost/new") is SaveOutcome.SAVED
    assert [(row.name, row.url) for row in stored(db_sessions)] == [("Desk", "json://localhost/new")]


def test_a_name_that_another_target_has_in_any_case_is_taken(db_sessions):
    add(db_sessions, name="Телефон")
    other = add(db_sessions, name="desk")

    assert save_target(db_sessions, None, "телефон", "json://localhost/x") is SaveOutcome.NAME_TAKEN
    assert save_target(db_sessions, other, "ТЕЛЕФОН", None) is SaveOutcome.NAME_TAKEN
    # A target keeps its own name, also in another case.
    assert save_target(db_sessions, other, "Desk", None) is SaveOutcome.SAVED
    assert [row.name for row in stored(db_sessions)] == ["Телефон", "Desk"]


def test_a_change_of_a_target_that_went_saves_nothing(db_sessions):
    assert save_target(db_sessions, 42, "Phone", "json://localhost/x") is SaveOutcome.GONE
    assert stored(db_sessions) == []


def test_the_records_hold_only_the_scheme_and_clean_the_stored_values(db_sessions):
    add(db_sessions, name="b", events=["video_added", "no_such_event", "run_failed"], min_level=9, enabled=False)
    add(db_sessions, name="a", url="json://localhost", events=None, min_level=-2)

    assert read_targets(db_sessions) == [
        TargetRecord(
            id=1,
            name="b",
            url_scheme="tgram://…",
            min_level=Level.FAILURE,
            events=frozenset({Event.VIDEO_ADDED, Event.RUN_FAILED}),
            enabled=False,
        ),
        TargetRecord(id=2, name="a", url_scheme="json://…", min_level=Level.INFO, events=frozenset(), enabled=True),
    ]
    assert TOKEN not in repr(read_targets(db_sessions))


def test_load_target_gives_the_url_for_a_test(db_sessions):
    target_id = add(db_sessions, min_level=2, events=["run_failed"])

    assert load_target(db_sessions, target_id) == Target(
        "phone", f"tgram://{TOKEN}/chat", min_level=Level.WARNING, events=frozenset({Event.RUN_FAILED})
    )
    assert load_target(db_sessions, 99) is None


def test_the_link_of_the_services_goes_to_the_apprise_site():
    # The wiki of the Apprise repository on GitHub says "Documentation Moved".
    assert APPRISE_URLS == "https://appriseit.com/services/"


def choices(events=(Event.RUN_FAILED,), min_level=Level.INFO, enabled=True) -> TargetChoices:
    return TargetChoices(frozenset(events), min_level, enabled)


def test_save_choices_writes_each_changed_choice_and_keeps_each_other_stored_name(db_sessions):
    target_id = add(db_sessions, events=["no_such_event", "run_failed"])
    before = choices()

    edited = choices(events={Event.VIDEO_ADDED}, min_level=Level.WARNING, enabled=False)
    assert save_choices(db_sessions, target_id, before, edited) is True

    (row,) = stored(db_sessions)
    assert (row.events, row.min_level, row.enabled) == (["video_added", "no_such_event"], 2, False)


def test_save_choices_keeps_the_values_that_the_page_did_not_change(db_sessions):
    # Another page turned the target off, raised its level and checked CHANNEL_ERROR after this page read it.
    target_id = add(db_sessions, events=["run_failed", "channel_error"], min_level=3, enabled=False)
    before = choices()

    assert save_choices(db_sessions, target_id, before, choices(events={Event.RUN_FAILED, Event.VIDEO_ADDED})) is True

    (row,) = stored(db_sessions)
    assert (row.events, row.min_level, row.enabled) == (["run_failed", "channel_error", "video_added"], 3, False)


def test_save_choices_of_a_target_that_went_tells_so(db_sessions):
    assert save_choices(db_sessions, 7, choices(), choices(enabled=False)) is False


def test_keep_edits_keeps_each_change_of_the_user_and_takes_each_other_choice_from_the_read():
    before = choices(events={Event.RUN_FAILED, Event.CHANNEL_ERROR}, min_level=Level.INFO, enabled=True)
    # The user checked VIDEO_ADDED, cleared CHANNEL_ERROR and raised the level.
    edited = choices(events={Event.RUN_FAILED, Event.VIDEO_ADDED}, min_level=Level.WARNING, enabled=True)
    # Another page cleared RUN_FAILED, checked CHANNEL_ERROR and SCHEDULER_STATE, lowered the level and turned it off.
    read = choices(events={Event.CHANNEL_ERROR, Event.SCHEDULER_STATE}, min_level=Level.SUCCESS, enabled=False)

    assert keep_edits(before, edited, read) == choices(
        events={Event.VIDEO_ADDED, Event.SCHEDULER_STATE}, min_level=Level.WARNING, enabled=False
    )
    assert keep_edits(before, before, read) == read


def test_delete_target_removes_the_row_and_ignores_a_missing_one(db_sessions):
    first = add(db_sessions, name="a")
    add(db_sessions, name="b")

    delete_target(db_sessions, first)
    delete_target(db_sessions, 99)

    assert [row.name for row in stored(db_sessions)] == ["b"]
