import dataclasses
import importlib.metadata
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youtube_automanager import ytdlp_job
from youtube_automanager.db import make_engine, make_session_factory
from youtube_automanager.db.models import (
    Account,
    Run,
    RunStatus,
    RunTrigger,
    Schedule,
    ScheduleKind,
    SessionState,
    Setting,
)
from youtube_automanager.runner import INTERRUPTED_ERROR, UNAVAILABLE_EXIT_CODE
from youtube_automanager.scheduler import ROTATION_DELAY_S, ROTATION_JOB_ID, RunNowOutcome
from youtube_automanager.settings import Settings
from youtube_automanager.web.app import create_app

WAIT_SECONDS = 10
KYIV = ZoneInfo("Europe/Kyiv")


@pytest.fixture(autouse=True)
def logging_setups(monkeypatch):
    """Records the logging setup of the lifespan instead of a change of the handlers that pytest captures."""
    calls = []
    monkeypatch.setattr("youtube_automanager.web.app.setup_logging", calls.append)
    return calls


@pytest.fixture
def settings(tmp_path):
    # The data folder does not exist yet: the service creates the folder of its SQLite file.
    home = tmp_path / "data"
    return Settings(
        home=home,
        database_url=f"sqlite:///{home / 'service.sqlite'}",
        ytdlp_dir=home / "yt-dlp",
        tz="Europe/Kyiv",
        child_timeout_s=WAIT_SECONDS,
    )


@pytest.fixture
def sessions(settings, new_database):
    """Sessions of the database of ``settings``, with the newest schema."""
    new_database(settings.home / "service.sqlite")
    engine = make_engine(settings.database_url)
    yield make_session_factory(engine)
    engine.dispose()


def store(sessions, *rows):
    with sessions.begin() as session:
        session.add_all(rows)


def wait_until(condition, what):
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if result := condition():
            return result
        time.sleep(0.05)
    raise AssertionError(f"{what} did not happen within {WAIT_SECONDS} s")


def test_start_writes_the_default_schedule_and_starts_the_scheduler(settings, fake_starter, logging_setups):
    with TestClient(create_app(settings, start=fake_starter)) as client:
        status = client.get("/api/status").json()

    assert logging_setups == [settings.verbose]
    assert status["scheduler_state"] == "running"
    next_run_at = datetime.fromisoformat(status["next_run_at"])
    assert (next_run_at.astimezone(KYIV).hour, next_run_at.minute) == (18, 0)
    assert datetime.now(UTC) < next_run_at <= datetime.now(UTC) + timedelta(days=1)
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            (row,) = session.scalars(select(Schedule)).all()
    finally:
        engine.dispose()
    assert (row.enabled, row.kind, row.cron_expression, row.timezone) == (
        True,
        ScheduleKind.CRON,
        "0 18 * * *",
        "Europe/Kyiv",
    )


def test_start_keeps_the_saved_schedule(settings, sessions, fake_starter):
    store(sessions, Schedule(enabled=False, kind=ScheduleKind.INTERVAL, interval_s=600, timezone="UTC"))

    with TestClient(create_app(settings, start=fake_starter)) as client:
        status = client.get("/api/status").json()

    assert (status["scheduler_state"], status["next_run_at"]) == ("paused", None)


def test_start_marks_interrupted_runs_as_failed(settings, sessions, fake_starter):
    store(sessions, Run(status=RunStatus.RUNNING, trigger=RunTrigger.SCHEDULE))

    with TestClient(create_app(settings, start=fake_starter)) as client:
        (run,) = client.get("/api/runs").json()

    assert (run["status"], run["error_text"], run["finished_at"]) == ("failed", INTERRUPTED_ERROR, None)


def test_health_answers_when_the_database_answers(settings, fake_starter, tmp_path):
    with TestClient(create_app(settings, start=fake_starter)) as client:
        healthy = client.get("/health")
        services = client.app.state.services
        broken_engine = make_engine(f"sqlite:///{tmp_path / 'missing' / 'folder' / 'db.sqlite'}")
        client.app.state.services = dataclasses.replace(services, engine=broken_engine)
        try:
            broken = client.get("/health")
        finally:
            client.app.state.services = services
            broken_engine.dispose()

    assert (healthy.status_code, healthy.json()) == (200, {"status": "ok", "detail": None})
    assert (broken.status_code, broken.json()) == (503, {"status": "error", "detail": "The database does not answer"})


