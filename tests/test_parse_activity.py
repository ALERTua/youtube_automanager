import pytest


@pytest.fixture
def playlists(fake_api, make_playlist):
    fake_api.playlists = [make_playlist("PL1", "One"), make_playlist("PL2", "Two")]


@pytest.mark.usefixtures("playlists")
class TestParseActivity:
    def test_matching_video_is_added_to_the_playlist_of_the_rule(
        self, fake_api, make_manager, make_activity, start_date
    ):
        manager = make_manager([{"channel_id": "UC1", "playlist_id": "PL1"}])
        manager.parse_activity(make_activity(), start_date)
        assert fake_api.added == [("vid1", "PL1")]

    def test_video_that_matches_no_rule_is_not_added(self, fake_api, make_manager, make_activity, start_date):
        manager = make_manager([{"channel_id": "UC9", "playlist_id": "PL1"}])
        manager.parse_activity(make_activity(), start_date)
        assert fake_api.added == []

    def test_video_older_than_start_date_is_skipped(self, fake_api, make_manager, make_activity, start_date):
        manager = make_manager([{"channel_id": "UC1", "playlist_id": "PL1"}])
        manager.parse_activity(make_activity(published_at="2025-12-31T23:59:59+00:00"), start_date)
        assert fake_api.added == []

    def test_video_published_exactly_at_start_date_is_added(self, fake_api, make_manager, make_activity, start_date):
        manager = make_manager([{"channel_id": "UC1", "playlist_id": "PL1"}])
        manager.parse_activity(make_activity(published_at=start_date.isoformat()), start_date)
        assert fake_api.added == [("vid1", "PL1")]

    def test_video_already_in_the_playlist_is_not_added(self, fake_api, make_manager, make_activity, start_date):
        fake_api.videos_in_playlist = {("PL1", "vid1")}
        manager = make_manager([{"channel_id": "UC1", "playlist_id": "PL1"}])
        manager.parse_activity(make_activity(), start_date)
        assert fake_api.added == []

    def test_video_in_another_playlist_is_still_added(self, fake_api, make_manager, make_activity, start_date):
        fake_api.videos_in_playlist = {("PL2", "vid1")}
        manager = make_manager([{"channel_id": "UC1", "playlist_id": "PL1"}])
        manager.parse_activity(make_activity(), start_date)
        assert fake_api.added == [("vid1", "PL1")]

    def test_playlist_is_found_by_name_through_get_playlists(self, fake_api, make_manager, make_activity, start_date):
        manager = make_manager([{"channel_id": "UC1", "playlist_name": "Two"}])
        manager.parse_activity(make_activity(), start_date)
        assert fake_api.added == [("vid1", "PL2")]

    def test_playlist_id_wins_over_playlist_name(self, fake_api, make_manager, make_activity, start_date):
        manager = make_manager([{"channel_id": "UC1", "playlist_id": "PL1", "playlist_name": "Two"}])
        manager.parse_activity(make_activity(), start_date)
        assert fake_api.added == [("vid1", "PL1")]

    def test_rule_without_playlist_id_and_name_adds_nothing(self, fake_api, make_manager, make_activity, start_date):
        manager = make_manager([{"channel_id": "UC1"}])
        manager.parse_activity(make_activity(), start_date)
        assert fake_api.added == []

    @pytest.mark.parametrize("playlist", [{"playlist_id": "PL404"}, {"playlist_name": "Missing"}])
    def test_playlist_that_is_not_found_adds_nothing(self, fake_api, make_manager, make_activity, start_date, playlist):
        manager = make_manager([{"channel_id": "UC1", **playlist}])
        manager.parse_activity(make_activity(), start_date)
        assert fake_api.added == []

    def test_rule_without_playlist_does_not_stop_the_next_rule(self, fake_api, make_manager, make_activity, start_date):
        manager = make_manager([{"channel_id": "UC1"}, {"channel_id": "UC1", "playlist_id": "PL2"}])
        manager.parse_activity(make_activity(), start_date)
        assert fake_api.added == [("vid1", "PL2")]

    def test_video_that_matches_two_rules_is_added_to_both_playlists(
        self, fake_api, make_manager, make_activity, start_date
    ):
        rules = [
            {"channel_id": "UC1", "playlist_id": "PL1"},
            {"video_title_pattern": "title", "playlist_name": "Two"},
        ]
        manager = make_manager(rules)
        manager.parse_activity(make_activity(), start_date)
        assert fake_api.added == [("vid1", "PL1"), ("vid1", "PL2")]

    def test_config_without_rules_adds_nothing(self, fake_api, make_manager, make_activity, start_date):
        manager = make_manager([])
        del manager.config.config["rules"]
        manager.parse_activity(make_activity(), start_date)
        assert fake_api.added == []
