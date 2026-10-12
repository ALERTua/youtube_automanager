import datetime
import os
import subprocess
import sys
import threading

import pytest
from sqlalchemy import select

from youtube_automanager import notify
from youtube_automanager import runner as runner_module
from youtube_automanager.db.models import Account, CookieVersion, Run, RunStatus, RunTrigger, SessionState, Setting
from youtube_automanager.engine import account_session
from youtube_automanager.notify import Event
from youtube_automanager.runner import (
    BUSY_TEXT,
    ERROR_TAIL_LINES,
    ERROR_TEXT_MAX_CHARS,
    INTERRUPTED_ERROR,
    SOURCE_ROOT,
    SYNC_LOCK,
    UNAVAILABLE_EXIT_CODE,
    RunAlreadyActive,
    SessionCheckOutcome,
    SessionCheckResult,
    SyncRunner,
    check_session_once,
    error_text_from_tail,
    mark_interrupted_runs,
    run_once,
    sync_lock_held,
)
from youtube_automanager.settings import Settings
from youtube_automanager.youtube.account import AccountError, NotLoggedInError
from youtube_automanager.youtube.session import CookieCipher, generate_key, save_cookies

WAIT_SECONDS = 10
COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t4000000000\tSAPISID\tfake-sapisid\n"
    ".youtube.com\tTRUE\t/\tFALSE\t4000000000\tSIDCC\tfake-sidcc-0\n"
)


@pytest.fixture
def make_runner(db_sessions, db_url, tmp_path, fake_starter):
    def factory(**options):
        arguments = {
            "ytdlp_dir": tmp_path / "yt-dlp",
            "database_url": db_url,
            "child_timeout_s": WAIT_SECONDS,
            "start": fake_starter,
            "lock": threading.Lock(),
        }
        arguments.update(options)
        return SyncRunner(db_sessions, **arguments)

    return factory


@pytest.fixture
def runner(make_runner):
    return make_runner()


def read_runs(sessions):
    with sessions() as session:
        return session.scalars(select(Run).order_by(Run.id)).all()


def store_run(sessions, **values):
    with sessions.begin() as session:
        run = Run(**values)
        session.add(run)
        session.flush()
        return run.id


def test_claim_writes_a_running_row_with_the_trigger(runner, db_sessions):
    run_id = runner.claim(RunTrigger.MANUAL)

    (run,) = read_runs(db_sessions)
    assert run.id == run_id
    assert (run.status, run.trigger, run.finished_at, run.error_text) == (
        RunStatus.RUNNING,
        RunTrigger.MANUAL,
        None,
        None,
    )
    assert runner.is_active()


def test_execute_writes_succeeded_and_releases_the_lock(runner, db_sessions):
    run_id = runner.claim(RunTrigger.SCHEDULE)

    assert runner.execute(run_id) is RunStatus.SUCCEEDED

    (run,) = read_runs(db_sessions)
    assert (run.status, run.error_text) == (RunStatus.SUCCEEDED, None)
    assert run.started_at <= run.finished_at
    assert not runner.is_active()


def test_child_gets_the_command_and_the_environment(runner, fake_starter, db_url, tmp_path):
    run_id = runner.claim(RunTrigger.MANUAL)
    runner.execute(run_id)

    ((command, env),) = fake_starter.calls
    assert command == [sys.executable, "-m", "youtube_automanager", "run-once", "--run-id", str(run_id)]
    assert env["PYTHONPATH"].split(os.pathsep)[:2] == [str(tmp_path / "yt-dlp"), str(SOURCE_ROOT)]
    assert env["DATABASE_URL"] == db_url
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_failed_child_gives_the_end_of_its_stderr(runner, fake_starter, db_sessions, capsys):
    lines = [f"line {number}" for number in range(30)]
    fake_starter.plan.append({"exit_code": 1, "stderr": "".join(f"{line}\n" for line in lines)})

    assert runner.execute(runner.claim(RunTrigger.MANUAL)) is RunStatus.FAILED

    (run,) = read_runs(db_sessions)
    assert run.status is RunStatus.FAILED
    assert run.error_text == "\n".join(lines[-ERROR_TAIL_LINES:])
    assert run.finished_at is not None
    # Each stderr line also reaches the stderr of the service.
    assert "line 0\n" in capsys.readouterr().err


