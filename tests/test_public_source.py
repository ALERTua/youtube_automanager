"""Tests of the public YouTube source on recorded yt-dlp answers. They make no network request."""

import copy
import datetime
import io
import itertools
import json
import logging
import socket
from pathlib import Path

import pytest
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.networking.common import Response
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import DownloadError, ExtractorError

from youtube_automanager.youtube import public
from youtube_automanager.youtube.public import ChannelInfo, PublicSourceError, VideoEntry

FIXTURES = Path(__file__).parent / "fixtures" / "ytdlp"

NASA = "UCLA_DiR1FfKNvjuUpBHmylQ"
EPICURIOUS = "UCcjhYlL1WRBjKaJsMH_h7Lg"
WIRED = "UCftwRNsjfRo08xYE31tkiyw"
GOOGLE_TECH_TALKS = "UCtXKDgv1AVoG88PLl8nGXmw"

NASA_UPLOADS_URL = "https://www.youtube.com/playlist?list=UULA_DiR1FfKNvjuUpBHmylQ"
EPICURIOUS_SHORTS_URL = f"https://www.youtube.com/channel/{EPICURIOUS}/shorts"
SEARCH_URL = "https://www.youtube.com/results?search_query=epicurious&sp=EgIQAg%253D%253D"
EPICURIOUS_PAGE_URL = "https://www.youtube.com/@Epicurious"
WIRED_PAGE_URL = f"https://www.youtube.com/channel/{WIRED}"

ALL_RECORDINGS = (
    "uploads_nasa",
    "shorts_epicurious",
    "search_epicurious",
    "channel_epicurious_by_handle",
    "channel_wired_by_id",
    "missing_handle",
    "missing_channel_id",
    "no_shorts_tab_google_tech_talks",
)


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def rebuild_error(described: dict) -> DownloadError:
    """Build the DownloadError of yt-dlp from its recorded description, with the same cause."""
    if described["cause"] == "HTTPError":
        cause = HTTPError(Response(io.BytesIO(), described["url"], {}, status=described["status"]))
    else:
        cause = ExtractorError(
            described["orig_msg"], expected=described["expected"], video_id=described["video_id"], ie=described["ie"]
        )
    return DownloadError(described["message"], (type(cause), cause, None))


def http_error(status: int) -> DownloadError:
    return rebuild_error(
        {
            "message": f"ERROR: [youtube:tab] x: Unable to download webpage: HTTP Error {status}",
            "cause": "HTTPError",
            "status": status,
            "url": "https://www.youtube.com/",
        }
    )


class FakeExtract:
    """Replaces public._extract: answers from the recorded files and counts each entry that it hands out."""

    def __init__(self, names=ALL_RECORDINGS):
        self.recordings = {recording["url"]: recording for recording in map(load, names)}
        self.calls = []
        self.entries_handed_out = 0

    def __call__(self, url, **options):
        self.calls.append((url, options))
        recording = self.recordings[url]
        if "error" in recording:
            raise rebuild_error(recording["error"])
        info = copy.deepcopy(recording["info"])
        if "entries" in info:
            info["entries"] = self._hand_out(info["entries"])
        return info

    def add_redirect(self, url, target, redirect_type="url"):
        self.recordings[url] = {"url": url, "info": {"_type": redirect_type, "url": target, "ie_key": "YoutubeTab"}}

    def _hand_out(self, entries):
        for entry in entries:
            self.entries_handed_out += 1
            yield entry


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test of the public source tried to open a network connection")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture
def fake(monkeypatch):
    fake_extract = FakeExtract()
    monkeypatch.setattr(public, "_extract", fake_extract)
    return fake_extract


def recorded_entries(name: str) -> list[dict]:
    return load(name)["info"]["entries"]


def test_extract_asks_yt_dlp_for_the_unprocessed_answer_without_cookies(monkeypatch):
    seen = {}

    class FakeYoutubeDL:
        def __init__(self, params):
            seen["params"] = params

        def extract_info(self, url, **kwargs):
            seen["call"] = (url, kwargs)
            return {"id": "x"}

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(public, "YoutubeDL", FakeYoutubeDL)

    assert public._extract("https://www.youtube.com/@x", playlist_items="0") == {"id": "x"}
    assert seen == {
        "params": {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "logger": public._YT_DLP_LOGGER,
            "playlist_items": "0",
        },
        "call": ("https://www.youtube.com/@x", {"download": False, "process": False}),
        "closed": True,
    }


