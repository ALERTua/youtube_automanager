import copy

import pytest
from test_live_rules import EPICURIOUS, EPICURIOUS_LEVELS_RULE, HOME_CHANNELS, HOME_RULE, WATCH_LATER_RULE

from youtube_automanager import i18n
from youtube_automanager.engine.rules import MATCH_ALL, VIDEO_FIELDS, RuleError, Verdict, evaluate, validate
from youtube_automanager.web.ui.rule_form import (
    FIELDS,
    LIVE_STATUSES,
    OPERATORS,
    DurationInput,
    Group,
    GroupKind,
    Leaf,
    ValueKind,
    change_field,
    change_operator,
    condition_from_json,
    condition_of,
    describe,
    duration_fields,
    duration_value,
    format_duration,
    from_tree,
    json_text,
    leaf_error,
    new_leaf,
    to_tree,
    value_kind,
)

A = "UCaaaaaaaaaaaaaaaaaaaaaa"
B = "UCbbbbbbbbbbbbbbbbbbbbbb"
TITLE = {"var": "title"}
DURATION = {"var": "duration_s"}
DATE = {"var": "upload_date"}
LONG = {">=": [DURATION, 120]}
DURATION_ERROR = "Enter whole minutes, 0 or more, and whole seconds from 0 to 59."
LEVELS = {"regex": [TITLE, "Levels of", "i"]}
FROM_A = {"==": [{"var": "channel_id"}, A]}

VIDEO = {
    "video_id": "vid1",
    "title": "4 Levels of Steak",
    "channel_id": A,
    "duration_s": 600,
    "upload_date": "2026-10-01",
    "live_status": "not_live",
    "is_short": False,
}


def video(**overrides):
    return {**VIDEO, **overrides}


def only(leaf: Leaf) -> Group:
    return Group(GroupKind.ALL, [leaf])


def tree_of(leaf: Leaf) -> dict:
    """The tree of a root group with ``leaf`` alone, without the root."""
    (tree,) = to_tree(only(leaf))["and"]
    return tree


def test_the_editor_offers_each_video_field():
    assert set(FIELDS) == set(OPERATORS) == VIDEO_FIELDS