def test_long_stderr_keeps_its_end(runner, fake_starter, db_sessions):
    fake_starter.plan.append({"exit_code": 1, "stderr": "x" * 5000 + "\nValueError: the real cause\n"})

    runner.execute(runner.claim(RunTrigger.MANUAL))

    (run,) = read_runs(db_sessions)
    assert len(run.error_text) == ERROR_TEXT_MAX_CHARS
    assert run.error_text.startswith("...")
    assert run.error_text.endswith("ValueError: the real cause")


def test_a_short_child_gives_its_last_stderr_line_in_full_and_the_end_of_its_stderr(runner, fake_starter):
    result = "[" + "x" * 5000 + "]"
    fake_starter.plan.append({"stderr": f"a warning\n{result}\n\n  \n"})

    end = runner.run_short_child(["child"], "The test child", 10)

    assert end.returncode == 0
    assert end.last_line == result
    assert len(end.error_text) == ERROR_TEXT_MAX_CHARS


def test_a_short_child_without_stderr_has_an_empty_last_line(runner, fake_starter):
    fake_starter.plan.append({"stderr": "\n \n"})

    assert runner.run_short_child(["child"], "The test child", 10).last_line == ""


def test_failed_child_without_stderr_names_the_exit_code(runner, fake_starter, db_sessions):
    fake_starter.plan.append({"exit_code": 3})

    runner.execute(runner.claim(RunTrigger.MANUAL))

    (run,) = read_runs(db_sessions)
    assert run.error_text == "The sync process exited with code 3."


def test_child_that_does_not_end_in_time_is_killed(make_runner, fake_starter, db_sessions):
    runner = make_runner(child_timeout_s=0.05)
    fake_starter.plan.append({"blocking": True})

    assert runner.execute(runner.claim(RunTrigger.SCHEDULE)) is RunStatus.FAILED

    (run,) = read_runs(db_sessions)
    assert run.error_text == "The run did not end within 0.05 s, so the service stopped it."
    assert fake_starter.children[0].killed
    assert not runner.is_active()


def test_child_that_cannot_start_fails_the_run(runner, fake_starter, db_sessions):
    fake_starter.error = OSError("no such program")

    assert runner.execute(runner.claim(RunTrigger.MANUAL)) is RunStatus.FAILED

    (run,) = read_runs(db_sessions)
    assert run.error_text == "The service cannot start the sync process: no such program"
    assert not runner.is_active()


def test_unexpected_error_still_finishes_the_run(runner, fake_starter, db_sessions):
    fake_starter.error = RuntimeError("bug")

    assert runner.execute(runner.claim(RunTrigger.MANUAL)) is RunStatus.FAILED

    (run,) = read_runs(db_sessions)
    assert run.error_text == "The service cannot run the sync process: bug"
    assert not runner.is_active()


def test_claim_while_a_run_is_active_writes_nothing(runner, db_sessions):
    first = runner.claim(RunTrigger.SCHEDULE)

    assert runner.claim(RunTrigger.MANUAL) is None
    assert [run.id for run in read_runs(db_sessions)] == [first]

    runner.execute(first)

    assert runner.claim(RunTrigger.MANUAL) is not None


def test_scheduled_run_does_nothing_while_a_run_is_active(runner, fake_starter, db_sessions, monkeypatch):
    monkeypatch.setattr(runner_module, "SCHEDULED_WAIT_S", 0.05)
    first = runner.claim(RunTrigger.MANUAL)

    runner.run_scheduled()

    assert [run.id for run in read_runs(db_sessions)] == [first]
    assert fake_starter.calls == []


def test_scheduled_run_waits_for_a_short_holder_of_the_sync_lock(make_runner, fake_starter, db_sessions):
    lock = threading.Lock()
    runner = make_runner(lock=lock)
    lock.acquire()
    threading.Timer(0.3, lock.release).start()

    runner.run_scheduled()

    (run,) = read_runs(db_sessions)
    assert (run.trigger, run.status) == (RunTrigger.SCHEDULE, RunStatus.SUCCEEDED)
    assert len(fake_starter.calls) == 1


