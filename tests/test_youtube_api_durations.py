from types import SimpleNamespace

import pytest

from youtube_automanager.youtube_api import YoutubeAPI


class FakeVideosApi:
    """Answers videos.list requests from a dict and records each request."""

    def __init__(self, durations=None, failing_requests=()):
        self.durations = durations or {}
        self.failing_requests = set(failing_requests)
        self.requests = []
        self.parts = []

    def get_video_by_id(self, video_id, parts):
        self.requests.append(list(video_id))
        self.parts.append(parts)
        if len(self.requests) - 1 in self.failing_requests:
            raise RuntimeError("quota exceeded")
        items = [
            SimpleNamespace(id=i, contentDetails=SimpleNamespace(duration=self.durations[i]))
            for i in video_id
            if i in self.durations
        ]
        return SimpleNamespace(items=items)


def make_api(videos_api):
    return YoutubeAPI(videos_api, "token")


def ids(count):
    return [f"v{i}" for i in range(count)]


class TestParseDuration:
    @pytest.mark.parametrize(
        ("text", "seconds"),
        [("PT1H23M45S", 5025), ("PT30S", 30), ("PT15M", 900), ("P1DT1H", 90000), ("PT0S", 0)],
    )
    def test_iso_duration_gives_whole_seconds(self, text, seconds):
        assert YoutubeAPI._parse_duration(text) == seconds

    @pytest.mark.parametrize("text", [None, ""])
    def test_missing_duration_gives_none(self, text):
        assert YoutubeAPI._parse_duration(text) is None

    def test_unparsable_duration_gives_none(self):
        assert YoutubeAPI._parse_duration("not a duration") is None


class TestGetVideosDurations:
    def test_returns_seconds_by_video_id(self):
        videos = FakeVideosApi({"a": "PT1H23M45S", "b": "PT30S"})
        assert make_api(videos).get_videos_durations(["a", "b"]) == {"a": 5025, "b": 30}

    def test_asks_for_the_content_details_part(self):
        videos = FakeVideosApi({"a": "PT30S"})
        make_api(videos).get_videos_durations(["a"])
        assert videos.parts == [["contentDetails"]]

    def test_empty_list_makes_no_request(self):
        videos = FakeVideosApi()
        assert make_api(videos).get_videos_durations([]) == {}
        assert videos.requests == []

    def test_ids_are_deduplicated_in_order(self):
        videos = FakeVideosApi({"a": "PT1S", "b": "PT2S"})
        make_api(videos).get_videos_durations(["b", "a", "b", "a"])
        assert videos.requests == [["b", "a"]]

    @pytest.mark.parametrize(
        ("count", "batch_sizes"),
        [(1, [1]), (49, [49]), (50, [50]), (51, [50, 1]), (120, [50, 50, 20])],
    )
    def test_requests_go_in_batches_of_50(self, count, batch_sizes):
        videos = FakeVideosApi({i: "PT1S" for i in ids(count)})
        result = make_api(videos).get_videos_durations(ids(count))
        assert [len(request) for request in videos.requests] == batch_sizes
        assert len(result) == count

    def test_second_call_for_the_same_ids_makes_no_new_request(self):
        videos = FakeVideosApi({"a": "PT1S", "b": "PT2S"})
        api = make_api(videos)
        first = api.get_videos_durations(["a", "b"])
        second = api.get_videos_durations(["a", "b"])
        assert second == first == {"a": 1, "b": 2}
        assert len(videos.requests) == 1

    def test_second_call_requests_only_the_ids_that_are_not_cached(self):
        videos = FakeVideosApi({"a": "PT1S", "b": "PT2S"})
        api = make_api(videos)
        api.get_videos_durations(["a"])
        assert api.get_videos_durations(["a", "b"]) == {"a": 1, "b": 2}
        assert videos.requests == [["a"], ["b"]]

    def test_cache_belongs_to_one_instance(self):
        videos = FakeVideosApi({"a": "PT1S"})
        make_api(videos).get_videos_durations(["a"])
        make_api(videos).get_videos_durations(["a"])
        assert len(videos.requests) == 2

    def test_id_without_a_duration_in_the_response_is_omitted(self):
        videos = FakeVideosApi({"a": "PT1S"})
        assert make_api(videos).get_videos_durations(["a", "unknown"]) == {"a": 1}

    def test_id_that_the_response_leaves_out_is_requested_again(self):
        videos = FakeVideosApi({"a": "PT1S"})
        api = make_api(videos)
        api.get_videos_durations(["a", "unknown"])
        api.get_videos_durations(["a", "unknown"])
        assert videos.requests == [["a", "unknown"], ["unknown"]]

    def test_unparsable_duration_is_omitted_and_not_requested_again(self):
        videos = FakeVideosApi({"a": "PT1S", "bad": "garbage"})
        api = make_api(videos)
        assert api.get_videos_durations(["a", "bad"]) == {"a": 1}
        assert api.get_videos_durations(["a", "bad"]) == {"a": 1}
        assert len(videos.requests) == 1

    def test_empty_response_gives_an_empty_result(self):
        api = make_api(SimpleNamespace(get_video_by_id=lambda video_id, parts: None))
        assert api.get_videos_durations(["a"]) == {}

    def test_failing_batch_is_skipped_and_the_other_batches_still_return(self):
        videos = FakeVideosApi({i: "PT1S" for i in ids(120)}, failing_requests={1})
        result = make_api(videos).get_videos_durations(ids(120))
        assert len(videos.requests) == 3
        assert set(result) == set(ids(50)) | set(ids(120)[100:])

    def test_failing_batch_is_requested_again_on_the_next_call(self):
        videos = FakeVideosApi({"a": "PT1S"}, failing_requests={0})
        api = make_api(videos)
        assert api.get_videos_durations(["a"]) == {}
        assert api.get_videos_durations(["a"]) == {"a": 1}
