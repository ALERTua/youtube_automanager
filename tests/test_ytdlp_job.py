"""Tests of the yt-dlp update job and its settings. A fake install stands in for PyPI and uv, so no test reads PyPI."""

import datetime
import importlib.metadata
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from conftest import FakeYtdlpInstall

from youtube_automanager import ytdlp_job
from youtube_automanager.db.models import Setting
from youtube_automanager.notify import Event
from youtube_automanager.runner import SCHEDULED_WAIT_S, SyncRunner, start_child
from youtube_automanager.youtube import ytdlp_updater
from youtube_automanager.ytdlp_job import (
    LAST_UPDATE_KEY,
    SETTINGS_KEY,
    LastUpdate,
    UpdateSettings,
    YtdlpUpdates,
    change_settings,
    parse_time,
    read_last_update,
    read_settings,
    save_settings,
)

WAIT_SECONDS = 10
# How long a test waits for something that must not happen.
SETTLE_SECONDS = 0.3
NOW = datetime.datetime(2026, 10, 12, 1, 0, tzinfo=datetime.UTC)
OLD = "2026.7.1"
NEW = "2026.8.19"
IMAGE = importlib.metadata.version("yt-dlp")


def store(sessions, key, value):
    with sessions.begin() as session:
        session.add(Setting(key=key, value=value))


def stored(sessions, key):
    with sessions() as session:
        row = session.get(Setting, key)
        return None if row is None else row.value


@pytest.fixture
def ytdlp_dir(tmp_path):
    return tmp_path / "data" / "yt-dlp"


@pytest.fixture
def make_runner(db_sessions, db_url, ytdlp_dir, fake_starter):
    def factory(start=fake_starter):
        return SyncRunner(
            db_sessions,
            ytdlp_dir=ytdlp_dir,
            database_url=db_url,
            child_timeout_s=WAIT_SECONDS,
            start=start,
            lock=threading.Lock(),
        )

    return factory


@pytest.fixture
def runner(make_runner):
    return make_runner()


@pytest.fixture
def install(runner):
    fake = FakeYtdlpInstall()
    fake.lock = runner._lock
    return fake


@pytest.fixture
def sent():
    return []


@pytest.fixture
def updates(db_sessions, runner, ytdlp_dir, install, sent):
    return YtdlpUpdates(db_sessions, runner, ytdlp_dir, install=install, send=sent.append, clock=lambda: NOW)


def in_thread(target):
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def hold_lock(runner):
    """Hold the sync lock of ``runner`` in another thread until the returned event is set.

    The thread holds the lock longer than a test waits for a thread, so a wait that ends shows the cause in the test.
    """
    holding, release = threading.Event(), threading.Event()

    def hold():
        with runner.lock_held():
            holding.set()
            release.wait(WAIT_SECONDS * 6)

    thread = in_thread(hold)
    assert holding.wait(WAIT_SECONDS)
    return release, thread


# The time of the day.