def test_a_stop_ends_the_wait_of_a_scheduled_run(make_runner, fake_starter, db_sessions):
    lock = threading.Lock()
    runner = make_runner(lock=lock)
    lock.acquire()
    try:
        worker = threading.Thread(target=runner.run_scheduled)
        worker.start()

        runner.stop(timeout=0.1)
        worker.join(WAIT_SECONDS)

        assert not worker.is_alive()
    finally:
        lock.release()
    assert read_runs(db_sessions) == []
    assert fake_starter.calls == []


def test_scheduled_run_writes_the_schedule_trigger(runner, db_sessions):
    runner.run_scheduled()

    (run,) = read_runs(db_sessions)
    assert (run.trigger, run.status) == (RunTrigger.SCHEDULE, RunStatus.SUCCEEDED)


def test_sync_lock_held_stops_runs_from_starting(make_runner, db_sessions):
    runner = make_runner(lock=SYNC_LOCK)

    with sync_lock_held(timeout=0):
        assert runner.claim(RunTrigger.MANUAL) is None

    assert read_runs(db_sessions) == []


def test_sync_lock_held_raises_while_a_run_is_active(make_runner):
    runner = make_runner(lock=SYNC_LOCK)
    run_id = runner.claim(RunTrigger.MANUAL)
    try:
        with pytest.raises(RunAlreadyActive), sync_lock_held(timeout=0):
            pass
    finally:
        runner.execute(run_id)

    with sync_lock_held(timeout=0):
        pass


def test_stop_terminates_the_active_child_and_fails_the_run(runner, fake_starter, db_sessions):
    fake_starter.plan.append({"blocking": True})
    run_id = runner.claim(RunTrigger.SCHEDULE)
    worker = threading.Thread(target=runner.execute, args=(run_id,))
    worker.start()
    assert fake_starter.child_started.wait(WAIT_SECONDS)

    assert runner.stop(timeout=WAIT_SECONDS) is True

    worker.join(WAIT_SECONDS)
    assert not worker.is_alive()
    assert fake_starter.children[0].terminated
    (run,) = read_runs(db_sessions)
    assert (run.status, run.error_text) == (RunStatus.FAILED, INTERRUPTED_ERROR)
    assert run.finished_at is not None
    # A stopped runner starts no more runs.
    assert runner.claim(RunTrigger.MANUAL) is None


def test_stop_before_the_child_starts_fails_the_run_without_a_start(runner, fake_starter, db_sessions):
    run_id = runner.claim(RunTrigger.MANUAL)
    with runner._state_lock:
        runner._stopping = True

    assert runner.execute(run_id) is RunStatus.FAILED

    assert fake_starter.calls == []
    (run,) = read_runs(db_sessions)
    assert run.error_text == INTERRUPTED_ERROR


def test_stop_without_an_active_run_returns_at_once(runner):
    assert runner.stop(timeout=WAIT_SECONDS) is True


def test_abandon_fails_the_run_and_releases_the_lock(runner, fake_starter, db_sessions):
    run_id = runner.claim(RunTrigger.MANUAL)

    runner.abandon(run_id, "cannot start")

    (run,) = read_runs(db_sessions)
    assert (run.status, run.error_text) == (RunStatus.FAILED, "cannot start")
    assert fake_starter.calls == []
    assert not runner.is_active()


def test_mark_interrupted_runs(db_sessions):
    first = store_run(db_sessions, status=RunStatus.RUNNING, trigger=RunTrigger.SCHEDULE)
    done = store_run(db_sessions, status=RunStatus.SUCCEEDED, trigger=RunTrigger.MANUAL)
    second = store_run(db_sessions, status=RunStatus.RUNNING, trigger=RunTrigger.MANUAL)

    assert mark_interrupted_runs(db_sessions) == 2

    runs = {run.id: run for run in read_runs(db_sessions)}
    for run_id in (first, second):
        assert (runs[run_id].status, runs[run_id].error_text) == (RunStatus.FAILED, INTERRUPTED_ERROR)
        assert runs[run_id].finished_at is None
    assert (runs[done].status, runs[done].error_text) == (RunStatus.SUCCEEDED, None)
    assert mark_interrupted_runs(db_sessions) == 0