def test_runs_come_newest_first_with_limit_and_offset(settings, sessions, fake_starter):
    start = datetime(2026, 10, 1, tzinfo=UTC)
    store(
        sessions,
        *[
            Run(
                id=number,
                status=RunStatus.SUCCEEDED,
                trigger=RunTrigger.SCHEDULE,
                started_at=start + timedelta(days=number),
                finished_at=start + timedelta(days=number, minutes=5),
                added_count=number,
            )
            for number in (1, 2, 3)
        ],
    )

    with TestClient(create_app(settings, start=fake_starter)) as client:
        first_page = client.get("/api/runs", params={"limit": 2}).json()
        second_page = client.get("/api/runs", params={"limit": 2, "offset": 2}).json()
        refused = [client.get("/api/runs", params=params).status_code for params in ({"limit": 0}, {"limit": 501})]
        refused.append(client.get("/api/runs", params={"offset": -1}).status_code)

    assert [run["id"] for run in first_page] == [3, 2]
    assert [run["id"] for run in second_page] == [1]
    assert first_page[0] == {
        "id": 3,
        "started_at": "2026-10-04T00:00:00Z",
        "finished_at": "2026-10-04T00:05:00Z",
        "status": "succeeded",
        "trigger": "schedule",
        "error_text": None,
        "added_count": 3,
        "seen_count": 0,
    }
    assert refused == [422, 422, 422]


def test_status_shows_the_last_run_the_session_and_the_ytdlp_of_the_data_folder(settings, sessions, fake_starter):
    store(
        sessions,
        Account(session_state=SessionState.OK),
        Run(
            id=1, status=RunStatus.SUCCEEDED, trigger=RunTrigger.SCHEDULE, started_at=datetime(2026, 10, 1, tzinfo=UTC)
        ),
        Run(id=2, status=RunStatus.FAILED, trigger=RunTrigger.MANUAL, started_at=datetime(2026, 10, 2, tzinfo=UTC)),
    )
    dist_info = settings.ytdlp_dir / "yt_dlp-2026.9.1.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: yt-dlp\nVersion: 2026.9.1\n", encoding="utf-8")

    with TestClient(create_app(settings, start=fake_starter)) as client:
        status = client.get("/api/status").json()

    assert (status["last_run"]["id"], status["last_run"]["status"]) == (2, "failed")
    assert status["session_state"] == "ok"
    assert (status["ytdlp_version"], status["ytdlp_source"]) == ("2026.9.1", "data_folder")


def test_status_without_an_account_and_without_the_data_folder(settings, fake_starter):
    with TestClient(create_app(settings, start=fake_starter)) as client:
        status = client.get("/api/status").json()

    assert (status["last_run"], status["session_state"]) == (None, None)
    assert (status["ytdlp_version"], status["ytdlp_source"]) == (importlib.metadata.version("yt-dlp"), "image")


def test_scheduled_run_appears_in_the_runs(settings, sessions, fake_starter):
    store(sessions, Schedule(enabled=True, kind=ScheduleKind.INTERVAL, interval_s=1, timezone="UTC"))

    with TestClient(create_app(settings, start=fake_starter)) as client:

        def succeeded_scheduled_run():
            runs = client.get("/api/runs").json()
            return next((run for run in runs if run["trigger"] == "schedule" and run["status"] == "succeeded"), None)

        run = wait_until(succeeded_scheduled_run, "A scheduled run")

    assert run["error_text"] is None
    assert run["finished_at"] is not None
    assert fake_starter.calls[0][0][-2:] == ["--run-id", str(run["id"])]


def test_start_plans_the_first_turn_of_the_cookie_rotation(settings, fake_starter):
    with TestClient(create_app(settings, start=fake_starter)) as client:
        job = client.app.state.services.scheduler._scheduler.get_job(ROTATION_JOB_ID)

    assert job is not None
    assert datetime.now(UTC) < job.next_run_time <= datetime.now(UTC) + timedelta(seconds=ROTATION_DELAY_S)


def test_start_plans_the_ytdlp_update_each_day_at_0400_in_the_time_zone_of_the_schedule(settings, fake_starter):
    with TestClient(create_app(settings, start=fake_starter)) as client:
        services = client.app.state.services
        times = client.portal.call(services.scheduler.next_ytdlp_update_times, 2)

    assert [(time.astimezone(KYIV).hour, time.astimezone(KYIV).minute) for time in times] == [(4, 0)] * 2
    assert datetime.now(UTC) < times[0] <= datetime.now(UTC) + timedelta(days=1)