class TestLeaves:
    @pytest.mark.parametrize(
        ("leaf", "tree"),
        [
            (Leaf("title", "regex", "Levels of"), {"regex": [TITLE, "Levels of", "i"]}),
            (Leaf("title", "regex", "Levels of", case_sensitive=True), {"regex": [TITLE, "Levels of"]}),
            (Leaf("title", "==", "A video"), {"==": [TITLE, "A video"]}),
            (Leaf("title", "!=", "A video"), {"!=": [TITLE, "A video"]}),
            (Leaf("video_id", "==", "vid1"), {"==": [{"var": "video_id"}, "vid1"]}),
            (Leaf("video_id", "regex", "^vid"), {"regex": [{"var": "video_id"}, "^vid", "i"]}),
            (Leaf("channel_id", "in", [A, B]), {"in": [{"var": "channel_id"}, [A, B]]}),
            (Leaf("channel_id", "==", A), {"==": [{"var": "channel_id"}, A]}),
            (Leaf("channel_id", "!=", A), {"!=": [{"var": "channel_id"}, A]}),
            (Leaf("channel_id", "regex", "^UCa"), {"regex": [{"var": "channel_id"}, "^UCa", "i"]}),
            (Leaf("duration_s", ">=", 120), {">=": [DURATION, 120]}),
            (Leaf("duration_s", ">", 120), {">": [DURATION, 120]}),
            (Leaf("duration_s", "<=", 120), {"<=": [DURATION, 120]}),
            (Leaf("duration_s", "<", 0), {"<": [DURATION, 0]}),
            (Leaf("duration_s", "==", 60), {"==": [DURATION, 60]}),
            (Leaf("duration_s", "!=", 60), {"!=": [DURATION, 60]}),
            (Leaf("upload_date", ">=", "2026-10-01"), {">=": [DATE, "2026-10-01"]}),
            (Leaf("upload_date", ">", "2026-10-01"), {">": [DATE, "2026-10-01"]}),
            (Leaf("upload_date", "<=", "2026-10-01"), {"<=": [DATE, "2026-10-01"]}),
            (Leaf("upload_date", "<", "2026-10-01"), {"<": [DATE, "2026-10-01"]}),
            (Leaf("upload_date", "==", "2026-10-01"), {"==": [DATE, "2026-10-01"]}),
            (Leaf("live_status", "==", "is_upcoming"), {"==": [{"var": "live_status"}, "is_upcoming"]}),
            (Leaf("live_status", "!=", "was_live"), {"!=": [{"var": "live_status"}, "was_live"]}),
            (Leaf("is_short", "==", True), {"==": [{"var": "is_short"}, True]}),
            (Leaf("is_short", "==", False), {"==": [{"var": "is_short"}, False]}),
        ],
    )
    def test_a_leaf_gives_its_tree_and_the_tree_gives_the_leaf(self, leaf, tree):
        assert to_tree(only(leaf)) == {"and": [tree]}
        assert from_tree({"and": [tree]}) == only(leaf)
        validate({"and": [tree]})

    @pytest.mark.parametrize(("field", "operator"), [(f, o) for f, operators in OPERATORS.items() for o in operators])
    def test_each_operator_of_each_field_has_a_leaf_form(self, field, operator):
        leaf = new_leaf(field)
        change_operator(leaf, operator)
        leaf.value = {
            ValueKind.TEXT: "x",
            ValueKind.CHANNEL: A,
            ValueKind.CHANNELS: [A],
            ValueKind.SECONDS: 5,
            ValueKind.DATE: "2026-10-01",
            ValueKind.LIVE_STATUS: "was_live",
            ValueKind.SHORT: False,
        }[value_kind(field, operator)]

        assert leaf_error(leaf) is None
        tree = to_tree(only(leaf))
        validate(tree)
        assert from_tree(tree) == only(leaf)

    @pytest.mark.parametrize(
        ("operator", "label", "matching", "other"),
        [
            ("<", "is before", "2026-09-30", "2026-10-01"),
            ("<=", "is on or before", "2026-10-01", "2026-10-02"),
            (">", "is after", "2026-10-02", "2026-10-01"),
            (">=", "is on or after", "2026-10-01", "2026-09-30"),
            ("==", "is on", "2026-10-01", "2026-10-02"),
        ],
    )
    def test_each_date_comparison_does_what_its_label_says(self, operator, label, matching, other):
        tree = to_tree(only(Leaf("upload_date", operator, "2026-10-01")))

        assert OPERATORS["upload_date"][operator] == label
        assert evaluate(tree, video(upload_date=matching)) is Verdict.MATCH
        assert evaluate(tree, video(upload_date=other)) is Verdict.NO_MATCH

    @pytest.mark.parametrize(
        ("operator", "label", "matching", "other"),
        [
            ("<", "is less than", 119, 120),
            ("<=", "is at most", 120, 121),
            (">", "is more than", 121, 120),
            (">=", "is at least", 120, 119),
            ("==", "is", 120, 121),
            ("!=", "is not", 121, 120),
        ],
    )
    def test_each_duration_comparison_does_what_its_label_says(self, operator, label, matching, other):
        tree = to_tree(only(Leaf("duration_s", operator, 120)))

        assert OPERATORS["duration_s"][operator] == label
        assert evaluate(tree, video(duration_s=matching)) is Verdict.MATCH
        assert evaluate(tree, video(duration_s=other)) is Verdict.NO_MATCH

    def test_a_pattern_ignores_the_case_unless_it_is_case_sensitive(self):
        loose = to_tree(only(Leaf("title", "regex", "levels OF")))
        strict = to_tree(only(Leaf("title", "regex", "levels OF", case_sensitive=True)))

        assert evaluate(loose, video(title="4 Levels of Steak")) is Verdict.MATCH
        assert evaluate(strict, video(title="4 Levels of Steak")) is Verdict.NO_MATCH
        assert evaluate(strict, video(title="4 levels OF Steak")) is Verdict.MATCH

    def test_a_pattern_finds_a_part_of_the_title(self):
        tree = to_tree(only(Leaf("title", "regex", "Levels")))

        assert evaluate(tree, video(title="4 Levels of Steak")) is Verdict.MATCH

    @pytest.mark.parametrize(("value", "expected"), [(True, Verdict.MATCH), (False, Verdict.NO_MATCH)])
    def test_the_shorts_leaf(self, value, expected):
        assert evaluate(to_tree(only(Leaf("is_short", "==", value))), video(is_short=True)) is expected

    def test_a_channel_list(self):
        tree = to_tree(only(Leaf("channel_id", "in", [A, B])))

        assert evaluate(tree, video(channel_id=B)) is Verdict.MATCH
        assert evaluate(tree, video(channel_id="UCother")) is Verdict.NO_MATCH


