"""Tests of the YouTube account client on recorded and anonymized answers. They make no network request.

The flat lists come from recorded yt-dlp answers. The API answers come from recorded YouTube answers. A fake transport
replaces ``YoutubeDL.urlopen``, so the real yt-dlp code builds each request and reads each answer.
"""

import copy
import datetime
import io
import itertools
import json
import logging
import re
import socket
from pathlib import Path

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.cookies import YoutubeDLCookieJar
from yt_dlp.networking.common import Response
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import DownloadError, ExtractorError
from yt_dlp.version import __version__ as YTDLP_VERSION

from youtube_automanager.db import make_engine, make_session_factory
from youtube_automanager.db.models import BreakerReason, RequestBudgetState
from youtube_automanager.pacing import BUCKET_SIZE, PAUSE_S, REASON_TEXTS, REFILL_S, RequestBudget
from youtube_automanager.youtube import account, public
from youtube_automanager.youtube.account import (
    AccountClient,
    AccountError,
    AccountIdentity,
    AccountPlaylist,
    FeedVideo,
    NotLoggedInError,
    RequestsPausedError,
    SubscribedChannel,
    WriteRejectedError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "account"
# The time of the wall clock at the zero of the fake clock.
EPOCH = datetime.datetime(2026, 10, 28, 12, 0, tzinfo=datetime.UTC)

ACCOUNT = "UCfakeAccount00000000001"
CHANNEL = "UCfakeChannel00000000001"
VIDEO = "SKOJfQG3qis"
OWN_1 = "PLfakeOwn0000000000000000000000001"
OWN_2 = "PLfakeOwn0000000000000000000000002"
OWN_ONLY_IN_DIALOG = "PLfakeOwn0000000000000000000000999"
FAVORITES = "FLfakeFavorites000000003"
SAVED_4 = "PLfakeSaved00000000000000000000004"
SAVED_5 = "PLfakeSaved00000000000000000000005"

HOME_URL = "https://www.youtube.com"
CHANNELS_URL = "https://www.youtube.com/feed/channels"
PLAYLISTS_URL = "https://www.youtube.com/feed/playlists"
SUBSCRIPTIONS_URL = "https://www.youtube.com/feed/subscriptions"
WATCH_LATER_URL = "https://www.youtube.com/playlist?list=WL"
SIGN_IN_URL = "https://accounts.google.com/v3/signin/identifier?continue=https%3A%2F%2Fwww.youtube.com"

COOKIES = (
    "# Netscape HTTP Cookie File\n"
    "\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tLOGIN_INFO\tfake-login-info\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID\tfake-sapisid\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1893456000\t__Secure-3PAPISID\tfake-3papisid\n"
    ".youtube.com\tTRUE\t/\tFALSE\t1893456000\tSID\tfake-sid\n"
    ".youtube.com\tTRUE\t/\tTRUE\t0\tYSC\tfake-ysc\n"
)

# The config of a logged-in page, with only the keys that yt-dlp reads, and fake values.
YTCFG = {
    "LOGGED_IN": True,
    "INNERTUBE_API_KEY": "fakeApiKey",
    "INNERTUBE_CLIENT_NAME": "WEB",
    "INNERTUBE_CLIENT_VERSION": "2.20261010.00.00",
    "INNERTUBE_CONTEXT_CLIENT_NAME": 1,
    "INNERTUBE_CONTEXT": {
        "client": {"clientName": "WEB", "clientVersion": "2.20261010.00.00", "hl": "en", "visitorData": "fakeVisitor"}
    },
    "VISITOR_DATA": "fakeVisitor",
    "DATASYNC_ID": "fakeUserSession||",
    "SESSION_INDEX": "0",
    "USER_SESSION_ID": "fakeUserSession",
}
ANONYMOUS_YTCFG = {key: value for key, value in YTCFG.items() if key not in ("SESSION_INDEX", "USER_SESSION_ID")} | {
    "LOGGED_IN": False
}


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def page_html(ytcfg: dict, initial_data: dict | None = None) -> str:
    html = f"<html><head><script>ytcfg.set({json.dumps(ytcfg)});</script></head><body>"
    if initial_data is not None:
        html += f"<script>var ytInitialData = {json.dumps(initial_data)};</script>"
    return html + "</body></html>"


class FakeTime:
    """A clock of the wall time, ``now`` seconds after ``EPOCH``, and a sleep that only moves the clock."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self) -> datetime.datetime:
        return EPOCH + datetime.timedelta(seconds=self.now)

    def sleep(self, seconds: float) -> None:
        # A budget that never stops waiting fails the test instead of a hang.
        assert len(self.sleeps) < 200, f"the budget keeps waiting: {self.sleeps[:5]}"
        self.sleeps.append(seconds)
        self.now += seconds


def fake_budget(sessions, fake_time: FakeTime, pause_s: float = PAUSE_S) -> RequestBudget:
    """A budget on the database of ``sessions`` with the fake clock, and the same pause after each answer."""
    return RequestBudget(sessions, clock=fake_time.clock, sleep=fake_time.sleep, random=lambda low, high: pause_s)


def stored_budget(sessions) -> RequestBudgetState | None:
    with sessions() as session:
        return session.get(RequestBudgetState, 1)


class FakeYouTube:
    """Replaces ``YoutubeDL.urlopen``: answers each request from canned pages and API answers, and records it.

    Each answer takes ``answer_s`` seconds on the fake clock, one second unless a test sets another time.
    """

    def __init__(self, fake_time: FakeTime):
        self.time = fake_time
        self.answer_s = 1.0
        self.pages = {}
        self.page_errors = {}
        self.api = {}
        self.requests = []
        self.responses = []

    def page(self, url: str, ytcfg: dict, initial_data: dict | None = None, final_url: str | None = None) -> None:
        self.pages[url] = (page_html(ytcfg, initial_data), final_url or url)

    def answer(self, endpoint: str, *answers) -> None:
        """Answer the API endpoint with these answers in order, and repeat the last one.

        An int is an HTTP error, and a tuple of an int and a text is an HTTP error with that message of YouTube.
        """
        self.api[endpoint] = list(answers)

    def __call__(self, ydl, request):
        body = json.loads(request.data) if request.data else None
        self.requests.append(
            {"method": request.method, "url": request.url, "headers": dict(request.headers), "body": body}
        )
        self.time.now += self.answer_s
        path = request.url.split("?")[0]
        if "/youtubei/v1/" in path:
            endpoint = path.split("/youtubei/v1/")[1]
            answers = self.api[endpoint]
            answer = answers.pop(0) if len(answers) > 1 else answers[0]
            if isinstance(answer, int):
                answer = (answer, "Fake error")
            if isinstance(answer, tuple):
                status, message = answer
                error_body = json.dumps({"error": {"code": status, "message": message}}).encode()
                raise HTTPError(Response(io.BytesIO(error_body), request.url, {}, status=status))
            return self._respond(json.dumps(answer), request.url, "application/json; charset=UTF-8")
        if path.rstrip("/") in self.page_errors:
            status = self.page_errors[path.rstrip("/")]
            raise HTTPError(Response(io.BytesIO(b""), request.url, {}, status=status))
        html, final_url = self.pages[path.rstrip("/")]
        return self._respond(html, final_url, "text/html; charset=utf-8")

    def _respond(self, text: str, url: str, content_type: str) -> Response:
        response = Response(io.BytesIO(text.encode("utf-8")), url, {"Content-Type": content_type})
        self.responses.append(response)
        return response

    def api_requests(self, endpoint: str | None = None) -> list[dict]:
        return [r for r in self.requests if "/youtubei/v1/" in r["url"] and (endpoint is None or endpoint in r["url"])]


class FakeExtract:
    """Replaces ``_AccountYoutubeDL.extract_info``: answers from the recorded flat answers.

    Like a logged-in page, it confirms the login, unless ``logged_in`` is False. It counts each entry it hands out.
    """

    def __init__(self, names=("channels", "playlists", "watch_later", "subscriptions")):
        self.recordings = {recording["url"]: recording for recording in map(load, names)}
        self.calls = []
        self.entries_handed_out = 0
        self.logged_in = True

    def __call__(self, ydl, url, **kwargs):
        self.calls.append((url, kwargs, ydl.params.get("extractor_args")))
        if self.logged_in:
            ydl.ytcfgs.append(YTCFG)
        info = copy.deepcopy(self.recordings[url]["info"])
        info["entries"] = self._hand_out(info["entries"])
        return info

    def _hand_out(self, entries):
        for entry in entries:
            self.entries_handed_out += 1
            yield entry


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test of the account client tried to open a network connection")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture
def fake_time():
    return FakeTime()


@pytest.fixture
def budget(sessions, fake_time):
    return fake_budget(sessions, fake_time)


@pytest.fixture
def client(budget):
    return AccountClient(COOKIES, budget=budget)


@pytest.fixture
def youtube(monkeypatch, fake_time):
    fake = FakeYouTube(fake_time)
    fake.page(HOME_URL, YTCFG)
    monkeypatch.setattr(YoutubeDL, "urlopen", lambda ydl, request: fake(ydl, request))
    return fake


@pytest.fixture
def fake(monkeypatch):
    fake_extract = FakeExtract()
    monkeypatch.setattr(
        account._AccountYoutubeDL, "extract_info", lambda ydl, url, **kwargs: fake_extract(ydl, url, **kwargs)
    )
    return fake_extract


@pytest.fixture
def closes(monkeypatch):
    """Record each close of a yt-dlp instance of the client."""
    closed = []
    real_close = account._AccountYoutubeDL.close

    def close(ydl):
        closed.append(ydl)
        real_close(ydl)

    monkeypatch.setattr(account._AccountYoutubeDL, "close", close)
    return closed


def recorded_response(name: str) -> dict:
    return load(name)["response"]


# The cookie text


def test_client_loads_each_cookie_of_the_netscape_text(client):
    assert {cookie.name: cookie.value for cookie in client._cookiejar} == {
        "LOGIN_INFO": "fake-login-info",
        "SAPISID": "fake-sapisid",
        "__Secure-3PAPISID": "fake-3papisid",
        "SID": "fake-sid",
        "YSC": "fake-ysc",
    }


def test_client_loads_a_text_with_crlf_line_ends_without_a_cr_in_the_values(budget):
    jar = AccountClient(COOKIES.replace("\n", "\r\n"), budget=budget)._cookiejar

    assert {cookie.name: cookie.value for cookie in jar}["YSC"] == "fake-ysc"
    assert not any(cookie.value.endswith("\r") for cookie in jar)


def test_client_loads_an_httponly_line(budget):
    text = COOKIES + "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1893456000\tHSID\tfake-hsid\n"

    assert "HSID" in {cookie.name for cookie in AccountClient(text, budget=budget)._cookiejar}


@pytest.mark.parametrize("text", ["", ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfake-sid\n", '[{"name": "SID"}]'])
def test_client_refuses_a_text_without_the_netscape_header_line(budget, text):
    with pytest.raises(ValueError, match="does not start with the header line"):
        AccountClient(text, budget=budget)


@pytest.mark.parametrize(
    "line",
    [
        ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID",
        ".youtube.com\tTRUE\t/\tTRUE\tsoon\tSAPISID\tsecret-value",
        ".youtube.com\tFALSE\t/\tTRUE\t1893456000\tSAPISID\tsecret-value",
        "   # a comment with leading spaces\tsecret-value",
    ],
    ids=["six fields", "bad expiry", "domain flag", "indented comment"],
)
def test_client_refuses_a_bad_line_and_names_only_its_number(budget, capsys, line):
    text = f"# Netscape HTTP Cookie File\n\n{line}\n"

    with pytest.raises(ValueError, match="line 3 of the cookie text") as raised:
        AccountClient(text, budget=budget)

    assert "secret-value" not in str(raised.value)
    assert capsys.readouterr().err == ""


def test_client_refuses_a_text_without_cookies(budget):
    with pytest.raises(ValueError, match="holds no cookie"):
        AccountClient("# Netscape HTTP Cookie File\n# only a comment\n", budget=budget)


def test_cookies_text_gives_the_cookies_as_a_netscape_cookie_file(client):
    text = client.cookies_text()

    jar = YoutubeDLCookieJar()
    jar.load(io.StringIO(text))
    assert text.startswith("# Netscape HTTP Cookie File")
    assert {cookie.name: cookie.value for cookie in jar} == {cookie.name: cookie.value for cookie in client._cookiejar}
    assert ".youtube.com\tTRUE\t/\tTRUE\t0\tYSC\tfake-ysc" in text.splitlines()


def test_cookies_text_keeps_the_session_cookie_of_the_client_usable(client):
    client.cookies_text()

    assert "YSC=fake-ysc" in client._cookiejar.get_cookie_header("https://www.youtube.com/")


def test_yt_dlp_gets_the_cookies_of_the_client_and_no_cookie_file(client):
    ydl = client._new_youtube_dl()
    try:
        handlers = ydl._request_director.handlers.values()
        assert ydl.params.get("cookiefile") is None
        assert handlers
        assert all(handler.cookiejar is client._cookiejar for handler in handlers)
    finally:
        ydl.close()


# The request budget


def test_a_request_of_the_client_takes_a_token_and_stores_the_end_of_its_answer(client, youtube, fake_time, sessions):
    started = fake_time.clock()

    client.check_session()

    assert len(youtube.requests) == 1
    row = stored_budget(sessions)
    assert (row.tokens, row.refilled_at) == (BUCKET_SIZE - 1, started)
    # The answer took one second on the fake clock, and the budget stored its end.
    assert row.last_answer_at == fake_time.clock() == started + datetime.timedelta(seconds=1)


def test_an_error_answer_also_takes_a_token_and_stores_its_end(client, youtube, fake_time, sessions):
    youtube.page_errors[HOME_URL] = 401
    started = fake_time.clock()

    with pytest.raises(NotLoggedInError):
        client.check_session()

    assert len(youtube.requests) == 1
    row = stored_budget(sessions)
    assert (row.tokens, row.refilled_at) == (BUCKET_SIZE - 1, started)
    assert row.last_answer_at == fake_time.clock() == started + datetime.timedelta(seconds=1)


def test_the_requests_of_the_client_keep_the_pause_after_each_answer(client, youtube, fake_time):
    youtube.answer("account/account_menu", recorded_response("account_menu"))

    client.identity()
    fake_time.now += 0.5
    client.identity()

    assert len(youtube.requests) == 4
    # Each answer ends right before the next request, except the one that was 0.5 s before it.
    assert fake_time.sleeps == pytest.approx([PAUSE_S, PAUSE_S - 0.5, PAUSE_S])


def test_15_requests_go_at_once_and_the_16th_waits_for_the_rest_of_4_s(sessions, youtube, fake_time):
    youtube.answer("account/account_menu", recorded_response("account_menu"))
    youtube.answer_s = 0.25
    waited_before_request = []

    def sleep(seconds):
        waited_before_request.append(len(youtube.requests) + 1)
        fake_time.sleep(seconds)

    budget = RequestBudget(sessions, clock=fake_time.clock, sleep=sleep, random=lambda low, high: 0.0)
    client = AccountClient(COOKIES, budget=budget)

    for _ in range(8):
        client.identity()

    assert len(youtube.requests) == 16
    assert waited_before_request == [16]
    # 15 answers took 3.75 s, so the bucket got 0.9375 tokens back, and the 16th request waits for the rest.
    assert fake_time.sleeps == [pytest.approx(REFILL_S - 15 * 0.25)]


def test_two_clients_on_one_database_share_the_budget(settings, sessions, youtube, fake_time):
    engines = [make_engine(settings.database_url), make_engine(settings.database_url)]
    try:
        first, second = (
            AccountClient(COOKIES, budget=fake_budget(make_session_factory(engine), fake_time)) for engine in engines
        )
        first.check_session()
        fake_time.now += 0.25

        second.check_session()

        # The second client waits for the rest of the pause after the answer of the first one.
        assert fake_time.sleeps == pytest.approx([PAUSE_S - 0.25])
        assert len(youtube.requests) == 2
        assert stored_budget(sessions).tokens == pytest.approx(BUCKET_SIZE - 2 + (1.0 + PAUSE_S) / REFILL_S)
    finally:
        for engine in engines:
            engine.dispose()


def test_an_anonymous_read_takes_no_token(client, monkeypatch):
    calls = []
    requests = []

    def refuse(ydl, request):
        requests.append(request.url)
        raise TransportError("a test of the account client refuses the network")

    monkeypatch.setattr(RequestBudget, "before_request", lambda budget: calls.append("token"))
    monkeypatch.setattr(RequestBudget, "after_answer", lambda budget, status=None: calls.append("answer"))
    monkeypatch.setattr(YoutubeDL, "urlopen", refuse)

    with pytest.raises(public.PublicSourceError):
        list(public.iter_channel_uploads(CHANNEL))
    with pytest.raises(public.PublicSourceError):
        public.search_channels("fake channel name")

    assert requests
    assert calls == []
    # The same fake counts the request of the client, so the empty list above is not a broken fake.
    with pytest.raises(AccountError):
        client.check_session()
    assert calls[:2] == ["token", "answer"]


# The breaker of the request budget

# The texts of YouTube that yt-dlp passes on. YouTube writes the apostrophe of "you're" as U+2019.
BOT_CHECK_TEXT = "Sign in to confirm you’re not a bot. This helps protect our community. Learn more"
TRY_AGAIN_LATER_TEXT = "This content isn't available, try again later."
OPENED_TITLE = "YouTube AutoManager: the requests with cookies are paused"
CLOSED_TITLE = "YouTube AutoManager: the requests with cookies work again"


def alert(text: str) -> dict:
    """An answer with one error alert of YouTube, which yt-dlp raises or reports as "YouTube said: <text>"."""
    return {"alerts": [{"alertRenderer": {"type": "ERROR", "text": {"simpleText": text}}}]}


def breaker(sessions) -> tuple[BreakerReason | None, datetime.timedelta | None]:
    """Return the reason and the length of the stored pause."""
    row = stored_budget(sessions)
    if row.breaker_until is None or row.breaker_opened_at is None:
        return row.breaker_reason, None
    return row.breaker_reason, row.breaker_until - row.breaker_opened_at


def test_http_429_of_a_page_opens_the_breaker_and_the_next_request_fails_without_the_network(client, youtube, sessions):
    youtube.page_errors[HOME_URL] = 429

    with pytest.raises(RequestsPausedError) as raised:
        client.check_session()
    del youtube.page_errors[HOME_URL]
    with pytest.raises(RequestsPausedError) as again:
        client.check_session()

    # The answer ended at 12:16:41 on the fake clock, so the pause ends at 13:16:41.
    assert (
        str(raised.value)
        == str(again.value)
        == (
            "The service sends no request with the cookies of the account until 2026-10-28 13:16 UTC, because YouTube "
            "answered a request with HTTP 429 (too many requests)."
        )
    )
    assert len(youtube.requests) == 1
    assert breaker(sessions) == (BreakerReason.HTTP_429, datetime.timedelta(hours=1))


@pytest.mark.parametrize(
    ("answer", "reason", "hours"),
    [
        (429, BreakerReason.HTTP_429, 1),
        (alert(TRY_AGAIN_LATER_TEXT), BreakerReason.TRY_AGAIN_LATER, 1),
        (alert(BOT_CHECK_TEXT), BreakerReason.BOT_CHECK, 6),
        (alert(BOT_CHECK_TEXT.replace("’", "'")), BreakerReason.BOT_CHECK, 6),
    ],
    ids=["http 429", "try again later", "bot check", "bot check with a plain apostrophe"],
)
def test_each_limit_in_an_api_answer_opens_the_breaker_and_the_call_is_not_sent_again(
    client, youtube, sessions, answer, reason, hours
):
    # A second request of the same call would get the good answer.
    youtube.answer("account/account_menu", answer, recorded_response("account_menu"))

    with pytest.raises(RequestsPausedError, match=re.escape(f"answered a request with {REASON_TEXTS[reason]}.")):
        client.identity()

    assert len(youtube.api_requests("account/account_menu")) == 1
    assert breaker(sessions) == (reason, datetime.timedelta(hours=hours))


def test_a_bot_check_in_the_message_of_an_error_answer_opens_the_breaker(client, youtube, sessions):
    # yt-dlp gives the message of an error answer only in a warning that quotes YouTube, and raises the HTTP error.
    youtube.answer("account/account_menu", (403, BOT_CHECK_TEXT))

    with pytest.raises(RequestsPausedError, match="a bot check") as first:
        client.identity()
    with pytest.raises(RequestsPausedError) as second:
        client.identity()

    assert str(first.value) == str(second.value)
    assert len(youtube.requests) == 2
    assert breaker(sessions) == (BreakerReason.BOT_CHECK, datetime.timedelta(hours=6))


def test_http_429_of_a_list_page_stops_the_api_request_that_yt_dlp_sends_next(client, youtube, sessions):
    # yt-dlp reports an HTTP 429 of a list page as a warning only, and then reads the list through the API.
    youtube.page_errors[CHANNELS_URL] = 429

    with pytest.raises(RequestsPausedError, match="HTTP 429"):
        client.subscribed_channels()

    assert [request["url"] for request in youtube.requests] == [CHANNELS_URL]
    assert breaker(sessions) == (BreakerReason.HTTP_429, datetime.timedelta(hours=1))


def test_a_bot_check_on_a_list_page_fails_the_read_after_one_request(client, youtube, sessions):
    youtube.page(CHANNELS_URL, YTCFG, alert(BOT_CHECK_TEXT))

    with pytest.raises(RequestsPausedError, match="a bot check"):
        client.subscribed_channels()

    assert [request["url"] for request in youtube.requests] == [CHANNELS_URL]
    assert breaker(sessions) == (BreakerReason.BOT_CHECK, datetime.timedelta(hours=6))


def test_a_warning_of_yt_dlp_that_does_not_quote_youtube_opens_no_breaker(client, sessions):
    ydl = client._new_youtube_dl()
    try:
        ydl.report_warning("[youtube:tab] The network failed, try again later")
    finally:
        ydl.close()

    assert stored_budget(sessions) is None


@pytest.mark.parametrize("status", [401, 404])
def test_another_error_answer_leaves_the_breaker_closed(client, youtube, sessions, status):
    youtube.page_errors[HOME_URL] = status

    with pytest.raises(AccountError) as raised:
        client.check_session()

    assert not isinstance(raised.value, RequestsPausedError)
    assert breaker(sessions) == (None, None)


@pytest.mark.parametrize(
    "call",
    [
        AccountClient.check_session,
        AccountClient.identity,
        AccountClient.subscribed_channels,
        lambda client: client.add_video("WL", VIDEO),
        lambda client: client.unsubscribe(CHANNEL),
    ],
    ids=["check_session", "identity", "subscribed_channels", "add_video", "unsubscribe"],
)
def test_during_the_pause_each_call_of_the_client_fails_at_once_without_the_network(client, youtube, budget, call):
    budget.open_breaker(BreakerReason.BOT_CHECK)

    with pytest.raises(RequestsPausedError) as raised:
        call(client)

    assert str(raised.value) == (
        "The service sends no request with the cookies of the account until 2026-10-28 18:16 UTC, because YouTube "
        'answered a request with a bot check ("Sign in to confirm you\'re not a bot").'
    )
    assert youtube.requests == []


def test_an_anonymous_read_goes_on_during_the_pause(budget, monkeypatch):
    budget.open_breaker(BreakerReason.HTTP_429)
    requests = []

    def refuse(ydl, request):
        requests.append(request.url)
        raise TransportError("a test of the account client refuses the network")

    monkeypatch.setattr(YoutubeDL, "urlopen", refuse)

    with pytest.raises(public.PublicSourceError):
        list(public.iter_channel_uploads(CHANNEL))

    assert requests


def test_the_first_answer_after_the_pause_sends_the_end_of_the_breaker(sessions, youtube, fake_time):
    sent = []
    budget = RequestBudget(
        sessions, clock=fake_time.clock, sleep=fake_time.sleep, random=lambda low, high: 0.0, send=sent.append
    )
    client = AccountClient(COOKIES, budget=budget)
    youtube.page_errors[HOME_URL] = 429
    with pytest.raises(RequestsPausedError):
        client.check_session()
    del youtube.page_errors[HOME_URL]
    fake_time.now += 3600

    client.check_session()

    assert [notification.title for notification in sent] == [OPENED_TITLE, CLOSED_TITLE]
    assert breaker(sessions) == (None, datetime.timedelta(hours=1))


def paused_client(sessions, youtube, fake_time, sent) -> AccountClient:
    """A client whose first call met HTTP 429, with the clock at the end of that pause."""
    budget = RequestBudget(
        sessions, clock=fake_time.clock, sleep=fake_time.sleep, random=lambda low, high: 0.0, send=sent.append
    )
    client = AccountClient(COOKIES, budget=budget)
    youtube.page_errors[HOME_URL] = 429
    with pytest.raises(RequestsPausedError):
        client.check_session()
    del youtube.page_errors[HOME_URL]
    fake_time.now += 3600
    return client


def test_a_bot_check_as_the_first_answer_after_the_pause_sends_no_end_notification(sessions, youtube, fake_time):
    sent = []
    client = paused_client(sessions, youtube, fake_time, sent)
    youtube.answer("account/account_menu", alert(BOT_CHECK_TEXT))

    with pytest.raises(RequestsPausedError, match="a bot check"):
        client.identity()

    assert [notification.title for notification in sent] == [OPENED_TITLE, OPENED_TITLE]
    assert breaker(sessions) == (BreakerReason.BOT_CHECK, datetime.timedelta(hours=6))


def test_a_try_again_later_page_as_the_first_answer_after_the_pause_sends_no_end_notification(
    sessions, youtube, fake_time
):
    sent = []
    client = paused_client(sessions, youtube, fake_time, sent)
    # yt-dlp reports the alert of a list page as a warning only.
    youtube.page(CHANNELS_URL, YTCFG, alert(TRY_AGAIN_LATER_TEXT))

    with pytest.raises(RequestsPausedError, match="try again later"):
        client.subscribed_channels()

    assert [notification.title for notification in sent] == [OPENED_TITLE, OPENED_TITLE]
    assert breaker(sessions) == (BreakerReason.TRY_AGAIN_LATER, datetime.timedelta(hours=2))


@pytest.mark.parametrize(
    "call",
    [AccountClient.identity, AccountClient.subscribed_channels, lambda client: client.add_video("WL", VIDEO)],
    ids=["api call", "list page", "change"],
)
def test_the_end_of_a_call_without_a_limit_after_the_pause_sends_the_end_notification(
    sessions, youtube, fake_time, call
):
    sent = []
    client = paused_client(sessions, youtube, fake_time, sent)
    youtube.answer("account/account_menu", recorded_response("account_menu"))
    youtube.answer("browse/edit_playlist", {"status": "STATUS_SUCCEEDED"})
    youtube.page(CHANNELS_URL, YTCFG, load("page_channels")["initial_data"])

    call(client)

    assert [notification.title for notification in sent] == [OPENED_TITLE, CLOSED_TITLE]


def test_an_error_answer_without_a_limit_after_the_pause_also_ends_the_pause(sessions, youtube, fake_time):
    sent = []
    client = paused_client(sessions, youtube, fake_time, sent)
    youtube.answer("account/account_menu", 404)

    with pytest.raises(AccountError) as raised:
        client.identity()

    assert not isinstance(raised.value, RequestsPausedError)
    assert [notification.title for notification in sent] == [OPENED_TITLE, CLOSED_TITLE]


@pytest.mark.parametrize(
    "message",
    [
        "The service is currently unavailable. Please try again later.",
        "This content isn't available, try again later.",
        "Sign in to confirm you’re not a bot.",
    ],
)
def test_the_message_of_a_server_error_opens_no_breaker(client, youtube, sessions, message):
    youtube.answer("account/account_menu", (503, message))

    with pytest.raises(AccountError) as raised:
        client.identity()

    assert not isinstance(raised.value, RequestsPausedError)
    assert "HTTP Error 503" in str(raised.value)
    assert breaker(sessions) == (None, None)


@pytest.mark.parametrize("text", [TRY_AGAIN_LATER_TEXT, BOT_CHECK_TEXT])
def test_a_server_error_whose_text_names_a_limit_page_opens_no_breaker(budget, sessions, text):
    cause = HTTPError(Response(io.BytesIO(b""), HOME_URL, {}, status=503))
    error = ExtractorError(f"YouTube said: {text}", cause=cause, expected=True)

    mapped = account._account_error(error, "read the account menu", budget)

    assert type(mapped) is AccountError
    assert breaker(sessions) == (None, None)


@pytest.mark.parametrize(
    "text",
    ["Something went wrong. Please try again later.", "Try again later.", "This video isn't available any more"],
)
def test_another_alert_that_asks_to_try_again_later_opens_no_breaker(client, youtube, sessions, text):
    youtube.answer("account/account_menu", alert(text))

    with pytest.raises(AccountError) as raised:
        client.identity()

    assert not isinstance(raised.value, RequestsPausedError)
    assert breaker(sessions) == (None, None)


def test_the_try_again_later_page_also_matches_with_the_apostrophe_of_youtube(client, youtube, sessions):
    youtube.answer("account/account_menu", alert(TRY_AGAIN_LATER_TEXT.replace("'", "’")))

    with pytest.raises(RequestsPausedError):
        client.identity()

    assert breaker(sessions) == (BreakerReason.TRY_AGAIN_LATER, datetime.timedelta(hours=1))


# The login check on the real yt-dlp extractor


def test_subscribed_channels_through_yt_dlp_on_a_logged_in_page(client, youtube):
    youtube.page(CHANNELS_URL, YTCFG, load("page_channels")["initial_data"])

    channels = client.subscribed_channels()

    assert channels == [
        SubscribedChannel(
            channel_id=CHANNEL,
            title="Fake channel 1",
            handle="@fakehandle1",
            thumbnail_url="https://yt3.ggpht.com/fake-avatar-1=s176-c-k-c0x00ffffff-no-rj",
        )
    ]
    assert [request["url"] for request in youtube.requests] == [CHANNELS_URL]


@pytest.mark.parametrize(
    "call",
    [
        lambda client: client.playlists(),
        lambda client: client.subscribed_channels(),
        lambda client: client.playlist_video_ids("WL"),
        lambda client: client.iter_subscription_feed(),
    ],
    ids=["playlists", "channels", "playlist", "feed"],
)
def test_a_page_for_an_anonymous_user_raises_not_logged_in_after_one_request(client, youtube, call):
    for url in (PLAYLISTS_URL, CHANNELS_URL, WATCH_LATER_URL.split("?")[0], SUBSCRIPTIONS_URL):
        youtube.page(url, ANONYMOUS_YTCFG)

    with pytest.raises(NotLoggedInError, match="as to an anonymous user"):
        call(client)

    assert len(youtube.requests) == 1


def test_a_redirect_to_the_sign_in_page_raises_not_logged_in_after_one_request(client, youtube):
    youtube.page(CHANNELS_URL, {}, final_url=SIGN_IN_URL)

    with pytest.raises(NotLoggedInError, match="Google sign-in page"):
        client.subscribed_channels()

    assert len(youtube.requests) == 1
    assert youtube.responses[0].fp.closed


def test_a_read_closes_yt_dlp_when_the_login_check_fails(client, youtube, closes):
    youtube.page(PLAYLISTS_URL, ANONYMOUS_YTCFG)

    with pytest.raises(NotLoggedInError):
        client.playlists()

    assert len(closes) == 1


# The session check


def test_check_session_reads_the_page_config_of_the_home_page_once(client, youtube, closes):
    assert client.check_session() is None

    assert [(request["method"], request["url"]) for request in youtube.requests] == [("GET", HOME_URL)]
    assert len(closes) == 1


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (lambda youtube: youtube.page(HOME_URL, ANONYMOUS_YTCFG), "as to an anonymous user"),
        (lambda youtube: youtube.page(HOME_URL, {}, final_url=SIGN_IN_URL), "Google sign-in page"),
        (lambda youtube: youtube.page_errors.update({HOME_URL: 401}), "HTTP 401"),
    ],
    ids=["anonymous", "sign-in page", "401"],
)
def test_check_session_of_a_dead_session_raises_not_logged_in_after_one_request(
    client, youtube, closes, answer, message
):
    answer(youtube)

    with pytest.raises(NotLoggedInError, match=message):
        client.check_session()

    assert len(youtube.requests) == 1
    assert len(closes) == 1


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (lambda youtube: youtube.pages.update({HOME_URL: ("<html></html>", HOME_URL)}), "no page config"),
        (lambda youtube: youtube.page_errors.update({HOME_URL: 503}), "HTTP Error 503"),
    ],
    ids=["no config", "503"],
)
def test_check_session_without_a_clear_answer_raises_account_error(client, youtube, answer, message):
    answer(youtube)

    with pytest.raises(AccountError, match=message) as raised:
        client.check_session()

    assert not isinstance(raised.value, NotLoggedInError)


def test_use_cookies_replaces_the_cookies_and_keeps_the_budget(client, youtube, fake_time, sessions):
    client.check_session()

    client.use_cookies(COOKIES.replace("fake-sid", "fake-sid-2"))
    client.check_session()

    assert {cookie.name: cookie.value for cookie in client._cookiejar}["SID"] == "fake-sid-2"
    assert fake_time.sleeps == [PAUSE_S]
    assert stored_budget(sessions).tokens == pytest.approx(BUCKET_SIZE - 2 + (1.0 + PAUSE_S) / REFILL_S)


def test_use_cookies_refuses_a_bad_text_and_keeps_the_cookies(client):
    with pytest.raises(ValueError, match="does not start with the header line"):
        client.use_cookies("SID=fake-sid-2")

    assert {cookie.name: cookie.value for cookie in client._cookiejar}["SID"] == "fake-sid"


# The account menu and the internal API


def test_identity_reads_the_account_menu_with_the_cookies_of_the_account(client, youtube):
    youtube.answer("account/account_menu", recorded_response("account_menu"))

    assert client.identity() == AccountIdentity(channel_id=ACCOUNT, title="Fake account", handle="@fakeaccount")

    home, menu = youtube.requests
    assert (home["method"], home["url"]) == ("GET", HOME_URL)
    assert menu["method"] == "POST"
    assert menu["url"].startswith("https://www.youtube.com/youtubei/v1/account/account_menu?")
    assert menu["body"]["context"]["client"]["clientName"] == "WEB"
    assert menu["headers"]["Authorization"].startswith("SAPISIDHASH ")
    assert menu["headers"]["X-Youtube-Bootstrap-Logged-In"] == "true"


def test_identity_of_an_account_without_a_channel(client, youtube):
    menu = recorded_response("account_menu")
    header = menu["actions"][0]["openPopupAction"]["popup"]["multiPageMenuRenderer"]["header"]
    del header["activeAccountHeaderRenderer"]["manageAccountTitle"]
    del header["activeAccountHeaderRenderer"]["channelHandle"]
    youtube.answer("account/account_menu", menu)

    assert client.identity() == AccountIdentity(channel_id=None, title="Fake account", handle=None)


def test_identity_refuses_a_menu_without_an_account_name(client, youtube):
    youtube.answer("account/account_menu", {"responseContext": {}, "actions": []})

    with pytest.raises(AccountError, match="no account name"):
        client.identity()


def test_an_anonymous_home_page_stops_a_call_before_the_api_request(client, youtube):
    youtube.page(HOME_URL, ANONYMOUS_YTCFG)

    with pytest.raises(NotLoggedInError):
        client.add_video("WL", VIDEO)

    assert youtube.api_requests() == []


def test_http_401_of_the_api_raises_not_logged_in(client, youtube):
    youtube.answer("account/account_menu", 401)

    with pytest.raises(NotLoggedInError, match="HTTP 401") as raised:
        client.identity()

    assert isinstance(raised.value.__cause__, ExtractorError)


@pytest.mark.parametrize(
    ("call", "fixture"),
    [
        (lambda client: client.add_video("WL", VIDEO), "edit_playlist_add"),
        (lambda client: client.remove_video("WL", VIDEO), "edit_playlist_remove"),
    ],
    ids=["add", "remove"],
)
def test_a_playlist_change_sends_the_recorded_request_and_accepts_the_recorded_answer(client, youtube, call, fixture):
    recorded = load(fixture)
    youtube.answer("browse/edit_playlist", recorded["response"])

    assert call(client) is None

    [request] = youtube.api_requests("browse/edit_playlist")
    assert {key: request["body"][key] for key in recorded["query"]} == recorded["query"]
    assert request["headers"]["Authorization"].startswith("SAPISIDHASH ")


@pytest.mark.parametrize("answer", [{"status": "STATUS_FAILED"}, {"responseContext": {}}], ids=["failed", "no status"])
def test_a_playlist_change_without_success_raises_write_rejected(client, youtube, answer):
    youtube.answer("browse/edit_playlist", answer)

    with pytest.raises(WriteRejectedError, match=f"add video {VIDEO} to playlist WL"):
        client.add_video("WL", VIDEO)


def test_a_change_goes_to_youtube_only_once_when_youtube_fails(client, youtube):
    youtube.answer("browse/edit_playlist", 500)

    with pytest.raises(AccountError, match="HTTP Error 500") as raised:
        client.remove_video("WL", VIDEO)

    assert not isinstance(raised.value, NotLoggedInError)
    assert len(youtube.api_requests("browse/edit_playlist")) == 1


def test_a_change_refused_with_http_401_raises_not_logged_in(client, youtube):
    youtube.answer("browse/edit_playlist", 401)

    with pytest.raises(NotLoggedInError):
        client.add_video("WL", VIDEO)

    assert len(youtube.api_requests("browse/edit_playlist")) == 1


@pytest.mark.parametrize(
    "call",
    [
        lambda client: client.add_video("WL", "short"),
        lambda client: client.add_video("WL", "SKOJfQG3qis&x"),
        lambda client: client.remove_video("WL?x=1", VIDEO),
        lambda client: client.add_video("", VIDEO),
        lambda client: client.subscribe("@handle"),
        lambda client: client.unsubscribe(CHANNEL + "x"),
        lambda client: client.playlist_video_ids("WL&index=2"),
    ],
)
def test_a_bad_id_raises_value_error_before_any_request(client, youtube, call):
    with pytest.raises(ValueError, match="not a YouTube"):
        call(client)

    assert youtube.requests == []


def subscribe_answer(channel_id: str, subscribed: bool) -> dict:
    return {
        "responseContext": {},
        "actions": [{"updateSubscribeButtonAction": {"subscribed": subscribed, "channelId": channel_id}}],
    }


@pytest.mark.parametrize(
    ("call", "endpoint", "subscribed"),
    [
        (lambda client: client.subscribe(CHANNEL), "subscription/subscribe", True),
        (lambda client: client.unsubscribe(CHANNEL), "subscription/unsubscribe", False),
    ],
    ids=["subscribe", "unsubscribe"],
)
def test_a_subscription_change_sends_the_channel_id(client, youtube, call, endpoint, subscribed):
    youtube.answer(endpoint, subscribe_answer(CHANNEL, subscribed))

    assert call(client) is None

    [request] = youtube.api_requests(endpoint)
    assert request["body"]["channelIds"] == [CHANNEL]


@pytest.mark.parametrize(
    "answer",
    [
        {"responseContext": {}},
        subscribe_answer(CHANNEL, False),
        subscribe_answer("UCfakeChannel00000000002", True),
        {**subscribe_answer(CHANNEL, True), "status": "STATUS_FAILED"},
    ],
    ids=["no state", "not subscribed", "other channel", "failed status"],
)
def test_a_subscription_change_without_confirmation_raises_write_rejected(client, youtube, answer):
    youtube.answer("subscription/subscribe", answer)

    with pytest.raises(WriteRejectedError, match=f"subscribe to channel {CHANNEL}"):
        client.subscribe(CHANNEL)

    assert len(youtube.api_requests("subscription/subscribe")) == 1


# The lists of the account


def test_subscribed_channels_maps_each_field(client, fake):
    channels = client.subscribed_channels()

    assert fake.calls == [(CHANNELS_URL, {"download": False, "process": False, "ie_key": "YoutubeTab"}, None)]
    assert channels == [
        SubscribedChannel(
            channel_id="UCfakeChannel00000000001",
            title="Fake channel 1",
            handle="@fakehandle1",
            thumbnail_url="https://yt3.ggpht.com/fake-avatar-1=s176-c-k-c0x00ffffff-no-rj",
        ),
        SubscribedChannel(
            channel_id="UCfakeChannel00000000002",
            title="Fake channel 2",
            handle=None,
            thumbnail_url="https://yt3.ggpht.com/fake-avatar-2=s176-c-k-c0x00ffffff-no-rj",
        ),
        SubscribedChannel(
            channel_id="UCfakeChannel00000000003",
            title="Fake channel 3",
            handle="@fakehandle2",
            thumbnail_url="https://yt3.ggpht.com/fake-avatar-3=s176-c-k-c0x00ffffff-no-rj",
        ),
    ]


def test_subscribed_channels_refuses_an_entry_without_a_channel_id(client, fake):
    entry = fake.recordings[CHANNELS_URL]["info"]["entries"][1]
    entry["id"] = entry["channel_id"] = None

    with pytest.raises(AccountError, match="has no channel id"):
        client.subscribed_channels()


def test_playlists_marks_each_playlist_of_the_save_dialog_as_own(client, fake, youtube):
    recorded = load("save_dialog")
    youtube.answer("playlist/get_add_to_playlist", recorded["response"])

    playlists = client.playlists()

    assert playlists == [
        AccountPlaylist(playlist_id="LL", title="Liked videos", is_own=False),
        AccountPlaylist(playlist_id="WL", title="Watch later", is_own=True),
        AccountPlaylist(playlist_id=OWN_1, title="Fake playlist 3", is_own=True),
        AccountPlaylist(playlist_id="YS", title="Saved Shorts", is_own=False),
        AccountPlaylist(playlist_id=OWN_2, title="Fake playlist 5", is_own=True),
        AccountPlaylist(playlist_id=FAVORITES, title="Favorites", is_own=True),
        AccountPlaylist(playlist_id=SAVED_4, title="Fake playlist 7", is_own=False),
        AccountPlaylist(playlist_id=SAVED_5, title="Fake playlist 8", is_own=False),
        AccountPlaylist(playlist_id=OWN_ONLY_IN_DIALOG, title="Fake playlist only in the dialog", is_own=True),
    ]
    assert fake.calls == [(PLAYLISTS_URL, {"download": False, "process": False, "ie_key": "YoutubeTab"}, None)]
    # The dialog uses the config of the library page, so the client reads no home page.
    [request] = youtube.requests
    assert "/youtubei/v1/playlist/get_add_to_playlist" in request["url"]
    assert {key: request["body"][key] for key in recorded["query"]} == recorded["query"]


def test_playlists_refuses_a_save_dialog_without_watch_later(client, fake, youtube):
    response = load("save_dialog")["response"]
    options = response["contents"][0]["addToPlaylistRenderer"]["playlists"]
    options[:] = [o for o in options if o["playlistAddToOptionRenderer"]["playlistId"] != "WL"]
    youtube.answer("playlist/get_add_to_playlist", response)

    with pytest.raises(AccountError, match="does not list Watch Later"):
        client.playlists()


def test_playlist_video_ids_keeps_the_playlist_order(client, fake):
    assert client.playlist_video_ids("WL") == [VIDEO, "fakeVideo01", "fakeVideo02", "fakeVideo03"]
    assert fake.calls == [(WATCH_LATER_URL, {"download": False, "process": False, "ie_key": "YoutubeTab"}, None)]


def test_iter_subscription_feed_maps_each_field(client, fake):
    videos = list(client.iter_subscription_feed())

    assert fake.calls == [
        (
            SUBSCRIPTIONS_URL,
            {"download": False, "process": False, "ie_key": "YoutubeTab"},
            {"youtubetab": {"approximate_date": [""]}},
        )
    ]
    assert videos == [
        FeedVideo(
            video_id="fakeVideo04",
            title="Fake video 1",
            channel_id="UCfakeChannel00000000008",
            channel_title="Fake channel 8",
            duration_s=607,
            upload_date=datetime.date(2026, 10, 11),
            live_status=None,
        ),
        FeedVideo(
            video_id="fakeVideo03",
            title="Fake video 2",
            channel_id=None,
            channel_title="Fake channel A and Fake channel B 2",
            duration_s=1214,
            upload_date=datetime.date(2026, 10, 10),
            live_status=None,
        ),
        FeedVideo(
            video_id="fakeVideo05",
            title="Fake video 3",
            channel_id="UCfakeChannel00000000009",
            channel_title="Fake channel 9",
            duration_s=1821,
            upload_date=datetime.date(2026, 10, 9),
            live_status="was_live",
        ),
        FeedVideo(
            video_id="fakeVideo06",
            title="Fake video 4",
            channel_id="UCfakeChannel00000000010",
            channel_title="Fake channel 10",
            duration_s=2428,
            upload_date=datetime.date(2026, 10, 8),
            live_status=None,
        ),
    ]


def test_iter_subscription_feed_takes_each_entry_only_when_the_iteration_reaches_it(client, fake):
    feed = client.iter_subscription_feed()
    assert fake.entries_handed_out == 0

    first = list(itertools.islice(feed, 2))

    assert [video.video_id for video in first] == ["fakeVideo04", "fakeVideo03"]
    assert fake.entries_handed_out == 2


def test_iter_subscription_feed_skips_a_link_that_is_not_a_video(client, fake):
    # A shelf link as yt-dlp gives it for a shelf of the feed: no id, and the tab extractor.
    shelf = {"_type": "url", "ie_key": "YoutubeTab", "url": "https://www.youtube.com/feed/subscriptions/shorts"}
    fake.recordings[SUBSCRIPTIONS_URL]["info"]["entries"].insert(1, shelf)

    assert [video.video_id for video in client.iter_subscription_feed()] == [
        "fakeVideo04",
        "fakeVideo03",
        "fakeVideo05",
        "fakeVideo06",
    ]


def test_a_read_closes_yt_dlp_when_the_entries_end(client, fake, closes):
    feed = client.iter_subscription_feed()
    next(feed)
    assert closes == []

    list(feed)

    assert len(closes) == 1


def test_a_read_closes_yt_dlp_when_the_caller_stops_early(client, fake, closes):
    feed = client.iter_subscription_feed()
    next(feed)
    feed.close()

    assert len(closes) == 1


def test_a_read_without_a_confirmed_login_raises_account_error(client, fake, closes):
    fake.logged_in = False

    with pytest.raises(AccountError, match="login is not confirmed"):
        client.subscribed_channels()

    assert len(closes) == 1


def test_a_read_that_yt_dlp_does_not_give_as_a_list_raises_account_error(client, fake):
    fake.recordings[CHANNELS_URL]["info"] = {"_type": "url", "url": "https://www.youtube.com/", "entries": []}

    with pytest.raises(AccountError, match="did not read https://www.youtube.com/feed/channels as a list"):
        client.subscribed_channels()


def download_error(cause: Exception) -> DownloadError:
    return DownloadError(f"ERROR: [youtube:tab] x: {cause}", (type(cause), cause, None))


def http_error(status: int) -> ExtractorError:
    cause = HTTPError(Response(io.BytesIO(), "https://www.youtube.com/youtubei/v1/browse", {}, status=status))
    return ExtractorError(f"Unable to download API page: {cause}", cause=cause)


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        (download_error(http_error(401)), NotLoggedInError),
        (download_error(http_error(503)), AccountError),
        (download_error(ExtractorError("YouTube said: The playlist does not exist.", expected=True)), AccountError),
    ],
    ids=["401", "503", "missing playlist"],
)
def test_a_yt_dlp_failure_raises_an_account_error(monkeypatch, client, closes, error, raised):
    def failing_extract_info(ydl, url, **kwargs):
        raise error

    monkeypatch.setattr(account._AccountYoutubeDL, "extract_info", failing_extract_info)

    with pytest.raises(AccountError) as caught:
        client.playlist_video_ids("PLfakeOwn0000000000000000000000001")

    assert type(caught.value) is raised
    assert caught.value.__cause__ is error
    assert "https://www.youtube.com/playlist?list=PLfakeOwn0000000000000000000000001" in str(caught.value)
    assert len(closes) == 1


@pytest.mark.parametrize(("status", "raised"), [(401, NotLoggedInError), (503, AccountError)])
def test_a_failure_on_a_later_page_raises_an_account_error(monkeypatch, client, closes, status, raised):
    error = http_error(status)

    def entries():
        yield {"_type": "url", "ie_key": "Youtube", "id": VIDEO}
        raise error

    def extract_info(ydl, url, **kwargs):
        ydl.ytcfgs.append(YTCFG)
        return {"_type": "playlist", "entries": entries()}

    monkeypatch.setattr(account._AccountYoutubeDL, "extract_info", extract_info)
    feed = client.iter_subscription_feed()
    next(feed)

    with pytest.raises(AccountError) as caught:
        next(feed)

    assert type(caught.value) is raised
    assert caught.value.__cause__ is error
    assert len(closes) == 1


def test_the_yt_dlp_messages_go_to_the_log_of_the_account_module(caplog):
    caplog.set_level(logging.DEBUG, logger=account.__name__)

    for method in ("debug", "info", "warning", "error"):
        getattr(account._YT_DLP_LOGGER, method)(f"{method} message")

    assert [(record.levelno, record.getMessage()) for record in caplog.records if record.name == account.__name__] == [
        (logging.DEBUG, "debug message"),
        (logging.DEBUG, "info message"),
        (logging.WARNING, "warning message"),
        (logging.WARNING, "error message"),
    ]


# A yt-dlp release that changed an internal of the client


def tab_class():
    ydl = YoutubeDL()
    try:
        return type(ydl.get_info_extractor("YoutubeTab"))
    finally:
        ydl.close()


def missing(name):
    """An attribute that is gone, as after a rename in a new release of yt-dlp: each lookup raises AttributeError."""

    def lookup(self):
        raise AttributeError(f"'{type(self).__name__}' object has no attribute {name!r}")

    return property(lookup)


def raising(error):
    def call(self, *args, **kwargs):
        raise error

    return call


def call_check_session(client, youtube):
    client.check_session()


def call_identity(client, youtube):
    youtube.answer("account/account_menu", recorded_response("account_menu"))
    client.identity()


@pytest.mark.parametrize(
    ("name", "replacement", "work", "call", "kind"),
    [
        ("_download_webpage", missing("_download_webpage"), call_check_session, "_download_webpage", AttributeError),
        (
            "initialize",
            raising(AttributeError("no _real_initialize")),
            call_check_session,
            "initialize",
            AttributeError,
        ),
        ("extract_ytcfg", raising(TypeError("takes 2 arguments")), call_check_session, "extract_ytcfg", TypeError),
        ("_extract_response", missing("_extract_response"), call_identity, "_extract_response", AttributeError),
        (
            "generate_api_headers",
            raising(KeyError("INNERTUBE_CONTEXT")),
            call_identity,
            "generate_api_headers",
            KeyError,
        ),
    ],
    ids=["download_webpage", "initialize", "extract_ytcfg", "extract_response", "generate_api_headers"],
)
def test_a_changed_internal_of_the_tab_extractor_raises_an_account_error_with_the_version_and_the_call(
    monkeypatch, client, youtube, caplog, name, replacement, work, call, kind
):
    monkeypatch.setattr(tab_class(), name, replacement)

    with pytest.raises(AccountError) as caught:
        work(client, youtube)

    assert not isinstance(caught.value, NotLoggedInError)
    assert isinstance(caught.value.__cause__, kind)
    assert f"yt-dlp {YTDLP_VERSION} does not fit the client" in str(caught.value)
    assert f"YoutubeTab.{call} raised {kind.__name__}" in str(caught.value)
    (record,) = [record for record in caplog.records if record.exc_info is not None]
    assert record.exc_info[1] is caught.value.__cause__
    assert "fake-" not in caplog.text


@pytest.mark.parametrize("kind", [AttributeError, TypeError, KeyError])
def test_a_changed_extract_info_raises_an_account_error(monkeypatch, client, closes, kind):
    monkeypatch.setattr(account._AccountYoutubeDL, "extract_info", raising(kind("changed")))

    with pytest.raises(AccountError, match=f"YoutubeDL.extract_info raised {kind.__name__}") as caught:
        client.subscribed_channels()

    assert isinstance(caught.value.__cause__, kind)
    assert len(closes) == 1


def test_a_changed_entry_on_a_later_page_raises_an_account_error(monkeypatch, client, closes):
    def entries():
        yield {"_type": "url", "ie_key": "Youtube", "id": VIDEO}
        raise KeyError("continuationItems")

    def extract_info(ydl, url, **kwargs):
        ydl.ytcfgs.append(YTCFG)
        return {"_type": "playlist", "entries": entries()}

    monkeypatch.setattr(account._AccountYoutubeDL, "extract_info", extract_info)
    feed = client.iter_subscription_feed()
    next(feed)

    with pytest.raises(AccountError, match=f"the entries of {SUBSCRIPTIONS_URL} raised KeyError"):
        next(feed)

    assert len(closes) == 1


def test_an_error_of_the_own_code_of_the_client_stays_as_it_is(monkeypatch, client, fake):
    def broken(entry):
        raise KeyError("a defect of the client")

    monkeypatch.setattr(account, "_subscribed_channel", broken)

    with pytest.raises(KeyError, match="a defect of the client"):
        client.subscribed_channels()


def test_an_error_of_the_login_check_of_the_client_stays_as_it_is(monkeypatch, client, youtube):
    monkeypatch.setattr(account._AccountYoutubeDL, "_check_login", raising(TypeError("a defect of the client")))

    with pytest.raises(TypeError, match="a defect of the client"):
        client.check_session()


def test_the_locked_yt_dlp_has_each_internal_of_the_client():
    assert account.missing_internals() == []
    assert set(account.YTDLP_INTERNALS) == {"YoutubeDL", "YoutubeTab"}


@pytest.mark.parametrize(
    ("owner", "name"),
    [("YoutubeTab", "_extract_response"), ("YoutubeTab", "generate_api_headers"), ("YoutubeDL", "urlopen")],
)
def test_missing_internals_names_each_internal_that_a_release_lacks(monkeypatch, owner, name):
    monkeypatch.setattr(YoutubeDL if owner == "YoutubeDL" else tab_class(), name, missing(name))

    assert account.missing_internals() == [f"{owner}.{name}"]
