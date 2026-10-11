import pytest

from youtube_automanager.runners.automanage import YoutubeAutoManager

ADDED = [("vid1", "PL1")]


@pytest.fixture
def run(fake_api, make_manager, make_playlist, make_activity, start_date):
    """Run one activity of channel UC1 through a single rule and return the videos that were added."""
    fake_api.playlists = [make_playlist("PL1", "One")]

    def run_rule(rule, durations=None, **activity_kwargs):
        manager = make_manager([{"channel_id": "UC1", "playlist_id": "PL1", **rule}])
        manager.parse_activity(make_activity(**activity_kwargs), start_date, durations)
        return fake_api.added

    return run_rule


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ({"video_duration_min": 60}, True),
        ({"video_duration_max": 600}, True),
        ({"video_duration_min": 60, "video_duration_max": 600}, True),
        ({"video_duration_min": 0}, True),
        ({"video_duration_max": 0}, True),
        ({}, False),
        ({"channel_id": "UC1"}, False),
        ({"video_duration_min": None, "video_duration_max": None}, False),
    ],
)
def test_rule_has_duration_filter(rule, expected):
    assert YoutubeAutoManager._rule_has_duration_filter(rule) is expected


class TestDurationFilterInParseActivity:
    def test_video_shorter_than_min_is_not_added(self, run):
        assert run({"video_duration_min": 60}, {"vid1": 59}) == []

    def test_video_longer_than_max_is_not_added(self, run):
        assert run({"video_duration_max": 600}, {"vid1": 601}) == []

    def test_video_inside_the_range_is_added(self, run):
        assert run({"video_duration_min": 60, "video_duration_max": 600}, {"vid1": 300}) == ADDED

    @pytest.mark.parametrize("duration", [60, 600])
    def test_bounds_are_inclusive(self, run, duration):
        assert run({"video_duration_min": 60, "video_duration_max": 600}, {"vid1": duration}) == ADDED

    @pytest.mark.parametrize(("duration", "added"), [(59, False), (60, True), (100_000, True)])
    def test_min_only(self, run, duration, added):
        assert bool(run({"video_duration_min": 60}, {"vid1": duration})) is added

    @pytest.mark.parametrize(("duration", "added"), [(0, True), (600, True), (601, False)])
    def test_max_only(self, run, duration, added):
        assert bool(run({"video_duration_max": 600}, {"vid1": duration})) is added

    def test_zero_max_is_a_filter(self, run):
        assert run({"video_duration_max": 0}, {"vid1": 1}) == []

    def test_zero_max_keeps_a_zero_length_video(self, run):
        assert run({"video_duration_max": 0}, {"vid1": 0}) == ADDED

    def test_zero_min_keeps_a_zero_length_video(self, run):
        assert run({"video_duration_min": 0}, {"vid1": 0}) == ADDED

    @pytest.mark.parametrize("durations", [None, {}, {"other": 5}])
    def test_unknown_duration_skips_the_filter(self, run, durations):
        assert run({"video_duration_min": 60, "video_duration_max": 600}, durations) == ADDED

    def test_duration_is_taken_by_video_id(self, run):
        assert run({"video_duration_min": 60}, {"vid1": 30, "other": 300}) == []

    def test_video_that_does_not_match_the_rule_is_not_added_even_when_in_range(self, run):
        rule = {"channel_id": "UC9", "video_duration_min": 60}
        assert run(rule, {"vid1": 300}) == []

    def test_filter_of_one_rule_does_not_stop_another_rule(
        self, fake_api, make_manager, make_playlist, make_activity, start_date
    ):
        fake_api.playlists = [make_playlist("PL1", "One"), make_playlist("PL2", "Two")]
        rules = [
            {"channel_id": "UC1", "playlist_id": "PL1", "video_duration_min": 600},
            {"channel_id": "UC1", "playlist_id": "PL2"},
        ]
        manager = make_manager(rules)
        manager.parse_activity(make_activity(), start_date, {"vid1": 30})
        assert fake_api.added == [("vid1", "PL2")]