class TestGroups:
    @pytest.mark.parametrize(
        ("kind", "tree"),
        [
            (GroupKind.ALL, {"and": [LEVELS, LONG]}),
            (GroupKind.ANY, {"or": [LEVELS, LONG]}),
            (GroupKind.NONE, {"!": [{"or": [LEVELS, LONG]}]}),
        ],
    )
    def test_each_kind_of_group(self, kind, tree):
        group = Group(kind, [Leaf("title", "regex", "Levels of"), Leaf("duration_s", ">=", 120)])

        assert to_tree(group) == tree
        assert from_tree(tree) == group

    @pytest.mark.parametrize(
        ("kind", "title", "duration", "expected"),
        [
            (GroupKind.ALL, "Levels of", 600, Verdict.MATCH),
            (GroupKind.ALL, "Levels of", 60, Verdict.NO_MATCH),
            (GroupKind.ANY, "Levels of", 60, Verdict.MATCH),
            (GroupKind.ANY, "Other", 60, Verdict.NO_MATCH),
            (GroupKind.NONE, "Other", 60, Verdict.MATCH),
            (GroupKind.NONE, "Levels of", 60, Verdict.NO_MATCH),
            (GroupKind.NONE, "Other", 600, Verdict.NO_MATCH),
        ],
    )
    def test_each_kind_of_group_does_what_its_name_says(self, kind, title, duration, expected):
        group = Group(kind, [Leaf("title", "regex", "Levels of"), Leaf("duration_s", ">=", 120)])

        assert evaluate(to_tree(group), video(title=title, duration_s=duration)) is expected

    def test_nested_groups_keep_their_levels(self):
        tree = {
            "and": [
                {"or": [LEVELS, {"!": [{"or": [FROM_A, {"and": [LONG, LEVELS]}]}]}]},
                LONG,
            ]
        }
        expected = Group(
            GroupKind.ALL,
            [
                Group(
                    GroupKind.ANY,
                    [
                        Leaf("title", "regex", "Levels of"),
                        Group(
                            GroupKind.NONE,
                            [
                                Leaf("channel_id", "==", A),
                                Group(
                                    GroupKind.ALL, [Leaf("duration_s", ">=", 120), Leaf("title", "regex", "Levels of")]
                                ),
                            ],
                        ),
                    ],
                ),
                Leaf("duration_s", ">=", 120),
            ],
        )

        assert from_tree(tree) == expected
        assert to_tree(expected) == tree

    def test_a_group_of_one_item_stays_a_group(self):
        tree = {"or": [{"and": [LEVELS]}]}

        assert from_tree(tree) == Group(GroupKind.ANY, [Group(GroupKind.ALL, [Leaf("title", "regex", "Levels of")])])
        assert to_tree(from_tree(tree)) == tree

    @pytest.mark.parametrize("kind", list(GroupKind))
    def test_a_root_without_leaves_is_match_all(self, kind):
        assert to_tree(Group(kind)) == MATCH_ALL
        assert to_tree(Group(kind, [Group(GroupKind.ANY, [Group(GroupKind.NONE)])])) == MATCH_ALL

    def test_match_all_opens_as_an_empty_root_group(self):
        assert from_tree(MATCH_ALL) == Group(GroupKind.ALL, [])

    def test_a_group_without_leaves_inside_the_root_gives_no_condition(self):
        group = Group(GroupKind.ALL, [Group(GroupKind.ANY), Leaf("duration_s", ">=", 120), Group(GroupKind.NONE)])

        assert to_tree(group) == {"and": [LONG]}

    def test_the_tree_of_an_empty_root_is_a_copy_of_match_all(self):
        tree = to_tree(Group())
        tree["=="].append(2)

        assert MATCH_ALL == {"==": [1, 1]}

    def test_the_model_does_not_share_lists_with_the_tree(self):
        tree = {"and": [{"in": [{"var": "channel_id"}, [A]]}]}
        group = from_tree(tree)
        group.items[0].value.append(B)
        again = to_tree(group)
        again["and"][0]["in"][1].append("UCx")

        assert tree == {"and": [{"in": [{"var": "channel_id"}, [A]]}]}
        assert group.items[0].value == [A, B]


