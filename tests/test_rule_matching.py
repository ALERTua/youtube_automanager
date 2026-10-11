import pytest

from youtube_automanager.runners.automanage import YoutubeAutoManager


def matches(rule, channel_id="UC1", channel_name="Chan Name", title="A Title", **kwargs):
    return YoutubeAutoManager._rule_matches_metadata(rule, "vid1", channel_id, channel_name, title, **kwargs)


def can_match(rule, channel_id="UC1", channel_name="Chan Name"):
    return YoutubeAutoManager._rule_can_match_channel(rule, channel_id, channel_name)


class TestRuleMatchesMetadata:
    def test_channel_id_as_string_matches(self):
        assert matches({"channel_id": "UC1"})

    def test_channel_id_as_list_matches_any_item(self):
        assert matches({"channel_id": ["UC0", "UC1"]})

    def test_channel_id_missing_from_list_does_not_match(self):
        assert not matches({"channel_id": ["UC0", "UC2"]})

    @pytest.mark.parametrize("rule_channel_id", ["somehandle", "@somehandle", "UC", "UC12", "uc1"])
    def test_channel_id_compares_exactly(self, rule_channel_id):
        assert not matches({"channel_id": rule_channel_id})

    def test_handle_never_matches_a_channel_id(self):
        assert not matches(
            {"channel_id": "somehandle"}, channel_id="UCabcdefghijklmnopqrstuv", channel_name="somehandle"
        )

    def test_channel_id_alone_matches_when_title_pattern_does_not(self):
        assert matches({"channel_id": "UC1", "video_title_pattern": "^nothing like this$"})

    def test_title_pattern_alone_matches_when_channel_id_does_not(self):
        assert matches({"channel_id": "UC9", "video_title_pattern": "Title"})

    def test_channel_name_alone_matches_when_channel_id_and_title_do_not(self):
        rule = {"channel_id": "UC9", "channel_name": "Chan", "video_title_pattern": "^nothing$"}
        assert matches(rule)

    @pytest.mark.parametrize("pattern", ["Chan", "Chan Name", "Chan.*", "C", "[A-Z]han"])
    def test_channel_name_matches_at_the_start_of_the_name(self, pattern):
        assert matches({"channel_name": pattern})

    @pytest.mark.parametrize("pattern", ["Name", "an N", "han", "Name$"])
    def test_channel_name_matching_only_in_the_middle_does_not_match(self, pattern):
        assert not matches({"channel_name": pattern})

    def test_channel_name_is_case_sensitive(self):
        assert not matches({"channel_name": "chan"})

    def test_channel_name_as_list_matches_any_item(self):
        assert matches({"channel_name": ["Other", "Chan"]})

    def test_channel_name_list_without_a_match_does_not_match(self):
        assert not matches({"channel_name": ["Other", "Name"]})

    @pytest.mark.parametrize("pattern", ["Review", "^My Review", "Video 42$", r"\d+"])
    def test_title_pattern_matches_anywhere_in_the_title(self, pattern):
        assert matches({"video_title_pattern": pattern}, title="My Review Video 42")

    @pytest.mark.parametrize("pattern", ["review", "REVIEW", "my review video"])
    def test_title_pattern_ignores_case(self, pattern):
        assert matches({"video_title_pattern": pattern}, title="My Review Video")

    def test_title_pattern_as_list_matches_any_item(self):
        assert matches({"video_title_pattern": ["nothing", "title"]})

    def test_title_pattern_without_a_match_does_not_match(self):
        assert not matches({"video_title_pattern": "nothing"})

    @pytest.mark.parametrize("rule", [{}, {"playlist_id": "PL1"}, {"video_duration_min": 10}])
    def test_rule_without_criteria_does_not_match(self, rule):
        assert not matches(rule)

    def test_log_flag_does_not_change_the_result(self):
        rule = {"channel_id": "UC9", "video_title_pattern": "title"}
        assert matches(rule, log=False)
        assert not matches({"channel_id": "UC9"}, log=False)


class TestRuleCanMatchChannel:
    @pytest.mark.parametrize(
        ("channel_id", "channel_name"),
        [("UC1", "Chan Name"), ("UC9", "Unrelated"), ("", "")],
    )
    def test_rule_with_title_pattern_can_match_any_channel(self, channel_id, channel_name):
        assert can_match({"video_title_pattern": "x", "channel_id": "UC0"}, channel_id, channel_name)

    def test_title_pattern_given_as_list_counts_as_a_pattern(self):
        assert can_match({"video_title_pattern": ["x", "y"]}, "UC9", "Unrelated")

    def test_empty_title_pattern_does_not_count_as_a_pattern(self):
        assert not can_match({"video_title_pattern": ""}, "UC9", "Unrelated")

    def test_channel_id_as_string_matches(self):
        assert can_match({"channel_id": "UC1"})

    def test_channel_id_as_list_matches_any_item(self):
        assert can_match({"channel_id": ["UC0", "UC1"]})

    def test_channel_id_without_a_match_does_not_match(self):
        assert not can_match({"channel_id": ["UC0", "UC2"]})

    def test_channel_name_matches_by_regex_at_the_start_of_the_name(self):
        assert can_match({"channel_name": "Chan.*"})

    def test_channel_name_as_list_matches_any_item(self):
        assert can_match({"channel_name": ["Other", "Chan"]})

    def test_channel_name_matching_only_in_the_middle_does_not_match(self):
        assert not can_match({"channel_name": "Name"})

    def test_rule_without_a_matching_criterion_gives_false(self):
        assert not can_match({"channel_id": "UC9", "channel_name": "Other"})

    @pytest.mark.parametrize("rule", [{}, {"playlist_id": "PL1"}])
    def test_rule_without_criteria_gives_false(self, rule):
        assert not can_match(rule)