def test_error_text_from_tail_strips_the_line_ends():
    assert error_text_from_tail(["first\n", "second \r\n", "\n"]) == "first\nsecond"
    assert error_text_from_tail([]) == ""


def test_run_once_finds_its_run_row(db_sessions, db_url, tmp_path):
    run_id = store_run(db_sessions, status=RunStatus.RUNNING, trigger=RunTrigger.MANUAL)
    settings = Settings(home=tmp_path, database_url=db_url, tz="UTC")

    assert run_once(run_id, settings) == 0
    assert run_once(run_id + 1, settings) == 1


def test_real_child_process_runs_the_sync(db_sessions, db_url, tmp_path, capfd):
    """Starts the real `run-once` child: the command, the import path and the database reach the child."""
    runner = SyncRunner(
        db_sessions,
        ytdlp_dir=tmp_path / "yt-dlp",
        database_url=db_url,
        child_timeout_s=90,
        lock=threading.Lock(),
    )

    run_id = runner.claim(RunTrigger.MANUAL)
    status = runner.execute(run_id)

    captured = capfd.readouterr()
    assert status is RunStatus.SUCCEEDED, captured.err
    assert f"Run {run_id} finished: 0 video(s) added" in captured.out
    (run,) = read_runs(db_sessions)
    assert (run.status, run.error_text) == (RunStatus.SUCCEEDED, None)


def test_run_once_writes_the_cause_of_a_stopped_run_to_stderr(db_sessions, db_url, tmp_path, capsys):
    run_id = store_run(db_sessions, status=RunStatus.RUNNING, trigger=RunTrigger.MANUAL)
    with db_sessions.begin() as session:
        session.add(Setting(key="processing_mode", value={"mode": "sometimes"}))
    settings = Settings(home=tmp_path, database_url=db_url, tz="UTC")

    assert run_once(run_id, settings) == 1

    err = capsys.readouterr().err
    assert (
        err.strip().splitlines()[-1].startswith("The setting 'processing_mode' is not valid: unknown mode 'sometimes'")
    )
    assert error_text_from_tail(err.splitlines()).endswith("use one of 'stop_at_seen', 'newest', 'mark_date'")