class ClosingYoutubeDL:
    """Replaces YoutubeDL: answers with the given result or error, and records when it is closed."""

    def __init__(self, answer):
        self.answer = answer
        self.closed = False

    def __call__(self, params):
        return self

    def extract_info(self, url, **kwargs):
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def close(self):
        self.closed = True


def test_extract_closes_yt_dlp_only_after_the_lazy_entries_end(monkeypatch):
    ydl = ClosingYoutubeDL({"entries": iter([{"id": "a"}, {"id": "b"}])})
    monkeypatch.setattr(public, "YoutubeDL", ydl)

    entries = public._extract("https://www.youtube.com/playlist?list=UUx")["entries"]
    assert next(entries) == {"id": "a"}
    assert not ydl.closed

    assert list(entries) == [{"id": "b"}]
    assert ydl.closed


def test_extract_closes_yt_dlp_when_the_caller_stops_early(monkeypatch):
    ydl = ClosingYoutubeDL({"entries": iter([{"id": "a"}, {"id": "b"}])})
    monkeypatch.setattr(public, "YoutubeDL", ydl)

    entries = public._extract("https://www.youtube.com/playlist?list=UUx")["entries"]
    next(entries)
    entries.close()

    assert ydl.closed


def test_extract_closes_yt_dlp_when_a_later_page_fails(monkeypatch):
    def pages():
        yield {"id": "a"}
        raise ExtractorError("Unable to download API page: HTTP Error 503", expected=True)

    ydl = ClosingYoutubeDL({"entries": pages()})
    monkeypatch.setattr(public, "YoutubeDL", ydl)

    entries = public._extract("https://www.youtube.com/playlist?list=UUx")["entries"]
    next(entries)
    with pytest.raises(ExtractorError):
        next(entries)

    assert ydl.closed


@pytest.mark.parametrize(
    "answer",
    [{"id": "x"}, {"_type": "url", "url": "https://www.youtube.com/@y"}, {"entries": [{"id": "tab"}]}],
    ids=["no entries", "redirect", "list of tabs"],
)
def test_extract_closes_yt_dlp_at_once_for_an_answer_without_lazy_entries(monkeypatch, answer):
    ydl = ClosingYoutubeDL(answer)
    monkeypatch.setattr(public, "YoutubeDL", ydl)

    public._extract("https://www.youtube.com/@x")

    assert ydl.closed


def test_extract_closes_yt_dlp_when_the_extraction_fails(monkeypatch):
    ydl = ClosingYoutubeDL(http_error(429))
    monkeypatch.setattr(public, "YoutubeDL", ydl)

    with pytest.raises(DownloadError):
        public._extract("https://www.youtube.com/@x")

    assert ydl.closed


def test_uploads_playlist_id():
    assert public.uploads_playlist_id(NASA) == "UULA_DiR1FfKNvjuUpBHmylQ"


@pytest.mark.parametrize(
    "value",
    ["", "UC", NASA[:-1], NASA + "x", "UULA_DiR1FfKNvjuUpBHmylQ", "@NASA", "UCLA_DiR1FfKNvjuUpBHmyl!"],
)
def test_uploads_playlist_id_refuses_a_value_that_is_not_a_channel_id(value):
    with pytest.raises(ValueError, match="not a YouTube channel id"):
        public.uploads_playlist_id(value)


def test_iter_channel_uploads_reads_the_uploads_playlist_with_approximate_dates(fake):
    list(public.iter_channel_uploads(NASA))

    assert fake.calls == [(NASA_UPLOADS_URL, {"extractor_args": {"youtubetab": {"approximate_date": [""]}}})]


