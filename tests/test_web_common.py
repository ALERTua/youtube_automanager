from datetime import UTC, datetime
from unittest.mock import Mock, call
from zoneinfo import ZoneInfo

import pytest
from ui_support import store

from youtube_automanager.db.models import Schedule, ScheduleKind
from youtube_automanager.web.ui.common import busy, column, format_time, read_timezone, stored_timezone


def test_column_left_aligns_the_field_with_the_name_of_the_column():
    assert column("title", "Channel") == {"name": "title", "label": "Channel", "field": "title", "align": "left"}


def test_column_can_be_sortable():
    assert column("title", "Channel", sortable=True) == {
        "name": "title",
        "label": "Channel",
        "field": "title",
        "align": "left",
        "sortable": True,
    }


def test_time_zone_of_the_schedule_is_the_default_before_a_schedule_exists(sessions):
    assert read_timezone(sessions, "Europe/Kyiv") == "Europe/Kyiv"
    with sessions() as session:
        assert stored_timezone(session, "Europe/Kyiv") == "Europe/Kyiv"


def test_time_zone_of_the_schedule_is_the_stored_one(sessions):
    store(sessions, Schedule(enabled=True, kind=ScheduleKind.CRON, timezone="Asia/Tokyo"))

    assert read_timezone(sessions, "Europe/Kyiv") == "Asia/Tokyo"
    with sessions() as session:
        assert stored_timezone(session, "Europe/Kyiv") == "Asia/Tokyo"


def test_time_shows_in_the_zone_with_its_name():
    moment = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    assert format_time(moment, ZoneInfo("Europe/Kyiv")) == "2026-10-01 15:00 EEST"


def test_busy_button_loads_and_is_off_during_the_block_and_back_to_normal_after_it():
    button = Mock(is_deleted=False)

    with busy(button):
        assert button.mock_calls == [call.props("loading"), call.disable()]

    assert button.mock_calls[2:] == [call.props(remove="loading"), call.enable()]


def test_busy_button_is_back_to_normal_when_the_block_fails():
    button = Mock(is_deleted=False)

    with pytest.raises(RuntimeError), busy(button):
        raise RuntimeError

    assert button.mock_calls[2:] == [call.props(remove="loading"), call.enable()]


def test_busy_button_that_the_block_deleted_stays_as_it_is():
    button = Mock(is_deleted=False)

    with busy(button):
        button.is_deleted = True

    assert button.mock_calls == [call.props("loading"), call.disable()]