@pytest.mark.parametrize(
    ("text", "expected"),
    [("04:00", datetime.time(4, 0)), ("4:05", datetime.time(4, 5)), (" 23:59 ", datetime.time(23, 59))],
)
def test_parse_time_takes_h_mm_and_hh_mm(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize("text", ["24:00", "12:60", "1200", "", "4:5", "04:00:00", "-1:00", "١٢:٠٠", "４:00"])
def test_parse_time_refuses_another_text(text):
    with pytest.raises(ValueError, match="H:MM or HH:MM"):
        parse_time(text)


# The settings.


def test_without_a_stored_value_the_update_runs_each_day_at_0400_from_stable(db_sessions):
    assert read_settings(db_sessions) == UpdateSettings(enabled=True, channel="stable", at=datetime.time(4, 0))


def test_saved_settings_are_one_json_value(db_sessions):
    settings = UpdateSettings(enabled=False, channel="nightly", at=datetime.time(5, 30))

    save_settings(db_sessions, settings)

    assert stored(db_sessions, SETTINGS_KEY) == {"enabled": False, "channel": "nightly", "time": "05:30"}
    assert read_settings(db_sessions) == settings


def test_a_save_replaces_a_stored_value_of_another_type(db_sessions):
    store(db_sessions, SETTINGS_KEY, 1)

    save_settings(db_sessions, UpdateSettings(channel="nightly"))

    assert stored(db_sessions, SETTINGS_KEY) == {"enabled": True, "channel": "nightly", "time": "04:00"}


def test_change_settings_writes_only_the_changed_fields(db_sessions):
    before = UpdateSettings()
    # Another page moved the update to 06:15 after the caller read the settings.
    save_settings(db_sessions, UpdateSettings(at=datetime.time(6, 15)))

    saved = change_settings(db_sessions, before, UpdateSettings(channel="nightly"))

    assert saved == UpdateSettings(enabled=True, channel="nightly", at=datetime.time(6, 15))
    assert stored(db_sessions, SETTINGS_KEY) == {"enabled": True, "channel": "nightly", "time": "06:15"}


def test_change_settings_replaces_a_stored_value_that_is_not_valid(db_sessions, caplog):
    store(db_sessions, SETTINGS_KEY, {"time": 400})

    saved = change_settings(db_sessions, UpdateSettings(), UpdateSettings(enabled=False))

    assert saved == UpdateSettings(enabled=False)
    assert stored(db_sessions, SETTINGS_KEY) == {"enabled": False, "channel": "stable", "time": "04:00"}
    assert f"The setting {SETTINGS_KEY} is not valid" in caplog.text


def test_a_stored_value_without_some_keys_takes_their_defaults(db_sessions):
    store(db_sessions, SETTINGS_KEY, {"channel": "nightly"})

    assert read_settings(db_sessions) == UpdateSettings(enabled=True, channel="nightly", at=datetime.time(4, 0))


@pytest.mark.parametrize(
    "value",
    [1, "04:00", {"enabled": 1}, {"channel": "beta"}, {"channel": ["stable"]}, {"time": "25:00"}, {"time": 400}],
)
def test_a_stored_value_that_is_not_valid_gives_the_defaults_and_a_warning(db_sessions, caplog, value):
    store(db_sessions, SETTINGS_KEY, value)

    assert read_settings(db_sessions) == UpdateSettings()
    assert f"The setting {SETTINGS_KEY} is not valid" in caplog.text


def test_settings_refuse_an_unknown_channel():
    with pytest.raises(ValueError, match="Unknown yt-dlp channel"):
        UpdateSettings(channel="beta")


# The record of the last update.


def test_no_update_ran_yet(db_sessions):
    assert read_last_update(db_sessions) is None


def record_value(**changes):
    value = {
        "at": "2026-10-12T01:00:00+00:00",
        "ok": True,
        "channel": "stable",
        "old_version": None,
        "new_version": NEW,
        "error": None,
        "skipped": False,
    }
    return {**value, **changes}


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"at": "2026-10-12T01:00:00+00:00"},
        record_value(at="yesterday"),
        record_value(at="2026-10-12T01:00:00"),
        record_value(at=5),
        record_value(ok=1),
        record_value(channel="beta"),
        record_value(old_version=7),
        record_value(skipped="yes"),
    ],
    ids=[
        "not-an-object",
        "missing-keys",
        "bad-time",
        "no-time-zone",
        "time-not-text",
        "ok-not-bool",
        "bad-channel",
        "version-not-text",
        "skipped-not-bool",
    ],
)
def test_a_record_that_is_not_valid_reads_as_no_update(db_sessions, caplog, value):
    store(db_sessions, LAST_UPDATE_KEY, value)

    assert read_last_update(db_sessions) is None
    assert f"The setting {LAST_UPDATE_KEY} is not valid" in caplog.text


# An update on request.


def test_update_now_installs_from_the_stored_channel_while_it_holds_the_sync_lock(
    db_sessions, updates, install, runner, ytdlp_dir
):
    save_settings(db_sessions, UpdateSettings(channel="nightly"))
    install.plan.append({"version": NEW})

    record = updates.update_now()

    assert install.calls == [(ytdlp_dir, "nightly")]
    assert install.lock_taken == [True]
    assert not runner.is_active()
    assert record == LastUpdate(NOW, True, "nightly", None, NEW, None)
    assert read_last_update(db_sessions) == record
    assert stored(db_sessions, LAST_UPDATE_KEY) == record_value(channel="nightly")


def test_update_now_while_the_sync_lock_is_taken_changes_nothing(db_sessions, updates, install, runner, sent):
    with runner.lock_held(timeout=0):
        assert updates.update_now() is None

    assert install.calls == []
    assert read_last_update(db_sessions) is None
    assert sent == []


