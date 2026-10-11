from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from youtube_automanager.runners.automanage import YoutubeAutoManager

IN_WINDOW = "2026-01-02T00:00:00+00:00"


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
