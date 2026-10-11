import pendulum
import pytest
import yaml

from youtube_automanager.config import YoutubeAutoManagerConfig

RULE = {"channel_id": "UC1", "playlist_id": "PL1"}


@pytest.fixture
def load(tmp_path):
    def factory(content):
        path = tmp_path / "config.yaml"
        path.write_text(content if isinstance(content, str) else yaml.safe_dump(content), encoding="utf-8")
        return YoutubeAutoManagerConfig(config_filepath=path)

    return factory


class TestRules:
    def test_valid_config_is_ok(self, load):
        config = {
            "rules": [{**RULE, "video_title_pattern": "x", "video_duration_min": 60, "video_duration_max": 600}],
        }
        assert load(config).ok is True

    @pytest.mark.parametrize(
        "rule",
        [
            {"channel_name": "Chan", "playlist_name": "One"},
            {"channel_id": ["UC1", "UC2"], "playlist_id": "PL1"},
            {**RULE, "video_title_pattern": "x"},
            {**RULE, "video_duration_min": 0},
            {**RULE, "video_duration_max": 600},
            {**RULE, "video_duration_min": 10.5, "video_duration_max": 20.5},
            {**RULE, "video_duration_min": 60, "video_duration_max": 60},
        ],
    )
    def test_valid_rule_is_ok(self, load, rule):
        assert load({"rules": [rule]}).ok is True

    @pytest.mark.parametrize(
        "durations",
        [
            {"video_duration_min": True},
            {"video_duration_max": True},
            {"video_duration_min": False},
            {"video_duration_min": -1},
            {"video_duration_max": -0.5},
            {"video_duration_min": "60"},
            {"video_duration_max": [60]},
            {"video_duration_min": 600, "video_duration_max": 60},
        ],
    )
    def test_invalid_duration_filter_is_not_ok(self, load, durations):
        assert load({"rules": [{**RULE, **durations}]}).ok is False

    def test_rule_with_only_a_title_pattern_is_not_ok(self, load):
        assert load({"rules": [{"video_title_pattern": "x", "playlist_id": "PL1"}]}).ok is False

    def test_rule_without_a_playlist_is_not_ok(self, load):
        assert load({"rules": [{"channel_id": "UC1"}]}).ok is False

    def test_one_invalid_rule_makes_the_config_not_ok(self, load):
        assert load({"rules": [RULE, {"playlist_id": "PL1"}]}).ok is False

    @pytest.mark.parametrize("content", [{"rules": []}, {"rules": None}, {}])
    def test_empty_rules_are_not_ok(self, load, content):
        assert load(content).ok is False


class TestStartDate:
    def test_start_date_is_parsed(self, load):
        config = load({"start_date": "2022-06-17T10:00:00+02:00", "rules": [RULE]})
        assert config.ok is True
        assert isinstance(config.start_date, pendulum.DateTime)
        assert config.start_date == pendulum.datetime(2022, 6, 17, 8, 0)

    def test_start_date_without_seconds_is_parsed_as_utc(self, load):
        config = load("start_date: 2022-06-17 00:00\nrules:\n  - channel_id: UC1\n    playlist_id: PL1\n")
        assert config.ok is True
        assert config.start_date == pendulum.datetime(2022, 6, 17)

    def test_start_date_stays_none_when_the_config_has_none(self, load):
        config = load({"rules": [RULE]})
        assert config.ok is True
        assert config.start_date is None

    def test_invalid_start_date_is_not_ok(self, load):
        config = load({"start_date": "not a date", "rules": [RULE]})
        assert config.ok is False
        assert config.start_date is None

    @pytest.mark.parametrize("value", ["2022-06-17", "2022-06-17T10:00:00+00:00"])
    def test_unquoted_date_that_yaml_reads_as_a_date_object_is_not_ok(self, load, value):
        config = load(f"start_date: {value}\nrules:\n  - channel_id: UC1\n    playlist_id: PL1\n")
        assert config.ok is False
        assert config.start_date is None

    def test_quoted_date_only_is_ok(self, load):
        config = load({"start_date": "2022-06-17", "rules": [RULE]})
        assert config.ok is True
        assert config.start_date == pendulum.datetime(2022, 6, 17)


class TestFile:
    def test_missing_file_is_not_ok(self, tmp_path):
        assert YoutubeAutoManagerConfig(config_filepath=tmp_path / "missing.yaml").ok is False

    def test_empty_file_is_not_ok(self, load):
        assert load("").ok is False