@pytest.mark.parametrize(
    "tree",
    [
        pytest.param({"and": []}, id="empty all, which matches no video"),
        pytest.param({"or": []}, id="empty any"),
        pytest.param({"and": [{"or": []}, LONG]}, id="empty nested group"),
        pytest.param(LONG, id="leaf as the root"),
        pytest.param({"!": [LONG]}, id="not of a leaf"),
        pytest.param({"and": [{"!": [{"var": "is_short"}]}]}, id="not of a field"),
        pytest.param({"!": [{"and": [LONG]}]}, id="not of all"),
        pytest.param({"!": {"or": [LONG]}}, id="not without a list"),
        pytest.param({"and": LONG}, id="group without a list"),
        pytest.param({"and": [{"<=": ["2026-10-01", DATE]}]}, id="value before the field"),
        pytest.param({"and": [{"==": [{"var": ["title", ""]}, "x"]}]}, id="var with a default"),
        pytest.param({"and": [{"==": [{"var": "uploader"}, "x"]}]}, id="unknown field"),
        pytest.param({"and": [{">=": [TITLE, 5]}]}, id="operator that the field does not offer"),
        pytest.param({"and": [{"in": ["Levels", TITLE]}]}, id="text in the title"),
        pytest.param({"and": [{"in": [TITLE, ["a"]]}]}, id="title in a list"),
        pytest.param({"and": [{"in": [{"var": "channel_id"}, []]}]}, id="empty channel list"),
        pytest.param({"and": [{"in": [{"var": "channel_id"}, A]}]}, id="channel in a text"),
        pytest.param({"and": [{"regex": [TITLE, "x", "m"]}]}, id="pattern with the flag m"),
        pytest.param({"and": [{"regex": [TITLE, "x", "im"]}]}, id="pattern with the flags im"),
        pytest.param({"and": [{"regex": [TITLE, "x", ""]}]}, id="pattern with empty flags"),
        pytest.param({"and": [{"regex": [TITLE, "", "i"]}]}, id="empty pattern"),
        pytest.param({"and": [{"regex": [TITLE, "(", "i"]}]}, id="pattern that does not compile"),
        pytest.param({"and": [{"==": [TITLE, ""]}]}, id="empty text"),
        pytest.param({"and": [{"==": [TITLE, "x", "y"]}]}, id="three arguments"),
        pytest.param({"and": [{"==": [TITLE]}]}, id="one argument"),
        pytest.param({"and": [{">=": [DURATION, 120.5]}]}, id="fraction of a second"),
        pytest.param({"and": [{">=": [DURATION, 120.0]}]}, id="float second"),
        pytest.param({"and": [{">=": [DURATION, True]}]}, id="true as seconds"),
        pytest.param({"and": [{">=": [DURATION, -1]}]}, id="negative seconds"),
        pytest.param({"and": [{">=": [DURATION, "120"]}]}, id="seconds as text"),
        pytest.param({"and": [{"<": [DATE, "2026-1-1"]}]}, id="short date"),
        pytest.param({"and": [{"<": [DATE, "20261001"]}]}, id="date without dashes"),
        pytest.param({"and": [{"<": [DATE, "yesterday"]}]}, id="no date"),
        pytest.param({"and": [{"!=": [DATE, "2026-10-01"]}]}, id="date is not"),
        pytest.param({"and": [{"==": [{"var": "live_status"}, "unknown"]}]}, id="unknown live status"),
        pytest.param({"and": [{"==": [{"var": "live_status"}, ["is_live"]]}]}, id="live status list"),
        pytest.param({"and": [{"==": [{"var": "is_short"}, 1]}]}, id="one as a Short"),
        pytest.param({"and": [{"!=": [{"var": "is_short"}, True]}]}, id="Short is not"),
        pytest.param({"and": [{"==": [1, 1], "!=": [1, 2]}]}, id="object with two operators"),
        pytest.param({"==": [True, False]}, id="false condition"),
        pytest.param([LONG], id="list"),
        pytest.param("x", id="text"),
        pytest.param(None, id="null"),
    ],
)
def test_a_tree_that_the_editor_cannot_show_stays_in_json(tree):
    assert from_tree(tree) is None