def test_iter_channel_uploads_maps_each_field(fake):
    videos = {video.video_id: video for video in public.iter_channel_uploads(NASA)}

    assert videos["-62C9R_41Sc"] == VideoEntry(
        video_id="-62C9R_41Sc",
        title="From the Moon to the 50-Yard Line",
        channel_id=NASA,
        channel_title="NASA",
        duration_s=39,
        upload_date=datetime.date(2026, 10, 7),
        live_status=None,
    )
    assert videos["IoL51xLkMSY"] == VideoEntry(
        video_id="IoL51xLkMSY",
        title="NASA's Northrop Grumman Commercial Resupply Services 24 Departure",
        channel_id=NASA,
        channel_title="NASA",
        duration_s=2527,
        upload_date=datetime.date(2026, 10, 10),
        live_status="was_live",
    )
    assert videos["f8FE2R7esZ0"] == VideoEntry(
        video_id="f8FE2R7esZ0",
        title="NASA's SpaceX 35th Commercial Resupply Services Launch",
        channel_id=NASA,
        channel_title="NASA",
        duration_s=None,
        upload_date=None,
        live_status="is_upcoming",
    )


def test_iter_channel_uploads_keeps_the_newest_first_order(fake):
    videos = list(public.iter_channel_uploads(NASA))

    assert [video.video_id for video in videos] == [entry["id"] for entry in recorded_entries("uploads_nasa")]
    dates = [video.upload_date for video in videos if video.upload_date is not None]
    assert len(dates) > 20
    assert dates == sorted(dates, reverse=True)


def test_iter_channel_uploads_takes_each_entry_only_when_the_iteration_reaches_it(fake):
    uploads = public.iter_channel_uploads(NASA)
    assert fake.entries_handed_out == 0

    first = list(itertools.islice(uploads, 3))

    assert [video.video_id for video in first] == ["IoL51xLkMSY", "f8FE2R7esZ0", "x6p9Ri0DlNE"]
    assert fake.entries_handed_out == 3


def test_iter_channel_uploads_refuses_a_bad_channel_id_before_any_request(fake):
    with pytest.raises(ValueError):
        public.iter_channel_uploads("@NASA")

    assert fake.calls == []


def test_shorts_video_ids(fake):
    ids = public.shorts_video_ids(EPICURIOUS)

    assert ids == {entry["id"] for entry in recorded_entries("shorts_epicurious")}
    assert len(ids) == 30
    assert "Xz-uwGDjrjI" in ids
    assert fake.calls == [(EPICURIOUS_SHORTS_URL, {})]


def test_shorts_video_ids_stops_at_the_limit(fake):
    ids = public.shorts_video_ids(EPICURIOUS, limit=5)

    assert ids == {entry["id"] for entry in recorded_entries("shorts_epicurious")[:5]}
    assert fake.entries_handed_out == 5


def test_shorts_video_ids_is_empty_for_a_channel_without_a_shorts_tab(fake):
    assert public.shorts_video_ids(GOOGLE_TECH_TALKS) == set()

    assert fake.calls == [(f"https://www.youtube.com/channel/{GOOGLE_TECH_TALKS}/shorts", {})]


@pytest.mark.parametrize(
    ("orig_msg", "expected"),
    [
        ("This channel does not have a shorts tab", False),
        ("This channel does not have a streams tab", True),
        ("YouTube said: This channel does not exist.", True),
    ],
    ids=["not expected", "other tab", "missing channel"],
)
def test_shorts_video_ids_raises_for_another_extractor_error(monkeypatch, orig_msg, expected):
    error = rebuild_error(
        {
            "message": f"ERROR: [youtube:tab] {EPICURIOUS}: {orig_msg}",
            "cause": "ExtractorError",
            "orig_msg": orig_msg,
            "expected": expected,
            "video_id": EPICURIOUS,
            "ie": "youtube:tab",
        }
    )

    def failing_extract(url, **options):
        raise error

    monkeypatch.setattr(public, "_extract", failing_extract)

    with pytest.raises(PublicSourceError) as raised:
        public.shorts_video_ids(EPICURIOUS)

    assert raised.value.__cause__ is error


