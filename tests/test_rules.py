import json

import pytest

from youtube_automanager.engine import rules
from youtube_automanager.engine.rules import (
    MATCH_ALL,
    MAX_UNKNOWN_LEAVES,
    VIDEO_FIELDS,
    RuleError,
    Verdict,
    evaluate,
    validate,
)

VIDEO = {
    "video_id": "vid1",
    "title": "4 Levels of Steak: Amateur to Food Scientist",
    "channel_id": "UC1",
    "duration_s": 600,
    "upload_date": "2026-10-01",
    "live_status": "not_live",
    "is_short": False,
}

KNOWN_TRUE = {"==": [{"var": "channel_id"}, "UC1"]}
KNOWN_FALSE = {"==": [{"var": "channel_id"}, "UC2"]}
UNKNOWN_DURATION = {">=": [{"var": "duration_s"}, 120]}


def video(**overrides):
    return {**VIDEO, **overrides}


def test_video_fields_are_the_keys_of_the_video_data():
    assert VIDEO_FIELDS == set(VIDEO)


@pytest.mark.parametrize(
    "value", [False, None, 0, 0.0, "", [], {}, True, 1, -1, 0.5, "0", "false", " ", [0], [[]], {"a": 1}]
)
def test_python_truth_of_a_result_agrees_with_the_engine(value):
    engine_truth = json.loads(rules._ENGINE.eval_str('{"!!": [{"var": "x"}]}', json.dumps({"x": value})))

    assert bool(value) is engine_truth


def test_verdict_values_are_strings():
    assert [str(v) for v in Verdict] == ["match", "no_match", "unknown"]


class TestOperators:
    @pytest.mark.parametrize(
        ("tree", "expected"),
        [
            ({"==": [{"var": "duration_s"}, 600]}, Verdict.MATCH),
            ({"==": [{"var": "duration_s"}, 601]}, Verdict.NO_MATCH),
            ({"!=": [{"var": "live_status"}, "was_live"]}, Verdict.MATCH),
            ({"!=": [{"var": "live_status"}, "not_live"]}, Verdict.NO_MATCH),
            ({"<": [{"var": "duration_s"}, 601]}, Verdict.MATCH),
            ({"<": [{"var": "duration_s"}, 600]}, Verdict.NO_MATCH),
            ({"<=": [{"var": "duration_s"}, 600]}, Verdict.MATCH),
            ({"<=": [{"var": "duration_s"}, 599]}, Verdict.NO_MATCH),
            ({">": [{"var": "duration_s"}, 599]}, Verdict.MATCH),
            ({">": [{"var": "duration_s"}, 600]}, Verdict.NO_MATCH),
            ({">=": [{"var": "duration_s"}, 600]}, Verdict.MATCH),
            ({">=": [{"var": "duration_s"}, 601]}, Verdict.NO_MATCH),
            ({"in": [{"var": "channel_id"}, ["UC0", "UC1"]]}, Verdict.MATCH),
            ({"in": [{"var": "channel_id"}, ["UC0", "UC2"]]}, Verdict.NO_MATCH),
            ({"in": ["Steak", {"var": "title"}]}, Verdict.MATCH),
            ({"in": ["Pizza", {"var": "title"}]}, Verdict.NO_MATCH),
            ({"var": "title"}, Verdict.MATCH),
            ({"var": "is_short"}, Verdict.NO_MATCH),
            ({"var": ["is_short"]}, Verdict.NO_MATCH),
            ({"and": [KNOWN_TRUE, KNOWN_TRUE]}, Verdict.MATCH),
            ({"and": [KNOWN_TRUE, KNOWN_FALSE]}, Verdict.NO_MATCH),
            ({"or": [KNOWN_FALSE, KNOWN_TRUE]}, Verdict.MATCH),
            ({"or": [KNOWN_FALSE, KNOWN_FALSE]}, Verdict.NO_MATCH),
            ({"!": [KNOWN_FALSE]}, Verdict.MATCH),
            ({"!": KNOWN_TRUE}, Verdict.NO_MATCH),
            ({"!": {"var": "is_short"}}, Verdict.MATCH),
        ],
    )
    def test_operator(self, tree, expected):
        assert evaluate(tree, VIDEO) is expected

    def test_nested_groups(self):
        tree = {"and": [KNOWN_TRUE, {"or": [KNOWN_FALSE, {"!": [KNOWN_FALSE]}]}]}
        assert evaluate(tree, VIDEO) is Verdict.MATCH


