"""Acceptance tests with the live rules of the user."""

import datetime

import pytest

from youtube_automanager.db.models import SeenVideoState
from youtube_automanager.engine.rules import Verdict, evaluate, validate
from youtube_automanager.youtube.public import VideoEntry

# Target playlist "@Home". Each id comes from the subscriptions of the user. The channel names "GreenGrass" and
# "The Jungle Diaries" are not in the subscriptions, so they are not in the list. The handle @yevgensynelnykovv
# does not resolve, so it is not in the list either.
HOME_CHANNELS = [
    "UC0intLFzLaudFG-xAvUEO-A",  # Not Just Bikes
    "UCY1kMZp36IQSyNx_9h4mpCg",  # Mark Rober
    "UC1Q2MLy0DEswY-1ZheTcYig",  # імені Т.Г. Шевченка
    "UCekQr9znsk2vWxBo3YiLq2w",  # You Suck At Cooking
    "UCftNLdhx5pYzuzQT9ig362g",  # ідея олександрівна
    "UC3h6lPkBJP8yUzRke8Tb_qA",  # @misha_katsurin, the rule named it by its old title "Миша Кацурин"
    "UCsXVk37bltHxD1rDPwtNM8Q",  # Kurzgesagt – In a Nutshell
    "UC-3SbfTPJsL8fJAPKiVqBLg",  # Deep Look
    "UCRxAgfYexGLlu1WHGIMUDqw",  # JunsKitchen
    "UC7AvvBtzbwWpkcwxV4bGKLw",  # Однією Правою
    "UCioz7pLeEpBNrFfwHb2m-pA",  # @K.Rationalist
    "UC176GAQozKKjhz62H8u9vQQ",  # @realscience
    "UC8mrTZcdEiCkYGV2ckIVPmQ",  # @DmytroHorbatiuk
    "UCdZzWhdrkzu3CR_yxWOSnbg",  # @tarabon
    "UCIHkmNkkqHkqbKwcZbjIF0Q",  # @DvokolisniHroniky
]

# Epicurious. The handle @epicuriousdotcom gives HTTP 404. The legacy URL /user/epicuriousdotcom and the handle
# @epicurious both give this id.
EPICURIOUS = "UCcjhYlL1WRBjKaJsMH_h7Lg"

# Target playlist "WL".
WATCH_LATER_CHANNELS = [
    "UCbsfyGlrjrKQC0gbzK0-EiA",  # Jared Owen
    "UCcdv38QxPjSMqbt5ffLhJLA",  # @Tailscale
]

OTHER_CHANNEL = "UC0000000000000000000000"

LONG_ENOUGH = {">=": [{"var": "duration_s"}, 120]}

HOME_RULE = {"and": [{"in": [{"var": "channel_id"}, HOME_CHANNELS]}, LONG_ENOUGH]}
EPICURIOUS_LEVELS_RULE = {
    "and": [
        {"in": [{"var": "channel_id"}, [EPICURIOUS]]},
        {"regex": [{"var": "title"}, ".* Levels of .*", "i"]},
        LONG_ENOUGH,
    ]
}
WATCH_LATER_RULE = {"and": [{"in": [{"var": "channel_id"}, WATCH_LATER_CHANNELS]}, LONG_ENOUGH]}

LEVELS_TITLE = "4 Levels of Steak: Amateur to Food Scientist"

# Each rule with its channels and a title that the rule accepts.
RULES = {
    "home": (HOME_RULE, HOME_CHANNELS, "A video"),
    "epicurious_levels": (EPICURIOUS_LEVELS_RULE, [EPICURIOUS], LEVELS_TITLE),
    "watch_later": (WATCH_LATER_RULE, WATCH_LATER_CHANNELS, "A video"),
}
RULE_CHANNELS = [(name, channel_id) for name, (_, channels, _) in RULES.items() for channel_id in channels]


def video(channel_id, title="A video", duration_s=600, **overrides):
    return {
        "video_id": "vid1",
        "title": title,
        "channel_id": channel_id,
        "duration_s": duration_s,
        "upload_date": "2026-10-01",
        "live_status": "not_live",
        "is_short": False,
        **overrides,
    }


def test_channel_ids_are_unique_and_not_the_other_channel():
    all_ids = [*HOME_CHANNELS, EPICURIOUS, *WATCH_LATER_CHANNELS]
    assert len(set(all_ids)) == len(all_ids)
    assert OTHER_CHANNEL not in all_ids


@pytest.mark.parametrize("name", RULES)
def test_rule_is_valid(name):
    validate(RULES[name][0])


@pytest.mark.parametrize(("name", "channel_id"), RULE_CHANNELS)
def test_video_of_each_channel_matches(name, channel_id):
    tree, _, title = RULES[name]
    assert evaluate(tree, video(channel_id, title)) is Verdict.MATCH