def test_a_new_version_sends_the_notification_and_the_same_version_does_not(
    db_sessions, updates, install, ytdlp_dir, sent
):
    install.write_release(ytdlp_dir, OLD)
    install.plan.append({"version": NEW})

    updated = updates.update_now()
    current = updates.update_now()

    assert (updated.changed, current.changed) == (True, False)
    assert (current.old_version, current.new_version) == (NEW, NEW)
    assert read_last_update(db_sessions) == current
    [notification] = sent
    assert notification.event is Event.YTDLP_UPDATE_RESULT
    assert f"updated to {NEW}" in notification.title
    assert f"yt-dlp updated from {OLD} to {NEW}. The next run uses the new version." == notification.body


def test_a_failed_update_sends_the_notification_and_keeps_the_error(db_sessions, updates, install, ytdlp_dir, sent):
    install.write_release(ytdlp_dir, OLD)
    install.plan.append({"version": NEW, "error": "uv exited with code 2: error: No solution found"})

    record = updates.update_now()

    assert (record.ok, record.old_version, record.new_version) == (False, OLD, NEW)
    assert record.error == "uv exited with code 2: error: No solution found"
    assert read_last_update(db_sessions) == record
    [notification] = sent
    assert "the yt-dlp update failed" in notification.title
    assert f"The service keeps {OLD}." in notification.body
    assert "No solution found" in notification.body


def test_a_first_install_names_the_version_of_the_image(updates, install, sent):
    install.plan.append({"version": NEW})

    updates.update_now()

    [notification] = sent
    assert f"yt-dlp {NEW} is now in the data folder, which held no yt-dlp before." in notification.body
    assert f"Until now, the service used the yt-dlp {IMAGE} of the image." in notification.body


def test_a_failed_first_install_says_that_the_service_keeps_the_yt_dlp_of_the_image(updates, install, sent):
    install.plan.append({"error": "Cannot get the latest version from PyPI: network is down"})

    record = updates.update_now()

    assert (record.ok, record.old_version, record.new_version) == (False, None, None)
    [notification] = sent
    assert notification.body.startswith(
        f"yt-dlp did not update. The data folder holds no yt-dlp, so the service keeps the yt-dlp {IMAGE} of the image."
    )
    assert "None" not in notification.body


def test_a_long_error_keeps_only_its_end(updates, install):
    install.plan.append({"version": NEW, "error": "first line\n" + "x" * 5000 + "\nlast line"})

    record = updates.update_now()

    assert len(record.error) <= 2000
    assert record.error.endswith("x\nlast line")
    assert "first line" not in record.error


def test_a_failed_notification_does_not_change_the_result(db_sessions, runner, ytdlp_dir, install, caplog):
    def broken_send(notification):
        raise RuntimeError("the targets cannot be read")

    updates = YtdlpUpdates(db_sessions, runner, ytdlp_dir, install=install, send=broken_send, clock=lambda: NOW)
    install.plan.append({"version": NEW})

    record = updates.update_now()

    assert record.changed
    assert read_last_update(db_sessions) == record
    assert "Cannot send the result of the yt-dlp update: RuntimeError" in caplog.text


# The scheduled update.


def test_the_scheduled_update_waits_for_the_sync_lock(updates, install, runner):
    release, holder = hold_lock(runner)
    job = in_thread(updates.run_scheduled)

    job.join(SETTLE_SECONDS)
    assert job.is_alive()
    assert install.calls == []
    release.set()
    job.join(WAIT_SECONDS)
    holder.join(WAIT_SECONDS)

    assert not job.is_alive()
    assert len(install.calls) == 1
    assert install.lock_taken == [True]


def test_the_scheduled_update_waits_as_long_as_a_scheduled_run():
    assert ytdlp_job.SCHEDULED_WAIT_S == SCHEDULED_WAIT_S == 1800


def test_the_scheduled_update_gives_up_after_the_wait(db_sessions, updates, install, runner, monkeypatch, caplog):
    monkeypatch.setattr(ytdlp_job, "SCHEDULED_WAIT_S", 0.2)

    with runner.lock_held(timeout=0):
        updates.run_scheduled()

    assert install.calls == []
    assert read_last_update(db_sessions) is None
    assert "The scheduled yt-dlp update did not start" in caplog.text


def test_a_stop_of_the_service_ends_the_wait_and_starts_no_update(updates, install, runner):
    release, holder = hold_lock(runner)
    job = in_thread(updates.run_scheduled)
    job.join(SETTLE_SECONDS)
    try:
        assert runner.stop(timeout=0.1) is False
        job.join(WAIT_SECONDS)
        # The wait ended while the lock was still taken.
        assert not job.is_alive()
    finally:
        release.set()
        holder.join(WAIT_SECONDS)

    assert install.calls == []
    # The lock is free now, but the service stops, so an update on request does not start either.
    assert updates.update_now() is None
    assert install.calls == []


