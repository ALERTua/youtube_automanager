import datetime
import io
import logging
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

import pytest
from nicegui import Client
from nicegui.testing.general import nicegui_reset_globals
from sqlalchemy import select
from ui_support import running_service

from youtube_automanager.db import make_engine, make_session_factory, upgrade_to_head
from youtube_automanager.db.models import (
    AddedVideo,
    Channel,
    Playlist,
    Rule,
    RuleChannel,
    Run,
    RunAction,
    RunStatus,
    RunTrigger,
    SeenVideo,
    Setting,
)
from youtube_automanager.engine.sync import PROCESSING_MODE_KEY, SyncRun
from youtube_automanager.rotation import CookieRotation
from youtube_automanager.runner import SYNC_LOCK
from youtube_automanager.settings import Settings
from youtube_automanager.youtube.ytdlp_updater import UpdateResult, installed_version, previous_dir


class FakeYtdlpInstall:
    """Replaces the install of a yt-dlp release, so no test reads PyPI or runs uv. Records each call.

    Each call takes its outcome from ``plan``: ``{"version": ...}`` puts a fake release of that version into the folder,
    and ``{"error": ..., "version": ...}`` fails the update and leaves the folder as it is. With an empty plan, the
    folder stays as it is and counts as up to date. A version that equals ``skipped`` installs nothing. As the real
    update does, a new version keeps the replaced release in ``<folder>.previous``. A set ``release`` event holds each
    call until it is set. ``options`` records the check and the skipped version of each call.
    """

    def __init__(self):
        self.plan = []
        self.calls = []
        self.options = []
        # The sync lock of the runner, and whether it was taken at each call.
        self.lock = SYNC_LOCK
        self.lock_taken = []
        self.started = threading.Event()
        self.release = None

    def __call__(self, target_dir, channel, *, smoke=None, skipped=None):
        self.calls.append((target_dir, channel))
        self.options.append({"smoke": smoke, "skipped": skipped})
        self.lock_taken.append(self.lock.locked())
        self.started.set()
        if self.release is not None:
            self.release.wait(10)
        old_version = installed_version(target_dir)
        outcome = self.plan.pop(0) if self.plan else {"version": old_version}
        new_version = outcome.get("version")
        if "error" in outcome:
            return UpdateResult(False, channel, old_version, new_version, outcome["error"])
        if new_version != old_version and new_version == skipped:
            return UpdateResult(True, channel, old_version, new_version, None, skipped=True)
        if new_version != old_version:
            previous = previous_dir(target_dir)
            if old_version is not None:
                shutil.rmtree(previous, ignore_errors=True)
                target_dir.rename(previous)
            self.write_release(target_dir, new_version)
        return UpdateResult(True, channel, old_version, new_version, None)

    @staticmethod
    def write_release(target_dir, version):
        """Put a fake release of ``version`` into ``target_dir``: the metadata and a module with that version."""
        shutil.rmtree(target_dir, ignore_errors=True)
        dist_info = target_dir / f"yt_dlp-{version}.dist-info"
        dist_info.mkdir(parents=True)
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.4\nName: yt-dlp\nVersion: {version}\n", encoding="utf-8"
        )
        package = target_dir / "yt_dlp"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "version.py").write_text(f"__version__ = {version!r}\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def ytdlp_install(monkeypatch):
    """Replaces the install of a yt-dlp release in the service with a ``FakeYtdlpInstall``, and returns the fake.

    The service plans a daily update, so a test that runs the service at that time would read PyPI and run uv.
    """
    fake = FakeYtdlpInstall()
    monkeypatch.setattr("youtube_automanager.web.app.update_ytdlp", fake)
    return fake


class RefusingTransport(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    """Replaces the HTTP and the HTTPS transport of urllib. It records the URL of each request and refuses it."""

    def __init__(self, urls):
        super().__init__()
        self.urls = urls

    def _refuse(self, request):
        self.urls.append(request.full_url)
        raise urllib.error.URLError("a test sends no request to the network")

    http_open = https_open = _refuse


class OfflineRotations:
    """Builds the cookie rotation of the service on a ``RefusingTransport``, and records each rotation that it builds.

    ``refused`` holds the URL of each request that a rotation tried to send.
    """

    def __init__(self):
        self.rotations = []
        self.refused = []

    def __call__(self, sessions, settings):
        rotation = CookieRotation(sessions, settings, handlers=[RefusingTransport(self.refused)])
        self.rotations.append(rotation)
        return rotation


@pytest.fixture(autouse=True)
def rotation_offline(monkeypatch):
    """Gives the service a cookie rotation that sends no request, and returns the ``OfflineRotations`` that builds it.

    The rotation is on by default, so a test that runs the service with stored cookies long enough would send requests
    to accounts.youtube.com.
    """
    fake = OfflineRotations()
    monkeypatch.setattr("youtube_automanager.web.app.CookieRotation", fake)
    return fake


@pytest.fixture(autouse=True)
def history_cleanup_off(monkeypatch):
    """Stops the service from cleaning the history at its start.

    The cleanup deletes each run that is older than the retention period, so a test that seeds a run with a fixed date
    and then starts the service would depend on the clock. A test of the cleanup puts the real function back with
    ``monkeypatch.setattr(youtube_automanager.web.app, "clean_history", clean_history)``.
    """
    monkeypatch.setattr("youtube_automanager.web.app.clean_history", lambda sessions: None)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def logging_setups(monkeypatch):
    """Records the logging setup of the lifespan instead of a change of the handlers that pytest captures."""
    calls = []
    monkeypatch.setattr("youtube_automanager.web.app.setup_logging", calls.append)
    return calls


@pytest.fixture
def no_logged_errors(caplog):
    """Fails a test when the service logged an error. NiceGUI logs an error of a page or a timer instead of a raise."""
    yield
    errors = [record.getMessage() for record in caplog.get_records("call") if record.levelno >= logging.ERROR]
    assert not errors, f"The service logged errors: {errors}"


@pytest.fixture
def settings(tmp_path):
    home = tmp_path / "data"
    return Settings(
        home=home,
        database_url=f"sqlite:///{home / 'service.sqlite'}",
        ytdlp_dir=home / "yt-dlp",
        tz="Europe/Kyiv",
    )


@pytest.fixture(scope="session")
def migrated_database(tmp_path_factory):
    """A SQLite file with the newest schema, made once for the test session.

    The migrations of a fresh database take about 0.15 s, and a copy of this file takes less than 1 ms. The tests of the
    migrations themselves still migrate their own databases.
    """
    path = tmp_path_factory.mktemp("template") / "migrated.sqlite"
    upgrade_to_head(f"sqlite:///{path}")
    return path


@pytest.fixture
def new_database(migrated_database):
    """Return a function that copies the migrated database to a path and returns the URL of the copy."""

    def copy(path):
        url = f"sqlite:///{path}"
        if path.exists():
            # The service of the test, or another fixture, made this database already, and it holds their rows. A copy
            # would replace them, and the migrations of a database at head take a few milliseconds.
            upgrade_to_head(url)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(migrated_database, path)
        return url

    return copy


@pytest.fixture
def sessions(settings, new_database):
    """Sessions of the database of ``settings``, with the newest schema."""
    new_database(settings.home / "service.sqlite")
    engine = make_engine(settings.database_url)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
async def user(nicegui_globals, settings, fake_starter):
    async with running_service(settings, fake_starter) as user:
        yield user


@pytest.fixture
def nicegui_globals():
    """Give a test that mounts the pages a clean global state of NiceGUI.

    NiceGUI keeps one global application. ``mount_ui`` adds middleware to it, and Starlette refuses that after the
    application served a request. At its end, the reset of NiceGUI also removes the module of each page function and
    its parent packages from ``sys.modules``, so that a NiceGUI main file registers its pages again. The service
    registers its pages in ``mount_ui``, so the fixture empties the list of page functions first, and the modules of
    the service stay.
    """
    with nicegui_reset_globals():
        try:
            yield
        finally:
            Client.page_routes.clear()


class FakeChild:
    """Replaces the child process of a run.

    A child that does not block ends at once with ``exit_code``. A blocking child ends at ``finish``, ``terminate``
    or ``kill``.
    """

    TERMINATED_CODE = -15
    KILLED_CODE = -9

    def __init__(self, exit_code=0, stderr="", blocking=False):
        self.stderr = io.StringIO(stderr)
        self.returncode = None
        self.terminated = False
        self.killed = False
        self._exit_code = exit_code
        self._ended = threading.Event()
        if not blocking:
            self.finish(exit_code)

    def finish(self, exit_code=None):
        if not self._ended.is_set():
            self.returncode = self._exit_code if exit_code is None else exit_code
            self._ended.set()

    def wait(self, timeout=None):
        if not self._ended.wait(timeout):
            raise subprocess.TimeoutExpired("fake child", timeout)
        return self.returncode

    def poll(self):
        return self.returncode if self._ended.is_set() else None

    def terminate(self):
        self.terminated = True
        self.finish(self.TERMINATED_CODE)

    def kill(self):
        self.killed = True
        self.finish(self.KILLED_CODE)


class FakeStarter:
    """Replaces the start of the child process and records each start.

    Each start takes the keyword arguments of the next ``FakeChild`` from ``plan``. With an empty plan, the child ends
    at once with exit code 0. A set ``error`` is raised instead of a start. ``calls`` holds the command and the
    environment of each start, and ``inputs`` its input text, or None.
    """

    def __init__(self):
        self.plan = []
        self.error = None
        self.calls = []
        self.inputs = []
        self.children = []
        self.child_started = threading.Event()

    def __call__(self, command, env, input_text=None):
        if self.error is not None:
            raise self.error
        self.calls.append((list(command), dict(env)))
        self.inputs.append(input_text)
        child = FakeChild(**(self.plan.pop(0) if self.plan else {}))
        self.children.append(child)
        self.child_started.set()
        return child


@pytest.fixture
def fake_starter():
    return FakeStarter()


@pytest.fixture
def db_url(tmp_path, new_database):
    """A SQLite database with the newest schema."""
    return new_database(tmp_path / "service.sqlite")


@pytest.fixture
def db_sessions(db_url):
    engine = make_engine(db_url)
    yield make_session_factory(engine)
    engine.dispose()


class FakeSources:
    """Replaces the anonymous reads of yt-dlp: the uploads and the Shorts of each channel.

    ``uploads`` holds the entries of each channel, newest first. ``consumed`` counts the entries that the last read of
    a channel took, so a test sees where the read stopped.
    """

    def __init__(self):
        self.uploads = {}
        self.shorts = {}
        self.errors = {}
        self.page_errors = {}
        self.shorts_errors = {}
        self.consumed = {}
        self.upload_reads = []
        self.shorts_reads = []

    def read_uploads(self, channel_id):
        self.upload_reads.append(channel_id)
        if channel_id in self.errors:
            raise self.errors[channel_id]
        self.consumed[channel_id] = 0
        return self._iterate(channel_id)

    def _iterate(self, channel_id):
        failure = self.page_errors.get(channel_id)
        for index, entry in enumerate(self.uploads.get(channel_id, ())):
            if failure is not None and index == failure[0]:
                raise failure[1]
            self.consumed[channel_id] = index + 1
            yield entry

    def read_shorts(self, channel_id, limit):
        self.shorts_reads.append((channel_id, limit))
        if channel_id in self.shorts_errors:
            raise self.shorts_errors[channel_id]
        return set(self.shorts.get(channel_id, ()))


class FakeAccount:
    """Replaces the account client: serves the subscriptions feed and records each add and each try of an add."""

    def __init__(self):
        self.feed = []
        self.feed_error = None
        self.added = []
        self.tried = []
        self.add_errors = {}

    def iter_subscription_feed(self):
        if self.feed_error is not None:
            raise self.feed_error
        return iter(list(self.feed))

    def add_video(self, playlist_id, video_id):
        self.tried.append((playlist_id, video_id))
        error = self.add_errors.get((playlist_id, video_id))
        if error is not None:
            raise error
        self.added.append((playlist_id, video_id))


class SyncHarness:
    """Runs the sync engine on the test database with fake sources, a fake account and a fake clock."""

    def __init__(self, sessions):
        self.sessions = sessions
        self.sources = FakeSources()
        self.account = FakeAccount()
        self.account_error = None
        self.account_opens = 0
        self.session_deaths = []
        self.sent = []
        self.send_error = None
        self.now = datetime.datetime(2026, 10, 12, 18, 0, tzinfo=datetime.UTC)
        self.run_id = None
        self._position = 0

    def open_account(self):
        self.account_opens += 1
        if self.account_error is not None:
            raise self.account_error
        return self.account

    def send(self, notification):
        self.sent.append(notification)
        if self.send_error is not None:
            raise self.send_error

    def run(self):
        with self.sessions.begin() as session:
            run = Run(status=RunStatus.RUNNING, trigger=RunTrigger.MANUAL)
            session.add(run)
            session.flush()
            self.run_id = run.id
        self.sent.clear()
        sync = SyncRun(
            self.sessions,
            self.run_id,
            read_uploads=self.sources.read_uploads,
            read_shorts=self.sources.read_shorts,
            open_account=self.open_account,
            send=self.send,
            session_died=self.session_deaths.append,
            clock=lambda: self.now,
        )
        try:
            return sync.run()
        finally:
            self.now += datetime.timedelta(hours=1)

    # The setup.

    def playlist(self, playlist_id="WL", title=None):
        with self.sessions.begin() as session:
            if session.get(Playlist, playlist_id) is None:
                session.add(Playlist(playlist_id=playlist_id, title=title or f"Playlist {playlist_id}", is_own=True))

    def channel(self, channel_id, title=None):
        with self.sessions.begin() as session:
            if session.get(Channel, channel_id) is None:
                session.add(Channel(channel_id=channel_id, title=title or f"Channel {channel_id}", is_subscribed=True))

    def rule(self, name, condition, *, channels=(), target="WL", no_shorts=False, enabled=True):
        self.playlist(target)
        for channel_id in channels:
            self.channel(channel_id)
        self._position += 1
        with self.sessions.begin() as session:
            rule = Rule(
                name=name,
                enabled=enabled,
                position=self._position,
                condition=condition,
                target_playlist_id=target,
                no_shorts=no_shorts,
            )
            session.add(rule)
            session.flush()
            session.add_all(RuleChannel(rule_id=rule.id, channel_id=channel_id) for channel_id in channels)
            return rule.id

    def unlink(self, rule_id, channel_id):
        with self.sessions.begin() as session:
            session.delete(session.get(RuleChannel, (rule_id, channel_id)))

    def disable(self, rule_id):
        with self.sessions.begin() as session:
            session.get(Rule, rule_id).enabled = False

    def link(self, rule_id, channel_id):
        self.channel(channel_id)
        with self.sessions.begin() as session:
            session.add(RuleChannel(rule_id=rule_id, channel_id=channel_id))

    def mode(self, mode, count=None):
        value = {"mode": mode} if count is None else {"mode": mode, "count": count}
        self.setting(PROCESSING_MODE_KEY, value)

    def setting(self, key, value):
        with self.sessions.begin() as session:
            row = session.get(Setting, key)
            if row is None:
                session.add(Setting(key=key, value=value))
            else:
                row.value = value

    # The reads.

    def seen(self, source):
        """The stored videos of a source as ``{video_id: state}``."""
        with self.sessions() as session:
            rows = session.scalars(select(SeenVideo).where(SeenVideo.channel_id == source))
            return {row.video_id: row.state for row in rows}

    def seen_rows(self, source):
        with self.sessions() as session:
            return {
                row.video_id: row for row in session.scalars(select(SeenVideo).where(SeenVideo.channel_id == source))
            }

    def added_rows(self):
        """The ``added_video`` rows as ``{(playlist_id, video_id): rule_id}``."""
        with self.sessions() as session:
            return {(row.playlist_id, row.video_id): row.rule_id for row in session.scalars(select(AddedVideo))}

    def actions(self, run_id=None):
        """The actions of a run, the last run by default, as tuples of the kind, the video, the rule and the playlist."""
        with self.sessions() as session:
            rows = session.scalars(
                select(RunAction).where(RunAction.run_id == (run_id or self.run_id)).order_by(RunAction.id)
            )
            return [(row.action, row.video_id, row.rule_id, row.playlist_id) for row in rows]

    def action_rows(self, run_id=None):
        with self.sessions() as session:
            return session.scalars(
                select(RunAction).where(RunAction.run_id == (run_id or self.run_id)).order_by(RunAction.id)
            ).all()

    def run_row(self):
        with self.sessions() as session:
            return session.get(Run, self.run_id)

    def mark(self, channel_id):
        return self.channel_row(channel_id).mark_date

    def channel_row(self, channel_id):
        with self.sessions() as session:
            return session.get(Channel, channel_id)


@pytest.fixture
def sync(db_sessions):
    return SyncHarness(db_sessions)
