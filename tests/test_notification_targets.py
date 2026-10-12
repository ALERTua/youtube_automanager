"""Tests of the reads and the writes of the notification targets for the settings page."""

import pytest
from sqlalchemy import select

from youtube_automanager.db.models import NotificationTarget
from youtube_automanager.notify import DEFAULT_EVENTS, Event, Level, Target
from youtube_automanager.web.ui.notification_targets import (
    EVENT_LABELS,
    LEVEL_LABELS,
    SaveOutcome,
    TargetRecord,
    delete_target,
    load_target,
    read_targets,
    save_target,
    set_enabled,
    set_event,
    set_min_level,
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


def test_set_event_changes_one_event_and_keeps_each_other_stored_name(db_sessions):
    target_id = add(db_sessions, events=["no_such_event", "run_failed"])

    assert set_event(db_sessions, target_id, Event.VIDEO_ADDED, True) is True
    assert stored(db_sessions)[0].events == ["run_failed", "video_added", "no_such_event"]
    assert set_event(db_sessions, target_id, Event.RUN_FAILED, False) is True
    assert set_event(db_sessions, target_id, Event.RUN_FAILED, False) is True
    assert stored(db_sessions)[0].events == ["video_added", "no_such_event"]


def test_set_min_level_and_set_enabled_write_their_column(db_sessions):
    target_id = add(db_sessions)

    assert set_min_level(db_sessions, target_id, Level.WARNING) is True
    assert set_enabled(db_sessions, target_id, False) is True

    (row,) = stored(db_sessions)
    assert (row.min_level, row.enabled) == (2, False)


@pytest.mark.parametrize(
    ("write", "value"), [(set_min_level, Level.FAILURE), (set_enabled, False)], ids=["level", "enabled"]
)
def test_a_change_of_a_target_that_went_tells_so(db_sessions, write, value):
    assert write(db_sessions, 7, value) is False


def test_a_change_of_an_event_of_a_target_that_went_tells_so(db_sessions):
    assert set_event(db_sessions, 7, Event.RUN_FAILED, True) is False


def test_delete_target_removes_the_row_and_ignores_a_missing_one(db_sessions):
    first = add(db_sessions, name="a")
    add(db_sessions, name="b")

    delete_target(db_sessions, first)
    delete_target(db_sessions, 99)

    assert [row.name for row in stored(db_sessions)] == ["b"]