def test_search_channels_maps_each_field(fake):
    channels = public.search_channels("epicurious")

    assert fake.calls == [(SEARCH_URL, {})]
    assert len(channels) == 20
    assert fake.entries_handed_out == 20
    assert channels[0] == ChannelInfo(
        channel_id=EPICURIOUS,
        title="Epicurious",
        handle="@epicurious",
        url=f"https://www.youtube.com/channel/{EPICURIOUS}",
        thumbnail_url=(
            "https://yt3.googleusercontent.com/ytc/AIdro_lZx80-3cTYmxKt9xeXzUUkAP9RuDcDT6J0u_5RSRBYHQ"
            "=s176-c-k-c0x00ffffff-no-rj-mo"
        ),
        subscriber_count=6320000,
    )


def test_search_channels_keeps_a_missing_handle_and_subscriber_count_as_none(fake):
    channels = {channel.channel_id: channel for channel in public.search_channels("epicurious", limit=30)}

    topic = channels["UCNQ_n_8sSxAddB5dTjF4ksQ"]
    assert topic.title == "Epicurious - Topic"
    assert topic.handle is None
    assert topic.subscriber_count is None


def test_search_channels_encodes_the_query(fake):
    url = "https://www.youtube.com/results?search_query=bon+app%C3%A9tit+%26+co&sp=EgIQAg%253D%253D"
    fake.recordings[url] = fake.recordings[SEARCH_URL]

    public.search_channels("bon appétit & co", limit=1)

    assert fake.calls == [(url, {})]


@pytest.mark.parametrize(
    ("text", "url"),
    [
        ("@Epicurious", EPICURIOUS_PAGE_URL),
        ("Epicurious", EPICURIOUS_PAGE_URL),
        ("https://www.youtube.com/@Epicurious", EPICURIOUS_PAGE_URL),
        ("  https://youtube.com/@Epicurious/videos  ", EPICURIOUS_PAGE_URL),
        (f"https://www.youtube.com/channel/{WIRED}", WIRED_PAGE_URL),
        (WIRED, WIRED_PAGE_URL),
    ],
)
def test_resolve_channel_accepts_each_input_form(fake, text, url):
    channel = public.resolve_channel(text)

    assert fake.calls == [(url, {"playlist_items": "0"})]
    assert channel.channel_id == (WIRED if url == WIRED_PAGE_URL else EPICURIOUS)


def test_resolve_channel_maps_each_field(fake):
    assert public.resolve_channel("@Epicurious") == ChannelInfo(
        channel_id=EPICURIOUS,
        title="Epicurious",
        handle="@epicurious",
        url=f"https://www.youtube.com/channel/{EPICURIOUS}",
        thumbnail_url=(
            "https://yt3.googleusercontent.com/ytc/AIdro_lZx80-3cTYmxKt9xeXzUUkAP9RuDcDT6J0u_5RSRBYHQ"
            "=s900-c-k-c0x00ffffff-no-rj"
        ),
        subscriber_count=6320000,
    )
    assert public.resolve_channel(WIRED) == ChannelInfo(
        channel_id=WIRED,
        title="WIRED",
        handle="@WIRED",
        url=f"https://www.youtube.com/channel/{WIRED}",
        thumbnail_url=(
            "https://yt3.googleusercontent.com/3xLaRljIs8OZjbxyGnMlpHXtzrxY-keMiy9foklNwa9G5wzB2stC4ND56rZfZAuyLKmRLMm2cik"
            "=s900-c-k-c0x00ffffff-no-rj"
        ),
        subscriber_count=13000000,
    )


@pytest.mark.parametrize("text", ["@thishandledoesnotexist0x9q7z", "UCzzzzzzzzzzzzzzzzzzzzzz"])
def test_resolve_channel_returns_none_for_a_channel_that_does_not_exist(fake, text):
    assert public.resolve_channel(text) is None


@pytest.mark.parametrize("text", ["", "ab", "two words", "https://example.com/@Epicurious", "@"])
def test_resolve_channel_refuses_text_that_is_not_a_channel(fake, text):
    with pytest.raises(ValueError, match="not a YouTube channel id, handle or channel URL"):
        public.resolve_channel(text)

    assert fake.calls == []