@pytest.mark.parametrize("name", RULES)
class TestEachRule:
    def test_channel_outside_the_rule_does_not_match(self, name):
        tree, _, title = RULES[name]
        assert evaluate(tree, video(OTHER_CHANNEL, title)) is Verdict.NO_MATCH

    def test_duration_119_does_not_match(self, name):
        tree, channels, title = RULES[name]
        assert evaluate(tree, video(channels[0], title, duration_s=119)) is Verdict.NO_MATCH

    def test_duration_120_matches(self, name):
        tree, channels, title = RULES[name]
        assert evaluate(tree, video(channels[0], title, duration_s=120)) is Verdict.MATCH

    def test_duration_without_a_value_is_unknown(self, name):
        tree, channels, title = RULES[name]
        assert evaluate(tree, video(channels[0], title, duration_s=None)) is Verdict.UNKNOWN

    def test_upcoming_stream_without_duration_and_date_is_unknown(self, name):
        tree, channels, title = RULES[name]
        data = video(channels[0], title, duration_s=None, upload_date=None, live_status="is_upcoming")
        assert evaluate(tree, data) is Verdict.UNKNOWN

    def test_channel_outside_the_rule_does_not_match_without_a_duration(self, name):
        tree, _, title = RULES[name]
        assert evaluate(tree, video(OTHER_CHANNEL, title, duration_s=None)) is Verdict.NO_MATCH


class TestEpicuriousLevelsRule:
    @pytest.mark.parametrize(
        "title",
        [
            LEVELS_TITLE,
            "3 Levels of Pasta: Amateur vs. Home Cook vs. Food Scientist",
            "4 LEVELS OF Pizza",
            "4 levels of burger",
        ],
    )
    def test_levels_title_matches(self, title):
        assert evaluate(EPICURIOUS_LEVELS_RULE, video(EPICURIOUS, title)) is Verdict.MATCH

    @pytest.mark.parametrize(
        "title",
        [
            "Pastry Chef Answers Baking Questions From Twitter",
            "Levels of Steak",
            "4 Levels ofSteak",
            "4 Level of Steak",
        ],
    )
    def test_title_without_levels_of_does_not_match(self, title):
        assert evaluate(EPICURIOUS_LEVELS_RULE, video(EPICURIOUS, title)) is Verdict.NO_MATCH

    def test_levels_title_from_another_channel_does_not_match(self):
        assert evaluate(EPICURIOUS_LEVELS_RULE, video(OTHER_CHANNEL, LEVELS_TITLE)) is Verdict.NO_MATCH

    def test_levels_title_from_a_home_channel_does_not_match(self):
        assert evaluate(EPICURIOUS_LEVELS_RULE, video(HOME_CHANNELS[0], LEVELS_TITLE)) is Verdict.NO_MATCH

    def test_title_without_levels_of_does_not_match_without_a_duration(self):
        data = video(EPICURIOUS, "Pastry Chef Answers Baking Questions", duration_s=None)
        assert evaluate(EPICURIOUS_LEVELS_RULE, data) is Verdict.NO_MATCH


def upload(video_id, channel_id, title="A video", duration_s=600, live_status=None):
    upcoming = live_status == "is_upcoming"
    return VideoEntry(
        video_id=video_id,
        title=title,
        channel_id=channel_id,
        channel_title=None,
        duration_s=None if upcoming else duration_s,
        upload_date=None if upcoming else datetime.date(2026, 10, 12),
        live_status=live_status,
    )


def test_the_live_rules_through_the_sync_engine(sync):
    home = sync.rule("@Home", HOME_RULE, channels=HOME_CHANNELS, target="PLhome")
    levels = sync.rule("Epicurious levels", EPICURIOUS_LEVELS_RULE, channels=[EPICURIOUS], target="PLhome")
    later = sync.rule("Watch later", WATCH_LATER_RULE, channels=WATCH_LATER_CHANNELS, target="WL")
    channels = [*HOME_CHANNELS, EPICURIOUS, *WATCH_LATER_CHANNELS]
    old = {channel_id: [upload(f"old{index:02}", channel_id)] for index, channel_id in enumerate(channels)}
    sync.sources.uploads.update(old)
    sync.run()
    assert sync.account.added == []

    new = {
        HOME_CHANNELS[0]: [upload("homeLong", HOME_CHANNELS[0])],
        HOME_CHANNELS[1]: [upload("homeShort", HOME_CHANNELS[1], duration_s=119)],
        EPICURIOUS: [
            upload("levels", EPICURIOUS, LEVELS_TITLE),
            upload("pastry", EPICURIOUS, "Pastry Chef Answers Baking Questions From Twitter"),
            upload("pizza", EPICURIOUS, "4 Levels of Pizza", duration_s=100),
        ],
        WATCH_LATER_CHANNELS[0]: [upload("jared", WATCH_LATER_CHANNELS[0])],
        WATCH_LATER_CHANNELS[1]: [upload("stream", WATCH_LATER_CHANNELS[1], live_status="is_upcoming")],
    }
    for channel_id, videos in new.items():
        sync.sources.uploads[channel_id] = [*videos, *old[channel_id]]
    sync.run()

    assert sorted(sync.account.added) == [("PLhome", "homeLong"), ("PLhome", "levels"), ("WL", "jared")]
    assert sync.added_rows() == {("PLhome", "homeLong"): home, ("PLhome", "levels"): levels, ("WL", "jared"): later}
    assert sync.seen(WATCH_LATER_CHANNELS[1])["stream"] is SeenVideoState.DEFERRED
    assert sync.seen(EPICURIOUS)["pastry"] is SeenVideoState.SEEN