@pytest.mark.parametrize("tree", [HOME_RULE, EPICURIOUS_LEVELS_RULE, WATCH_LATER_RULE])
def test_each_live_rule_opens_in_the_visual_editor_and_saves_back_equal(tree):
    original = copy.deepcopy(tree)

    root = from_tree(tree)

    assert root is not None
    assert condition_of(root) == original
    assert tree == original


def test_the_live_rule_of_epicurious_in_the_model():
    assert from_tree(EPICURIOUS_LEVELS_RULE) == Group(
        GroupKind.ALL,
        [
            Leaf("channel_id", "in", [EPICURIOUS]),
            Leaf("title", "regex", ".* Levels of .*"),
            Leaf("duration_s", ">=", 120),
        ],
    )


class TestDuration:
    @pytest.mark.parametrize(
        ("value", "fields"),
        [(0, (0, 0)), (59, (0, 59)), (60, (1, 0)), (125, (2, 5)), (3600, (60, 0)), (None, (None, None))],
    )
    def test_the_two_fields_of_a_stored_duration(self, value, fields):
        assert duration_fields(value) == fields

    @pytest.mark.parametrize("value", [-1, 1.5, True, "120"])
    def test_a_value_that_is_no_duration_shows_empty_fields(self, value):
        assert duration_fields(value) == (None, None)

    def test_the_fields_of_a_wrong_input_show_again(self):
        assert duration_fields(DurationInput(1.5, 60)) == (1.5, 60)

    @pytest.mark.parametrize(
        ("minutes", "seconds", "value"),
        [
            (2.0, 0.0, 120),
            (2.0, None, 120),
            (None, 30.0, 30),
            (0.0, 0.0, 0),
            (0.0, 59.0, 59),
            (60.0, 0.0, 3600),
            (None, None, None),
        ],
    )
    def test_the_fields_give_the_total_of_the_seconds(self, minutes, seconds, value):
        assert duration_value(minutes, seconds) == value

    @pytest.mark.parametrize(
        ("minutes", "seconds"),
        [(0.0, 60.0), (1.0, 61.0), (1.5, 0.0), (0.0, 0.5), (-1.0, 0.0), (0.0, -1.0)],
    )
    def test_a_field_without_a_whole_number_in_its_range_gives_no_duration(self, minutes, seconds):
        value = duration_value(minutes, seconds)

        assert value == DurationInput(minutes, seconds)
        assert leaf_error(Leaf("duration_s", ">=", value)) == DURATION_ERROR

    @pytest.mark.parametrize("value", [0, 1, 59, 60, 61, 125, 3600, 7322])
    def test_a_stored_duration_comes_back_unchanged_from_its_fields(self, value):
        assert duration_value(*map(float, duration_fields(value))) == value

    @pytest.mark.parametrize(
        ("value", "text"), [(0, "0:00"), (5, "0:05"), (59, "0:59"), (60, "1:00"), (125, "2:05"), (3600, "60:00")]
    )
    def test_the_text_of_a_duration(self, value, text):
        assert format_duration(value) == text
        assert describe({"and": [{">=": [DURATION, value]}]}, {}) == f"Duration is at least {text}"