class TestRegex:
    @pytest.mark.parametrize("pattern", ["Levels of", r"^\d Levels", "Scientist$", ".* Levels of .*"])
    def test_pattern_matches_anywhere_in_the_value(self, pattern):
        assert evaluate({"regex": [{"var": "title"}, pattern]}, VIDEO) is Verdict.MATCH

    def test_pattern_without_flags_is_case_sensitive(self):
        assert evaluate({"regex": [{"var": "title"}, "levels of"]}, VIDEO) is Verdict.NO_MATCH

    def test_flag_i_ignores_case(self):
        assert evaluate({"regex": [{"var": "title"}, "levels of", "i"]}, VIDEO) is Verdict.MATCH

    def test_flag_m_makes_the_anchors_match_at_each_line(self):
        tree = {"regex": [{"var": "title"}, "^second", "m"]}
        assert evaluate(tree, video(title="first\nsecond")) is Verdict.MATCH
        assert evaluate({"regex": [{"var": "title"}, "^second"]}, video(title="first\nsecond")) is Verdict.NO_MATCH

    def test_flag_s_makes_the_dot_match_a_line_break(self):
        tree = {"regex": [{"var": "title"}, "first.second", "s"]}
        assert evaluate(tree, video(title="first\nsecond")) is Verdict.MATCH
        assert evaluate({"regex": [{"var": "title"}, "first.second"]}, video(title="first\nsecond")) is Verdict.NO_MATCH

    def test_empty_flags_are_allowed(self):
        assert evaluate({"regex": [{"var": "title"}, "Levels", ""]}, VIDEO) is Verdict.MATCH

    @pytest.mark.parametrize(
        "tree",
        [
            {"regex": [{"var": "duration_s"}, "600"]},
            {"regex": [{"var": "is_short"}, "False"]},
            {"regex": [600, "600"]},
            {"regex": [["600"], "600"]},
        ],
    )
    def test_value_that_is_not_a_string_does_not_match(self, tree):
        assert evaluate(tree, VIDEO) is Verdict.NO_MATCH

    def test_compiled_patterns_are_cached(self):
        rules._compile.cache_clear()
        tree = {"regex": [{"var": "title"}, "Levels of a cached pattern"]}
        evaluate(tree, VIDEO)
        evaluate(tree, VIDEO)
        assert rules._compile.cache_info().misses == 1


class TestValidate:
    @pytest.mark.parametrize(
        "tree",
        [
            {"and": [KNOWN_TRUE, {"or": [UNKNOWN_DURATION, {"!": [KNOWN_FALSE]}]}]},
            {"regex": [{"var": "title"}, "x", "ims"]},
            {"in": [{"var": "channel_id"}, ["UC1"]]},
            {"var": ["duration_s", 0]},
            {"==": [1, 1]},
        ],
    )
    def test_valid_tree_passes(self, tree):
        validate(tree)

    @pytest.mark.parametrize("tree", [[KNOWN_TRUE], "title", 1, True, None])
    def test_root_that_is_not_an_object_is_refused(self, tree):
        with pytest.raises(RuleError, match="root"):
            validate(tree)

    @pytest.mark.parametrize("operator", ["===", "cat", "some", "if", "missing", "substr"])
    def test_operator_outside_the_allowed_set_is_refused(self, operator):
        with pytest.raises(RuleError, match="not allowed"):
            validate({"and": [KNOWN_TRUE, {operator: [1, 1]}]})

    def test_object_with_two_operators_is_refused(self):
        with pytest.raises(RuleError, match="exactly one key"):
            validate({"and": [KNOWN_TRUE], "or": [KNOWN_TRUE]})

    @pytest.mark.parametrize(
        "args", ["channel", "channel_title", "", "title.length", ["channel"], [], 1, {"var": "title"}]
    )
    def test_var_outside_the_video_fields_is_refused(self, args):
        with pytest.raises(RuleError, match="not a video field"):
            validate({"==": [{"var": args}, 1]})

    @pytest.mark.parametrize("pattern", ["(", "[a-", "*x", "(?P<n>a)(?P<n>b)"])
    def test_regex_pattern_that_does_not_compile_is_refused(self, pattern):
        with pytest.raises(RuleError, match="does not compile"):
            validate({"regex": [{"var": "title"}, pattern]})

    @pytest.mark.parametrize("flags", ["x", "ia", "I", "u", 1, None])
    def test_regex_flags_outside_ims_are_refused(self, flags):
        with pytest.raises(RuleError, match="flags"):
            validate({"regex": [{"var": "title"}, "x", flags]})

    @pytest.mark.parametrize("pattern", [1, None, {"var": "title"}])
    def test_regex_pattern_that_is_not_a_string_is_refused(self, pattern):
        with pytest.raises(RuleError, match="pattern must be a string"):
            validate({"regex": [{"var": "title"}, pattern]})

    @pytest.mark.parametrize("args", ["x", [{"var": "title"}], [{"var": "title"}, "x", "i", "extra"]])
    def test_regex_with_a_wrong_number_of_arguments_is_refused(self, args):
        with pytest.raises(RuleError, match="regex takes"):
            validate({"regex": args})

    def test_rule_error_is_a_value_error(self):
        assert issubclass(RuleError, ValueError)

    def test_evaluate_refuses_an_invalid_tree(self):
        with pytest.raises(RuleError, match="not allowed"):
            evaluate({"cat": ["a", "b"]}, VIDEO)