@pytest.mark.parametrize(
    ("call", "url"),
    [
        (lambda: public.iter_channel_uploads(NASA), NASA_UPLOADS_URL),
        (lambda: public.shorts_video_ids(EPICURIOUS), EPICURIOUS_SHORTS_URL),
        (lambda: public.search_channels("epicurious"), SEARCH_URL),
        (lambda: public.resolve_channel("@Epicurious"), EPICURIOUS_PAGE_URL),
    ],
    ids=["uploads", "shorts", "search", "resolve"],
)
def test_another_yt_dlp_failure_raises_public_source_error(monkeypatch, call, url):
    error = http_error(429)

    def failing_extract(url, **options):
        raise error

    monkeypatch.setattr(public, "_extract", failing_extract)

    with pytest.raises(PublicSourceError, match="HTTP Error 429") as raised:
        call()

    assert url in str(raised.value)
    assert raised.value.__cause__ is error


def test_a_failure_on_a_later_page_raises_public_source_error(monkeypatch):
    error = ExtractorError("Unable to download API page: HTTP Error 503", expected=True)

    def entries():
        yield from recorded_entries("uploads_nasa")[:2]
        raise error

    monkeypatch.setattr(public, "_extract", lambda url, **options: {"entries": entries()})
    uploads = public.iter_channel_uploads(NASA)
    assert len(list(itertools.islice(uploads, 2))) == 2

    with pytest.raises(PublicSourceError, match="HTTP Error 503") as raised:
        next(uploads)

    assert NASA_UPLOADS_URL in str(raised.value)
    assert raised.value.__cause__ is error


@pytest.mark.parametrize("redirect_type", ["url", "url_transparent"])
def test_fetch_follows_one_redirect_with_the_same_options(fake, redirect_type):
    start = "https://www.youtube.com/@WIREDregional"
    fake.add_redirect(start, WIRED_PAGE_URL, redirect_type)

    channel = public.resolve_channel("@WIREDregional")

    assert channel.channel_id == WIRED
    assert fake.calls == [(start, {"playlist_items": "0"}), (WIRED_PAGE_URL, {"playlist_items": "0"})]


def test_fetch_refuses_a_second_redirect(fake):
    first = "https://www.youtube.com/@first"
    second = "https://www.youtube.com/@second"
    fake.add_redirect(first, second)
    fake.add_redirect(second, WIRED_PAGE_URL, "url_transparent")

    with pytest.raises(PublicSourceError, match="redirected https://www.youtube.com/@first more than once"):
        public.resolve_channel("@first")

    assert [url for url, options in fake.calls] == [first, second]


def test_the_yt_dlp_logger_sends_debug_and_info_to_debug_and_warning_and_error_to_warning(caplog):
    caplog.set_level(logging.DEBUG, logger=public.__name__)

    for method in ("debug", "info", "warning", "error"):
        getattr(public._YT_DLP_LOGGER, method)(f"{method} message")

    assert [(record.levelno, record.getMessage()) for record in caplog.records if record.name == public.__name__] == [
        (logging.DEBUG, "debug message"),
        (logging.DEBUG, "info message"),
        (logging.WARNING, "warning message"),
        (logging.WARNING, "error message"),
    ]


def raise_like_the_extractor(described: dict):
    """Raise the recorded failure the way the youtube:tab extractor raises it."""
    if described["cause"] == "HTTPError":
        try:
            raise HTTPError(Response(io.BytesIO(), described["url"], {}, status=described["status"]))
        except HTTPError as cause:
            raise ExtractorError(f"Unable to download API page: {cause}", cause=cause) from cause
    raise ExtractorError(
        described["orig_msg"], expected=described["expected"], video_id=described["video_id"], ie=described["ie"]
    )


@pytest.mark.parametrize(
    ("recording", "text", "logged"),
    [
        ("missing_handle", "@thishandledoesnotexist0x9q7z", "HTTP Error 404"),
        ("missing_channel_id", "UCzzzzzzzzzzzzzzzzzzzzzz", "This channel does not exist."),
    ],
)
def test_a_missing_channel_writes_nothing_to_stderr_and_logs_a_warning(
    monkeypatch, capsys, caplog, recording, text, logged
):
    described = load(recording)["error"]

    def extract(self, url):
        raise_like_the_extractor(described)

    monkeypatch.setattr(InfoExtractor, "extract", extract)

    assert public.resolve_channel(text) is None

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == public.__name__ and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert logged in warnings[0]