def test_the_parent_never_imports_yt_dlp():
    """Only the child of a run imports yt-dlp, so a yt-dlp update takes effect at the next run."""
    code = (
        "import sys\n"
        "import youtube_automanager.runner\n"
        "import youtube_automanager.web.app\n"
        "assert 'yt_dlp' not in sys.modules, sorted(name for name in sys.modules if name.startswith('yt_dlp'))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=SOURCE_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr


class TestFailureNotification:
    @pytest.fixture
    def sent(self):
        return []

    @pytest.fixture
    def runner(self, make_runner, sent):
        return make_runner(send=sent.append)

    def test_a_failed_child_sends_run_failed_with_the_error_text(self, runner, fake_starter, sent):
        fake_starter.plan.append({"exit_code": 1, "stderr": "YouTube does not accept the stored cookies\n"})

        runner.execute(runner.claim(RunTrigger.MANUAL))

        (notification,) = sent
        assert notification.event is Event.RUN_FAILED
        assert notification.body.endswith("YouTube does not accept the stored cookies")

    def test_a_child_that_times_out_sends_run_failed(self, make_runner, fake_starter, sent):
        runner = make_runner(child_timeout_s=0.05, send=sent.append)
        fake_starter.plan.append({"blocking": True})

        runner.execute(runner.claim(RunTrigger.SCHEDULE))

        assert [notification.event for notification in sent] == [Event.RUN_FAILED]
        assert "did not end within 0.05 s" in sent[0].body

    def test_a_succeeded_run_sends_nothing(self, runner, sent):
        runner.execute(runner.claim(RunTrigger.MANUAL))

        assert sent == []

    def test_a_run_that_the_stop_of_the_service_ended_sends_nothing(self, runner, sent):
        run_id = runner.claim(RunTrigger.MANUAL)
        with runner._state_lock:
            runner._stopping = True

        runner.execute(run_id)

        assert sent == []

    def test_a_failing_send_does_not_change_the_result(self, make_runner, fake_starter, db_sessions):
        def broken_send(notification):
            raise RuntimeError("the targets are broken")

        runner = make_runner(send=broken_send)
        fake_starter.plan.append({"exit_code": 2})

        assert runner.execute(runner.claim(RunTrigger.MANUAL)) is RunStatus.FAILED

        (run,) = read_runs(db_sessions)
        assert (run.status, run.error_text) == (RunStatus.FAILED, "The sync process exited with code 2.")
        assert not runner.is_active()

    def test_by_default_the_parent_sends_to_the_targets_of_the_database(self, make_runner, fake_starter, monkeypatch):
        calls = []
        monkeypatch.setattr(notify, "send_to_stored_targets", lambda sessions, notification: calls.append(notification))
        runner = make_runner()
        fake_starter.plan.append({"exit_code": 1, "stderr": "boom\n"})

        runner.execute(runner.claim(RunTrigger.MANUAL))

        assert [notification.event for notification in calls] == [Event.RUN_FAILED]


# The session check on request


NOW = datetime.datetime(2026, 10, 12, 18, 0, tzinfo=datetime.UTC)
LATER = datetime.datetime(2027, 10, 12, 6, 0, tzinfo=datetime.UTC)


class TestSessionCheck:
    def test_a_check_runs_the_check_child_and_gives_the_state_that_it_wrote(
        self, runner, fake_starter, db_sessions, db_url
    ):
        with db_sessions.begin() as session:
            session.add(Account(session_state=SessionState.EXPIRING, last_checked_at=NOW, earliest_cookie_expiry=LATER))

        result = runner.check_session()

        assert result == SessionCheckResult(SessionCheckOutcome.CHECKED, SessionState.EXPIRING, NOW, LATER, None)
        ((command, env),) = fake_starter.calls
        assert command == [sys.executable, "-m", "youtube_automanager", "check-session"]
        assert env["DATABASE_URL"] == db_url
        assert not runner.is_active()
        # A check is not a run.
        assert read_runs(db_sessions) == []

    def test_a_check_without_usable_cookies_names_the_cause(self, runner, fake_starter):
        fake_starter.plan.append(
            {"exit_code": UNAVAILABLE_EXIT_CODE, "stderr": "The service needs the YouTube account, but no cookies.\n"}
        )

        result = runner.check_session()

        assert result == SessionCheckResult(
            SessionCheckOutcome.UNAVAILABLE, detail="The service needs the YouTube account, but no cookies."
        )

    def test_a_failed_check_names_the_cause(self, runner, fake_starter):
        fake_starter.plan.append({"exit_code": 1, "stderr": "The session check failed: HTTP Error 503\n"})

        result = runner.check_session()

        assert (result.outcome, result.detail) == (
            SessionCheckOutcome.FAILED,
            "The session check failed: HTTP Error 503",
        )

    def test_a_check_while_a_run_is_active_starts_nothing(self, runner, fake_starter):
        run_id = runner.claim(RunTrigger.MANUAL)
        try:
            assert runner.check_session() == SessionCheckResult(SessionCheckOutcome.BUSY, detail=BUSY_TEXT)
        finally:
            runner.execute(run_id)

        assert len(fake_starter.calls) == 1

    def test_a_run_does_not_start_while_a_check_is_active(self, runner, fake_starter, db_sessions):
        fake_starter.plan.append({"blocking": True})
        results = []
        worker = threading.Thread(target=lambda: results.append(runner.check_session()))
        worker.start()
        assert fake_starter.child_started.wait(WAIT_SECONDS)

        assert runner.claim(RunTrigger.MANUAL) is None

        fake_starter.children[0].finish(0)
        worker.join(WAIT_SECONDS)
        assert results[0].outcome is SessionCheckOutcome.CHECKED
        assert read_runs(db_sessions) == []

    def test_a_check_that_does_not_end_in_time_is_killed(self, runner, fake_starter, monkeypatch):
        monkeypatch.setattr(runner_module, "CHECK_TIMEOUT_S", 0.05)
        fake_starter.plan.append({"blocking": True})

        result = runner.check_session()

        assert (result.outcome, result.detail) == (
            SessionCheckOutcome.FAILED,
            "The session check did not end within 0.05 s, so the service stopped it.",
        )
        assert fake_starter.children[0].killed
        assert not runner.is_active()

    def test_a_check_that_cannot_start_fails(self, runner, fake_starter):
        fake_starter.error = OSError("no such program")

        result = runner.check_session()

        assert (result.outcome, result.detail) == (
            SessionCheckOutcome.FAILED,
            "The service cannot start the session check: no such program",
        )

    def test_stop_terminates_an_active_check(self, runner, fake_starter):
        fake_starter.plan.append({"blocking": True})
        results = []
        worker = threading.Thread(target=lambda: results.append(runner.check_session()))
        worker.start()
        assert fake_starter.child_started.wait(WAIT_SECONDS)

        assert runner.stop(timeout=WAIT_SECONDS) is True

        worker.join(WAIT_SECONDS)
        assert fake_starter.children[0].terminated
        assert (results[0].outcome, results[0].detail) == (
            SessionCheckOutcome.FAILED,
            "The service stopped during the session check.",
        )


class TestCheckSessionOnce:
    @pytest.fixture
    def settings(self, tmp_path, db_url):
        return Settings(home=tmp_path, database_url=db_url, tz="UTC", cookies_key=generate_key())

    def store(self, db_sessions, settings):
        with db_sessions.begin() as session:
            save_cookies(session, CookieCipher(settings.cookies_key.get_secret_value()), COOKIES)

    def client(self, monkeypatch, check):
        calls = []

        class FakeClient:
            def __init__(self, text):
                self.text = text

            def check_session(self):
                calls.append("check")
                check()

            def cookies_text(self):
                return self.text.replace("fake-sidcc-0", "fake-sidcc-1")

        monkeypatch.setattr(account_session, "AccountClient", FakeClient)
        return calls

    def test_a_live_session_exits_with_0_and_saves_the_updated_cookies(self, settings, db_sessions, monkeypatch):
        self.store(db_sessions, settings)
        calls = self.client(monkeypatch, lambda: None)

        assert check_session_once(settings) == 0

        assert calls == ["check"]
        with db_sessions() as session:
            assert session.scalars(select(Account)).one().session_state is SessionState.OK
            assert len(session.scalars(select(CookieVersion)).all()) == 2

    def test_a_dead_session_exits_with_0(self, settings, db_sessions, monkeypatch):
        self.store(db_sessions, settings)

        def refuse():
            raise NotLoggedInError("YouTube answered as to an anonymous user")

        self.client(monkeypatch, refuse)

        assert check_session_once(settings) == 0
        with db_sessions() as session:
            assert session.scalars(select(Account)).one().session_state is SessionState.DEAD

    def test_a_failed_check_writes_its_cause_to_stderr(self, settings, db_sessions, monkeypatch, capsys):
        self.store(db_sessions, settings)

        def fail():
            raise AccountError("yt-dlp failed to check the session: HTTP Error 503")

        self.client(monkeypatch, fail)

        assert check_session_once(settings) == 1
        assert capsys.readouterr().err.strip().splitlines()[-1].endswith("HTTP Error 503")

    def test_without_cookies_the_child_exits_with_the_unavailable_code(self, settings, capsys):
        assert check_session_once(settings) == UNAVAILABLE_EXIT_CODE
        assert capsys.readouterr().err.strip().splitlines()[-1].endswith("Upload the YouTube cookies.")


def test_real_child_process_runs_the_session_check(db_sessions, db_url, tmp_path, capfd):
    """Starts the real `check-session` child: without stored cookies, it names the cause and sends no request."""
    runner = SyncRunner(
        db_sessions,
        ytdlp_dir=tmp_path / "yt-dlp",
        database_url=db_url,
        child_timeout_s=90,
        lock=threading.Lock(),
    )

    result = runner.check_session()

    captured = capfd.readouterr()
    assert result.outcome is SessionCheckOutcome.UNAVAILABLE, captured.err
    assert result.detail.startswith("The service needs the YouTube account, but")
