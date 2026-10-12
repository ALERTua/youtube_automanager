import io
import subprocess
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from nicegui import Client
from nicegui.testing.general import nicegui_reset_globals

from youtube_automanager.db import make_engine, make_session_factory, upgrade_to_head
from youtube_automanager.runners.automanage import YoutubeAutoManager

IN_WINDOW = "2026-01-02T00:00:00+00:00"


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
    at once with exit code 0. A set ``error`` is raised instead of a start.
    """

    def __init__(self):
        self.plan = []
        self.error = None
        self.calls = []
        self.children = []
        self.child_started = threading.Event()

    def __call__(self, command, env):
        if self.error is not None:
            raise self.error
        self.calls.append((list(command), dict(env)))
        child = FakeChild(**(self.plan.pop(0) if self.plan else {}))
        self.children.append(child)
        self.child_started.set()
        return child


@pytest.fixture
def fake_starter():
    return FakeStarter()


@pytest.fixture
def db_url(tmp_path):
    """A SQLite database with the newest schema."""
    url = f"sqlite:///{tmp_path / 'service.sqlite'}"
    upgrade_to_head(url)
    return url


@pytest.fixture
def db_sessions(db_url):
    engine = make_engine(db_url)
    yield make_session_factory(engine)
    engine.dispose()


class FakeYoutubeAPI:
    """Replaces the YouTube source of the manager: serves canned data and records each call that writes or fetches."""

    def __init__(self):
        self.playlists = []
        self.subscriptions = []
        self.activities_by_channel = {}
        self.durations = {}
        self.videos_in_playlist = set()
        self.added = []
        self.activities_requests = []
        self.durations_requests = []

    def get_playlist_by_id(self, playlist_id):
        return next((p for p in self.playlists if p.id == playlist_id), None)

    def get_playlists(self):
        return self.playlists

    def video_in_playlist(self, playlist_id, video_id):
        return (playlist_id, video_id) in self.videos_in_playlist

    def add_video_to_playlist(self, video_id, playlist_id):
        self.added.append((video_id, playlist_id))

    def get_subscriptions(self):
        return self.subscriptions

    def get_channel_activities(self, channel_id, **kwargs):
        self.activities_requests.append(channel_id)
        return SimpleNamespace(items=self.activities_by_channel.get(channel_id, []))

    def get_videos_durations(self, video_ids):
        self.durations_requests.append(list(video_ids))
        return {video_id: self.durations[video_id] for video_id in video_ids if video_id in self.durations}


class FakeDatabase:
    """Replaces DatabaseController: keeps the config row in memory and counts the saves."""

    def __init__(self):
        self.config = SimpleNamespace(last_update=None, refresh_token=None)
        self.save_config_calls = 0
        self.commit_calls = 0

    def save_config(self):
        self.save_config_calls += 1

    def commit(self):
        self.commit_calls += 1


@pytest.fixture
def start_date():
    return datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def fake_api():
    return FakeYoutubeAPI()


@pytest.fixture
def fake_db():
    return FakeDatabase()


@pytest.fixture
def make_manager(fake_api, fake_db):
    def factory(rules, start_date=None):
        config = SimpleNamespace(config={"rules": rules}, start_date=start_date)
        return YoutubeAutoManager(db=fake_db, config=config, yt_api=fake_api)

    return factory


@pytest.fixture
def make_activity():
    def factory(
        video_id="vid1",
        channel_id="UC1",
        channel_name="Chan",
        title="Title",
        published_at=IN_WINDOW,
        activity_type="upload",
    ):
        return SimpleNamespace(
            contentDetails=SimpleNamespace(upload=SimpleNamespace(videoId=video_id)),
            snippet=SimpleNamespace(
                channelId=channel_id,
                channelTitle=channel_name,
                title=title,
                publishedAt=published_at,
                type=activity_type,
            ),
        )

    return factory


@pytest.fixture
def make_playlist():
    def factory(playlist_id, title):
        return SimpleNamespace(id=playlist_id, snippet=SimpleNamespace(localized=SimpleNamespace(title=title)))

    return factory


@pytest.fixture
def make_subscription():
    def factory(channel_id, title="Chan"):
        return SimpleNamespace(snippet=SimpleNamespace(resourceId=SimpleNamespace(channelId=channel_id), title=title))

    return factory