def test_start_follows_the_stored_ytdlp_settings(settings, sessions, fake_starter):
    store(sessions, Setting(key=ytdlp_job.SETTINGS_KEY, value={"enabled": True, "channel": "nightly", "time": "05:45"}))

    with TestClient(create_app(settings, start=fake_starter)) as client:
        times = client.portal.call(client.app.state.services.scheduler.next_ytdlp_update_times, 1)

    assert [(time.astimezone(KYIV).hour, time.astimezone(KYIV).minute) for time in times] == [(5, 45)]


def test_start_without_the_daily_ytdlp_update_plans_none(settings, sessions, fake_starter):
    store(sessions, Setting(key=ytdlp_job.SETTINGS_KEY, value={"enabled": False}))

    with TestClient(create_app(settings, start=fake_starter)) as client:
        times = client.portal.call(client.app.state.services.scheduler.next_ytdlp_update_times, 1)

    assert times == []


def test_the_ytdlp_update_of_the_service_holds_the_sync_lock_of_the_runs(settings, fake_starter, ytdlp_install):
    ytdlp_install.plan.append({"version": "2099.1.2"})

    with TestClient(create_app(settings, start=fake_starter)) as client:
        record = client.app.state.services.ytdlp.update_now()
        status = client.get("/api/status").json()

    assert ytdlp_install.calls == [(settings.ytdlp_dir, "stable")]
    assert ytdlp_install.lock_taken == [True]
    assert record.changed
    assert (status["ytdlp_version"], status["ytdlp_source"]) == ("2099.1.2", "data_folder")


def test_session_check_runs_the_check_child_and_answers_with_the_state(settings, sessions, fake_starter):
    checked_at = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
    store(sessions, Account(session_state=SessionState.OK, last_checked_at=checked_at))

    with TestClient(create_app(settings, start=fake_starter)) as client:
        answer = client.post("/api/session/check")

    assert answer.status_code == 200
    assert answer.json() == {
        "outcome": "checked",
        "session_state": "ok",
        "last_checked_at": "2026-10-12T18:00:00Z",
        "earliest_cookie_expiry": None,
        "detail": None,
    }
    assert fake_starter.calls[0][0][-1] == "check-session"


def test_session_check_without_usable_cookies_answers_with_the_cause(settings, fake_starter):
    fake_starter.plan.append({"exit_code": UNAVAILABLE_EXIT_CODE, "stderr": "No YouTube cookies are stored.\n"})

    with TestClient(create_app(settings, start=fake_starter)) as client:
        answer = client.post("/api/session/check")

    assert answer.status_code == 200
    assert (answer.json()["outcome"], answer.json()["detail"]) == ("unavailable", "No YouTube cookies are stored.")


def test_session_check_while_a_run_is_active_answers_409(settings, sessions, fake_starter):
    fake_starter.plan.append({"blocking": True})

    with TestClient(create_app(settings, start=fake_starter)) as client:
        client.portal.call(client.app.state.services.scheduler.run_now)
        assert fake_starter.child_started.wait(WAIT_SECONDS)
        answer = client.post("/api/session/check")
        fake_starter.children[0].finish(0)

    assert answer.status_code == 409
    assert answer.json()["outcome"] == "busy"
    assert len(fake_starter.calls) == 1


def test_session_check_answers_only_a_post(settings, fake_starter):
    with TestClient(create_app(settings, start=fake_starter)) as client:
        assert client.get("/api/session/check").status_code in (404, 405)


def test_shutdown_stops_the_active_run(settings, sessions, fake_starter):
    fake_starter.plan.append({"blocking": True})

    with TestClient(create_app(settings, start=fake_starter)) as client:
        result = client.portal.call(client.app.state.services.scheduler.run_now)
        assert fake_starter.child_started.wait(WAIT_SECONDS)

    assert result.outcome is RunNowOutcome.STARTED
    assert fake_starter.children[0].terminated
    with sessions() as session:
        run = session.get(Run, result.run_id)
    assert (run.status, run.error_text) == (RunStatus.FAILED, INTERRUPTED_ERROR)
    assert run.finished_at is not None