class TestThreeOutcomes:
    @pytest.mark.parametrize("missing", ["none", "absent"])
    def test_leaf_on_a_field_without_a_value_gives_unknown(self, missing):
        data = video(duration_s=None) if missing == "none" else {k: v for k, v in VIDEO.items() if k != "duration_s"}
        assert evaluate(UNKNOWN_DURATION, data) is Verdict.UNKNOWN

    @pytest.mark.parametrize(
        ("tree", "expected"),
        [
            ({"and": [KNOWN_FALSE, UNKNOWN_DURATION]}, Verdict.NO_MATCH),
            ({"and": [UNKNOWN_DURATION, KNOWN_FALSE]}, Verdict.NO_MATCH),
            ({"and": [KNOWN_TRUE, UNKNOWN_DURATION]}, Verdict.UNKNOWN),
            ({"or": [KNOWN_TRUE, UNKNOWN_DURATION]}, Verdict.MATCH),
            ({"or": [UNKNOWN_DURATION, KNOWN_TRUE]}, Verdict.MATCH),
            ({"or": [KNOWN_FALSE, UNKNOWN_DURATION]}, Verdict.UNKNOWN),
            ({"!": [UNKNOWN_DURATION]}, Verdict.UNKNOWN),
            ({"!": UNKNOWN_DURATION}, Verdict.UNKNOWN),
            ({"!": [{"and": [KNOWN_FALSE, UNKNOWN_DURATION]}]}, Verdict.MATCH),
            ({"and": [KNOWN_TRUE, {"or": [KNOWN_TRUE, UNKNOWN_DURATION]}]}, Verdict.MATCH),
        ],
    )
    def test_groups_with_one_unknown_leaf(self, tree, expected):
        assert evaluate(tree, video(duration_s=None)) is expected

    def test_leaf_that_reads_a_known_and_an_unknown_field_is_unknown(self):
        tree = {"or": [KNOWN_FALSE, {"==": [{"var": "duration_s"}, {"var": "video_id"}]}]}
        assert evaluate(tree, video(duration_s=None)) is Verdict.UNKNOWN

    def test_leaf_with_a_var_default_is_still_unknown(self):
        tree = {"and": [KNOWN_TRUE, {">=": [{"var": ["duration_s", 600]}, 120]}]}
        assert evaluate(tree, video(duration_s=None)) is Verdict.UNKNOWN

    def test_list_inside_a_group_that_reads_an_unknown_field_is_unknown(self):
        tree = {"or": [KNOWN_FALSE, [{"var": "duration_s"}]]}
        assert evaluate(tree, video(duration_s=None)) is Verdict.UNKNOWN

    def test_literal_inside_a_group_is_not_unknown(self):
        assert evaluate({"and": [True, KNOWN_TRUE]}, video(duration_s=None)) is Verdict.MATCH
        assert evaluate({"or": [False, UNKNOWN_DURATION]}, video(duration_s=None)) is Verdict.UNKNOWN

    def test_two_leaves_on_the_same_field_count_as_independent(self):
        tree = {"or": [{"<": [{"var": "duration_s"}, 120]}, {">=": [{"var": "duration_s"}, 120]}]}
        assert evaluate(tree, video(duration_s=None)) is Verdict.UNKNOWN

    def test_false_and_zero_are_values_not_unknown(self):
        assert evaluate({"==": [{"var": "is_short"}, False]}, video(is_short=False)) is Verdict.MATCH
        assert evaluate({"<": [{"var": "duration_s"}, 1]}, video(duration_s=0)) is Verdict.MATCH

    def test_unknown_leaf_in_a_false_branch_does_not_hide_a_true_branch(self):
        tree = {"or": [{"and": [KNOWN_FALSE, UNKNOWN_DURATION]}, {"regex": [{"var": "title"}, "Steak"]}]}
        assert evaluate(tree, video(duration_s=None)) is Verdict.MATCH