def test_the_scheduled_update_reads_the_channel_after_the_wait(db_sessions, updates, install, runner):
    release, holder = hold_lock(runner)
    job = in_thread(updates.run_scheduled)
    job.join(SETTLE_SECONDS)

    save_settings(db_sessions, UpdateSettings(channel="nightly"))
    release.set()
    job.join(WAIT_SECONDS)
    holder.join(WAIT_SECONDS)

    assert [channel for _folder, channel in install.calls] == ["nightly"]


def test_a_scheduled_update_that_is_off_installs_nothing(db_sessions, updates, install, sent):
    save_settings(db_sessions, UpdateSettings(enabled=False))

    updates.run_scheduled()

    assert install.calls == []
    assert read_last_update(db_sessions) is None


def test_the_scheduled_update_stores_and_sends_its_result(db_sessions, updates, install, ytdlp_dir, sent):
    install.write_release(ytdlp_dir, OLD)
    install.plan.extend([{"version": NEW}, {"version": NEW}])

    updates.run_scheduled()
    updates.run_scheduled()

    assert [call[1] for call in install.calls] == ["stable", "stable"]
    assert read_last_update(db_sessions) == LastUpdate(NOW, True, "stable", NEW, NEW, None)
    assert [notification.title for notification in sent] == [f"YouTube AutoManager: yt-dlp updated to {NEW}"]


# The next child process.


def test_the_next_child_imports_the_new_version_without_a_new_image(
    db_sessions, make_runner, ytdlp_dir, monkeypatch, tmp_path
):
    """A real update into the data folder, with fake answers of PyPI and uv, and a real child of the runner after it."""
    pypi = {"info": {"version": NEW}, "releases": {OLD: [{"yanked": False}], NEW: [{"yanked": False}]}}
    monkeypatch.setattr(ytdlp_updater, "_fetch_pypi_json", lambda: pypi)
    real_run = subprocess.run

    def fake_uv(command, **kwargs):
        if command[:3] != ["uv", "pip", "install"]:
            return real_run(command, **kwargs)
        target = command[command.index("--target") + 1]
        FakeYtdlpInstall.write_release(Path(target), command[-1].removeprefix("yt-dlp=="))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_uv)
    runner = make_runner(start=start_child)
    updates = YtdlpUpdates(db_sessions, runner, ytdlp_dir, install=ytdlp_updater.update, send=lambda _: None)
    FakeYtdlpInstall.write_release(ytdlp_dir, OLD)
    code = "import sys, yt_dlp, yt_dlp.version\nprint(yt_dlp.version.__version__, yt_dlp.__file__, file=sys.stderr)\n"

    def child_import():
        end = runner.run_short_child([sys.executable, "-c", code], "The import check", WAIT_SECONDS * 6)
        assert end.returncode == 0, end.error_text
        version, module_file = end.last_line.split(" ", 1)
        return version, Path(module_file).resolve().parent.parent

    before = child_import()
    record = updates.update_now()
    after = child_import()

    assert before == (OLD, ytdlp_dir.resolve())
    assert (record.ok, record.old_version, record.new_version) == (True, OLD, NEW)
    assert after == (NEW, ytdlp_dir.resolve())