class TestLeafErrors:
    @pytest.mark.parametrize(
        ("leaf", "error"),
        [
            (Leaf("title", "regex", ""), "Enter a pattern."),
            (Leaf("title", "regex", None), "Enter a pattern."),
            (Leaf("title", "==", ""), "Enter a text."),
            (Leaf("video_id", "!=", None), "Enter a text."),
            (Leaf("channel_id", "==", None), "Choose a channel."),
            (Leaf("channel_id", "in", []), "Choose at least one channel."),
            (Leaf("channel_id", "in", [A, ""]), "Choose at least one channel."),
            (Leaf("duration_s", ">=", None), DURATION_ERROR),
            (Leaf("duration_s", ">=", 1.5), DURATION_ERROR),
            (Leaf("duration_s", ">=", -1), DURATION_ERROR),
            (Leaf("duration_s", ">=", DurationInput(1.5, None)), DURATION_ERROR),
            (Leaf("upload_date", "<", None), "Enter a date."),
            (Leaf("upload_date", "<", "2026-02-30"), "Enter a date."),
            (Leaf("live_status", "==", None), "Choose a live status."),
            (Leaf("live_status", "==", []), "Choose a live status."),
            (Leaf("is_short", "==", None), "Choose whether the video is a Short."),
            (Leaf("title", ">=", "x"), "Choose a field and a comparison."),
            (Leaf("uploader", "==", "x"), "Choose a field and a comparison."),
        ],
    )
    def test_an_incomplete_leaf_has_an_error(self, leaf, error):
        assert leaf_error(leaf) == error

    def test_a_pattern_that_does_not_compile_names_the_error(self):
        assert leaf_error(Leaf("title", "regex", "(")) == (
            "The pattern does not compile: missing ), unterminated subpattern at position 0"
        )

    @pytest.mark.parametrize(
        "leaf",
        [
            Leaf("duration_s", ">=", 0),
            Leaf("upload_date", "<", "2026-02-28"),
            *[Leaf("live_status", "==", status) for status in LIVE_STATUSES],
        ],
    )
    def test_a_complete_leaf_has_no_error(self, leaf):
        assert leaf_error(leaf) is None


class TestEditing:
    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            ("title", Leaf("title", "regex", "")),
            ("channel_id", Leaf("channel_id", "in", [])),
            ("duration_s", Leaf("duration_s", ">=", None)),
            ("upload_date", Leaf("upload_date", ">=", None)),
            ("live_status", Leaf("live_status", "==", None)),
            ("is_short", Leaf("is_short", "==", True)),
            ("video_id", Leaf("video_id", "regex", "")),
        ],
    )
    def test_a_new_leaf_of_each_field(self, field, expected):
        assert new_leaf(field) == expected

    def test_a_new_field_clears_the_operator_the_value_and_the_case(self):
        leaf = Leaf("title", "!=", "x", case_sensitive=True)

        change_field(leaf, "duration_s")

        assert leaf == Leaf("duration_s", ">=", None)

    @pytest.mark.parametrize(
        ("leaf", "operator", "expected"),
        [
            (Leaf("title", "regex", "x"), "==", Leaf("title", "==", "x")),
            (Leaf("duration_s", ">=", 120), "<", Leaf("duration_s", "<", 120)),
            (Leaf("channel_id", "==", A), "in", Leaf("channel_id", "in", [A])),
            (Leaf("channel_id", "==", None), "in", Leaf("channel_id", "in", [])),
            (Leaf("channel_id", "in", [A, B]), "!=", Leaf("channel_id", "!=", A)),
            (Leaf("channel_id", "in", []), "==", Leaf("channel_id", "==", None)),
            (Leaf("channel_id", "in", [A]), "regex", Leaf("channel_id", "regex", "")),
            (Leaf("channel_id", "regex", "x"), "==", Leaf("channel_id", "==", None)),
        ],
    )
    def test_a_new_operator_keeps_the_value_that_fits(self, leaf, operator, expected):
        change_operator(leaf, operator)

        assert leaf == expected


