from datetime import UTC, datetime

import pytest

OLD = "2025-12-31T00:00:00+00:00"


@pytest.fixture
def playlists(fake_api, make_playlist):
    fake_api.playlists = [make_playlist("PL1", "One"), make_playlist("PL2", "Two")]


@pytest.mark.usefixtures("playlists")
class TestParse:
    def test_channel_that_no_rule_can_match_gets_no_activities_request(
        self, fake_api, make_manager, make_subscription, start_date
    ):
        fake_api.subscriptions = [make_subscription("UC1", "Target"), make_subscription("UC9", "Other")]
        manager = make_manager([{"channel_id": "UC1", "playlist_id": "PL1"}], start_date)
        manager.parse()
        assert fake_api.activities_requests == ["UC1"]

    def test_channel_name_rule_selects_the_channels_to_request(
        self, fake_api, make_manager, make_subscription, start_date
    ):
        fake_api.subscriptions = [make_subscription("UC1", "Target Chan"), make_subscription("UC9", "Other Target")]
        manager = make_manager([{"channel_name": "Target", "playlist_id": "PL1"}], start_date)
        manager.parse()
        assert fake_api.activities_requests == ["UC1"]

    def test_title_pattern_rule_requests_the_activities_of_every_channel(
        self, fake_api, make_manager, make_subscription, start_date
    ):
        fake_api.subscriptions = [make_subscription("UC1"), make_subscription("UC9")]
        manager = make_manager([{"video_title_pattern": "x", "playlist_id": "PL1"}], start_date)
        manager.parse()
        assert fake_api.activities_requests == ["UC1", "UC9"]

    def test_matching_upload_is_added_to_the_playlist(
        self, fake_api, make_manager, make_subscription, make_activity, start_date
    ):
        fake_api.subscriptions = [make_subscription("UC1")]
        fake_api.activities_by_channel = {"UC1": [make_activity(video_id="a")]}
        manager = make_manager([{"channel_id": "UC1", "playlist_id": "PL1"}], start_date)
        manager.parse()
        assert fake_api.added == [("a", "PL1")]

    def test_durations_are_requested_only_for_in_window_videos_that_match_a_duration_rule(
        self, fake_api, make_manager, make_subscription, make_activity, start_date
    ):
        fake_api.subscriptions = [make_subscription("UC1")]
        fake_api.activities_by_channel = {
            "UC1": [
                make_activity(video_id="a", title="Keep A"),
                make_activity(video_id="b", title="Other B"),
                make_activity(video_id="c", title="Keep C", published_at=OLD),
            ],
        }
        fake_api.durations = {"a": 50, "b": 50, "c": 50}
        rules = [{"video_title_pattern": "^Keep", "video_duration_max": 100, "playlist_id": "PL1"}]
        manager = make_manager(rules, start_date)
        manager.parse()
        assert fake_api.durations_requests == [["a"]]
        assert fake_api.added == [("a", "PL1")]

    def test_the_duration_of_a_matching_video_decides_whether_it_is_added(
        self, fake_api, make_manager, make_subscription, make_activity, start_date
    ):
        fake_api.subscriptions = [make_subscription("UC1")]
        fake_api.activities_by_channel = {"UC1": [make_activity(video_id="a"), make_activity(video_id="b")]}
        fake_api.durations = {"a": 50, "b": 500}
        manager = make_manager([{"channel_id": "UC1", "video_duration_min": 100, "playlist_id": "PL1"}], start_date)
        manager.parse()
        assert fake_api.durations_requests == [["a", "b"]]
        assert fake_api.added == [("b", "PL1")]

    def test_durations_are_not_requested_for_a_video_that_matches_only_rules_without_a_duration_filter(
        self, fake_api, make_manager, make_subscription, make_activity, start_date
    ):
        fake_api.subscriptions = [make_subscription("UC1")]
        fake_api.activities_by_channel = {"UC1": [make_activity(video_id="a")]}
        rules = [
            {"channel_id": "UC1", "playlist_id": "PL1"},
            {"channel_id": "UC9", "video_duration_min": 100, "playlist_id": "PL2"},
        ]
        manager = make_manager(rules, start_date)
        manager.parse()
        assert fake_api.durations_requests == []
        assert fake_api.added == [("a", "PL1")]

    def test_durations_are_not_requested_when_no_rule_has_a_duration_filter(
        self, fake_api, make_manager, make_subscription, make_activity, start_date
    ):
        fake_api.subscriptions = [make_subscription("UC1")]
        fake_api.activities_by_channel = {"UC1": [make_activity(video_id="a"), make_activity(video_id="b")]}
        manager = make_manager([{"channel_id": "UC1", "playlist_id": "PL1"}], start_date)
        manager.parse()
        assert fake_api.durations_requests == []
        assert fake_api.added == [("a", "PL1"), ("b", "PL1")]

    def test_non_upload_activities_are_ignored(
        self, fake_api, make_manager, make_subscription, make_activity, start_date
    ):
        fake_api.subscriptions = [make_subscription("UC1")]
        fake_api.activities_by_channel = {
            "UC1": [
                make_activity(video_id="liked", activity_type="like"),
                make_activity(video_id="uploaded"),
                make_activity(video_id="shared", activity_type="social"),
            ],
        }
        fake_api.durations = {"liked": 100, "uploaded": 100, "shared": 100}
        manager = make_manager([{"channel_id": "UC1", "video_duration_min": 1, "playlist_id": "PL1"}], start_date)
        manager.parse()
        assert fake_api.durations_requests == [["uploaded"]]
        assert fake_api.added == [("uploaded", "PL1")]

    def test_channel_with_only_non_upload_activities_adds_nothing(
        self, fake_api, make_manager, make_subscription, make_activity, start_date
    ):
        fake_api.subscriptions = [make_subscription("UC1")]
        fake_api.activities_by_channel = {"UC1": [make_activity(video_id="liked", activity_type="like")]}
        manager = make_manager([{"channel_id": "UC1", "playlist_id": "PL1"}], start_date)
        manager.parse()
        assert fake_api.added == []

    def test_start_date_is_saved_after_the_run(self, fake_api, fake_db, make_manager, start_date):
        manager = make_manager([{"channel_id": "UC1", "playlist_id": "PL1"}], start_date)
        before = datetime.now(UTC)
        manager.parse()
        after = datetime.now(UTC)
        assert before <= fake_db.config.last_update <= after
        assert manager.start_date == fake_db.config.last_update
        assert fake_db.save_config_calls == 1
        assert fake_db.commit_calls == 1
