"""Tests of the processing mode and the cookie rotation as the settings page reads and writes them."""

import pytest

from youtube_automanager.db.models import Setting
from youtube_automanager.engine import sync
from youtube_automanager.engine.modes import MIN_CHECKED, Processing, ProcessingMode
from youtube_automanager.rotation import ROTATION_KEY
from youtube_automanager.web.ui.service_settings import (
    MODE_HELP,
    MODE_LABELS,
    PROCESSING_MODE_KEY,
    StoredMode,
    mode_help,
    read_processing,
    read_rotation,
    save_processing,
    save_rotation,
)


def setting(sessions, key):
    with sessions() as session:
        row = session.get(Setting, key)
        return None if row is None else row.value


def store_setting(sessions, key, value):
    with sessions.begin() as session:
        session.add(Setting(key=key, value=value))


def test_the_key_of_the_mode_is_the_key_of_the_sync_engine():
    assert PROCESSING_MODE_KEY == sync.PROCESSING_MODE_KEY


def test_each_mode_has_a_label_and_a_help_text():
    assert set(MODE_LABELS) == set(MODE_HELP) == set(ProcessingMode)
    assert f"at least the {MIN_CHECKED} newest videos" in mode_help(ProcessingMode.STOP_AT_SEEN)


def test_without_a_stored_mode_the_mode_is_the_default(db_sessions):
    assert read_processing(db_sessions) == StoredMode(Processing())


@pytest.mark.parametrize(
    "processing",
    [Processing(), Processing(ProcessingMode.NEWEST, 30), Processing(ProcessingMode.MARK_DATE)],
)
def test_a_saved_mode_reads_back_as_the_engine_reads_it(db_sessions, processing):
    save_processing(db_sessions, processing)

    assert read_processing(db_sessions) == StoredMode(processing)
    with db_sessions() as session:
        assert sync.load_processing(session) == processing


def test_a_stored_mode_that_is_not_valid_reads_with_its_cause(db_sessions):
    store_setting(db_sessions, PROCESSING_MODE_KEY, {"mode": "newest"})

    stored = read_processing(db_sessions)

    assert stored.processing is None
    assert "needs a count" in stored.error


def test_a_save_replaces_a_stored_mode_that_is_not_valid(db_sessions):
    store_setting(db_sessions, PROCESSING_MODE_KEY, "newest")

    save_processing(db_sessions, Processing(ProcessingMode.NEWEST, 5))

    assert setting(db_sessions, PROCESSING_MODE_KEY) == {"mode": "newest", "count": 5}


def test_the_rotation_is_on_by_default_and_its_saves_store_json_booleans(db_sessions):
    assert read_rotation(db_sessions) is True

    save_rotation(db_sessions, False)
    assert setting(db_sessions, ROTATION_KEY) is False
    assert read_rotation(db_sessions) is False

    save_rotation(db_sessions, True)
    assert setting(db_sessions, ROTATION_KEY) is True
    assert read_rotation(db_sessions) is True


@pytest.mark.parametrize(("stored", "saved"), [(1, True), (0, False), ("true", True)])
def test_a_save_of_the_rotation_replaces_a_value_of_another_type(db_sessions, stored, saved):
    # The change tracking takes 1 for true, so an update in place of the row would keep the 1.
    store_setting(db_sessions, ROTATION_KEY, stored)
    assert read_rotation(db_sessions) is False

    save_rotation(db_sessions, saved)

    value = setting(db_sessions, ROTATION_KEY)
    assert value is saved
    assert read_rotation(db_sessions) is saved