class TestConditionForSave:
    def test_an_incomplete_leaf_stops_the_save_with_its_field(self):
        root = Group(GroupKind.ALL, [Leaf("duration_s", ">=", 120), Group(GroupKind.ANY, [Leaf("title", "regex", "")])])

        with pytest.raises(RuleError, match=r"^Title: Enter a pattern\.$"):
            condition_of(root)

    def test_a_pattern_that_does_not_compile_stops_the_save(self):
        with pytest.raises(RuleError, match=r"^Title: The pattern does not compile: missing \)"):
            condition_of(only(Leaf("title", "regex", "Levels (of")))

    def test_an_empty_root_saves_match_all(self):
        assert condition_of(Group()) == MATCH_ALL

    def test_json_gives_its_tree(self):
        assert condition_from_json(json_text(HOME_RULE)) == HOME_RULE

    @pytest.mark.parametrize("text", ["", None, "{", "{'and': []}"])
    def test_text_that_is_not_json_stops_the_save(self, text):
        with pytest.raises(RuleError, match=r"^The condition is not valid JSON: "):
            condition_from_json(text)

    @pytest.mark.parametrize(
        ("text", "error"),
        [
            ('{"var": "channel_title"}', "The field 'channel_title' is not a video field"),
            ('{"regex": [{"var": "title"}, "("]}', "does not compile"),
            ("[1]", "The root of a rule must be an object"),
            ('{"merge": []}', "The operator 'merge' is not allowed"),
        ],
    )
    def test_json_that_is_no_valid_rule_stops_the_save(self, text, error):
        with pytest.raises(RuleError, match=error):
            condition_from_json(text)

    def test_the_json_text_keeps_the_letters_of_each_language(self):
        assert '"Однією Правою"' in json_text({"==": [TITLE, "Однією Правою"]})


TITLES = {EPICURIOUS: "Epicurious", A: "Alpha", B: "Beta"}


class TestDescription:
    @pytest.mark.parametrize(
        ("tree", "text"),
        [
            (MATCH_ALL, "Each video"),
            ({"and": []}, "Custom JSON condition"),
            (HOME_RULE, "Channel is one of 15 channels and Duration is at least 2:00"),
            (
                EPICURIOUS_LEVELS_RULE,
                (
                    'Channel is one of Epicurious and Title matches the pattern ".* Levels of .*" and '
                    "Duration is at least 2:00"
                ),
            ),
            (
                {"or": [{"in": [{"var": "channel_id"}, [A, B, "UCunknown"]]}, {"==": [{"var": "is_short"}, True]}]},
                "Channel is one of Alpha, Beta, UCunknown or the video is a Short",
            ),
            (
                {"and": [{"!": [{"or": [FROM_A, {"==": [{"var": "is_short"}, False]}]}]}]},
                "not (Channel is Alpha or the video is not a Short)",
            ),
            (
                {"and": [{"or": [{"regex": [TITLE, "Levels"]}, {"<": [DATE, "2026-10-01"]}]}, LONG]},
                (
                    '(Title matches the pattern "Levels" (case-sensitive) or Upload date is before 2026-10-01) and '
                    "Duration is at least 2:00"
                ),
            ),
            ({"and": [{"or": [LONG]}]}, "Duration is at least 2:00"),
            ({"or": [{"!=": [{"var": "live_status"}, "was_live"]}]}, "Live status is not a past live stream"),
            ({"or": [{"==": [{"var": "video_id"}, "vid1"]}]}, 'Video id is "vid1"'),
        ],
    )
    def test_description(self, tree, text):
        assert describe(tree, TITLES) == text

    def test_each_part_of_the_description_goes_through_the_translation(self, monkeypatch):
        class Marking:
            def gettext(self, message):
                return f"<<{message}>>"

        monkeypatch.setattr(i18n, "_translations", Marking())
        tree = {
            "and": [
                {"!": [{"or": [FROM_A, {"==": [{"var": "live_status"}, "is_live"]}]}]},
                {"regex": [TITLE, "x"]},
                {"in": [{"var": "channel_id"}, HOME_CHANNELS]},
                {"==": [{"var": "is_short"}, True]},
            ]
        }

        # Only the data stays outside the marks: the channel title Alpha and the pattern "x".
        assert describe(tree, TITLES) == (
            "<<not (<<<<Channel>> <<is>> Alpha>> <<or>> <<<<Live status>> <<is>> <<live now>>>>)>> <<and>> "
            '<<<<<<Title>> <<matches the pattern>> "x">> (case-sensitive)>> <<and>> '
            "<<<<Channel>> <<is one of>> <<15 channels>>>> <<and>> <<the video is a Short>>"
        )
        assert describe(MATCH_ALL, TITLES) == "<<Each video>>"
        assert describe({"and": []}, TITLES) == "<<Custom JSON condition>>"