class TestUnknownLeafLimit:
    @staticmethod
    def tree_with_unknown_leaves(count):
        return {"or": [KNOWN_TRUE, *({"==": [{"var": "duration_s"}, n]} for n in range(count))]}

    def test_limit_is_eight(self):
        assert MAX_UNKNOWN_LEAVES == 8

    def test_up_to_the_limit_the_leaves_are_evaluated(self):
        tree = self.tree_with_unknown_leaves(MAX_UNKNOWN_LEAVES)
        assert evaluate(tree, video(duration_s=None)) is Verdict.MATCH

    def test_above_the_limit_the_result_is_unknown_without_evaluation(self, monkeypatch):
        def fail(*args):
            raise AssertionError("evaluated")

        monkeypatch.setattr(rules, "_is_true", fail)
        tree = self.tree_with_unknown_leaves(MAX_UNKNOWN_LEAVES + 1)
        assert evaluate(tree, video(duration_s=None)) is Verdict.UNKNOWN

    def test_above_the_limit_with_known_fields_the_tree_is_evaluated(self):
        tree = self.tree_with_unknown_leaves(MAX_UNKNOWN_LEAVES + 1)
        assert evaluate(tree, VIDEO) is Verdict.MATCH


class TestTreeThatReadsNoField:
    EMPTY_VIDEO = dict.fromkeys(VIDEO_FIELDS)

    @pytest.mark.parametrize("data", [VIDEO, EMPTY_VIDEO, {}])
    def test_true_tree_matches(self, data):
        assert evaluate({"==": [1, 1]}, data) is Verdict.MATCH

    @pytest.mark.parametrize("data", [VIDEO, EMPTY_VIDEO, {}])
    def test_false_tree_does_not_match(self, data):
        assert evaluate({"and": [{"==": [1, 1]}, {"in": ["x", ["a", "b"]]}]}, data) is Verdict.NO_MATCH


class TestMatchAll:
    @pytest.mark.parametrize("data", [VIDEO, dict.fromkeys(VIDEO_FIELDS), {}])
    def test_match_all_is_valid_and_matches_each_video(self, data):
        validate(MATCH_ALL)
        assert evaluate(MATCH_ALL, data) is Verdict.MATCH

    @pytest.mark.parametrize("tree", [{"and": []}, {"or": []}])
    def test_an_empty_group_matches_no_video(self, tree):
        assert evaluate(tree, VIDEO) is Verdict.NO_MATCH

    @pytest.mark.parametrize(
        ("is_short", "expected"), [(False, Verdict.MATCH), (True, Verdict.NO_MATCH), (None, Verdict.UNKNOWN)]
    )
    def test_match_all_with_the_shorts_check_of_a_rule_without_shorts(self, is_short, expected):
        tree = {"and": [MATCH_ALL, {"!": [{"var": "is_short"}]}]}
        assert evaluate(tree, video(is_short=is_short)) is expected


def test_failure_at_evaluation_raises_rule_error():
    with pytest.raises(RuleError, match="failed at evaluation"):
        evaluate({"<": [{"var": "title"}, 5]}, VIDEO)