def test_the_job_module_imports_neither_nicegui_nor_yt_dlp():
    code = (
        "import sys\n"
        "import youtube_automanager.ytdlp_job\n"
        "loaded = sorted(name for name in sys.modules if name.split('.')[0] in ('nicegui', 'yt_dlp'))\n"
        "assert not loaded, loaded\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr


# The previous release, the yt-dlp of the image and the skipped version


def previous(ytdlp_dir):
    return ytdlp_updater.previous_dir(ytdlp_dir)


def test_a_record_of_an_older_service_has_no_skipped_flag(db_sessions):
    value = record_value()
    del value["skipped"]
    store(db_sessions, LAST_UPDATE_KEY, value)

    assert read_last_update(db_sessions) == LastUpdate(NOW, True, "stable", None, NEW, None, skipped=False)


def test_each_update_gets_the_check_of_a_new_release(db_sessions, runner, ytdlp_dir, install, sent):
    def smoke(folder, version):
        return None

    updates = YtdlpUpdates(db_sessions, runner, ytdlp_dir, install=install, smoke=smoke, send=sent.append)

    updates.update_now()
    updates.run_scheduled()

    assert [options["smoke"] for options in install.options] == [smoke, smoke]


def test_the_scheduled_update_skips_the_skipped_version_and_update_now_does_not(
    db_sessions, updates, install, ytdlp_dir, sent
):
    install.write_release(ytdlp_dir, OLD)
    store(db_sessions, ytdlp_job.SKIPPED_KEY, NEW)
    install.plan.extend([{"version": NEW}, {"version": NEW}])

    updates.run_scheduled()
    skipped = read_last_update(db_sessions)
    updates.update_now()

    assert [options["skipped"] for options in install.options] == [NEW, None]
    assert skipped == LastUpdate(NOW, True, "stable", OLD, NEW, None, skipped=True)
    assert not skipped.changed
    assert ytdlp_updater.installed_version(ytdlp_dir) == NEW
    assert [notification.title for notification in sent] == [f"YouTube AutoManager: yt-dlp updated to {NEW}"]


def test_an_update_that_installs_a_new_version_forgets_the_skipped_version(db_sessions, updates, install, ytdlp_dir):
    install.write_release(ytdlp_dir, OLD)
    store(db_sessions, ytdlp_job.SKIPPED_KEY, "2026.7.15")
    install.plan.append({"version": NEW})

    updates.run_scheduled()

    assert ytdlp_job.read_skipped(db_sessions) is None


def test_an_update_that_installs_nothing_keeps_the_skipped_version(db_sessions, updates, install, ytdlp_dir):
    install.write_release(ytdlp_dir, OLD)
    store(db_sessions, ytdlp_job.SKIPPED_KEY, NEW)
    install.plan.extend([{"version": NEW}, {"version": NEW, "error": "the check failed"}])

    updates.run_scheduled()
    updates.update_now()

    assert ytdlp_job.read_skipped(db_sessions) == NEW


def test_a_skipped_version_that_is_not_a_text_counts_as_none(db_sessions, caplog):
    store(db_sessions, ytdlp_job.SKIPPED_KEY, 7)

    assert ytdlp_job.read_skipped(db_sessions) is None
    assert f"The setting {ytdlp_job.SKIPPED_KEY} is not a text" in caplog.text


def test_go_back_puts_the_previous_release_back_and_skips_the_version_that_it_left(
    db_sessions, updates, install, ytdlp_dir
):
    install.write_release(ytdlp_dir, OLD)
    install.plan.append({"version": NEW})
    updates.update_now()

    result = updates.go_back()

    assert result == ytdlp_job.SwitchResult(left=NEW, active=OLD, error=None)
    assert ytdlp_updater.installed_version(ytdlp_dir) == OLD
    assert ytdlp_updater.installed_version(previous(ytdlp_dir)) == NEW
    assert ytdlp_job.read_skipped(db_sessions) == NEW


def test_use_image_moves_the_release_aside_and_skips_its_version(db_sessions, updates, install, ytdlp_dir):
    install.write_release(ytdlp_dir, NEW)

    result = updates.use_image()

    assert result == ytdlp_job.SwitchResult(left=NEW, active=IMAGE, error=None)
    assert ytdlp_updater.active_version(ytdlp_dir) == (IMAGE, "image")
    assert ytdlp_updater.installed_version(previous(ytdlp_dir)) == NEW
    assert ytdlp_job.read_skipped(db_sessions) == NEW


def test_a_failed_switch_keeps_the_skipped_version(db_sessions, updates, install, ytdlp_dir):
    install.write_release(ytdlp_dir, NEW)
    store(db_sessions, ytdlp_job.SKIPPED_KEY, "2026.9.1")

    result = updates.go_back()

    assert result == ytdlp_job.SwitchResult(left=NEW, active=NEW, error="The data folder keeps no previous yt-dlp.")
    assert ytdlp_job.read_skipped(db_sessions) == "2026.9.1"


@pytest.mark.parametrize("action", ["go_back", "use_image"])
def test_a_switch_while_the_sync_lock_is_taken_changes_nothing(
    db_sessions, updates, install, runner, ytdlp_dir, action
):
    install.write_release(previous(ytdlp_dir), OLD)
    install.write_release(ytdlp_dir, NEW)

    with runner.lock_held(timeout=0):
        assert getattr(updates, action)() is None

    assert ytdlp_updater.installed_version(ytdlp_dir) == NEW
    assert ytdlp_job.read_skipped(db_sessions) is None
