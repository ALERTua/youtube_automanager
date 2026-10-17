import pytest
import yt_dlp.networking.common as networking
from yt_dlp import YoutubeDL
from yt_dlp.networking.exceptions import TransportError

from youtube_automanager import rotation
from youtube_automanager.youtube import _ytdlp, public
from youtube_automanager.youtube.account import AccountClient, AccountError
from youtube_automanager.youtube.public import PublicSourceError
from youtube_automanager.youtube.user_agent import USER_AGENT

CHANNEL = "UCLA_DiR1FfKNvjuUpBHmylQ"
COOKIES = (
    "# Netscape HTTP Cookie File\n"
    "\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tLOGIN_INFO\tfake-login-info\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID\tfake-sapisid\n"
)


@pytest.fixture
def sent_agents(monkeypatch):
    """The User-Agent header of each request of yt-dlp. Each request fails before it reaches the network."""
    agents = []
    merge = networking.RequestHandler._merge_headers

    def merge_and_refuse(self, request_headers):
        agents.append(merge(self, request_headers).get("User-Agent"))
        raise TransportError("network access in a test")

    monkeypatch.setattr(networking.RequestHandler, "_merge_headers", merge_and_refuse)
    return agents


def test_the_rotation_and_yt_dlp_name_the_firefox_of_the_cookie_export():
    assert "Firefox/" in USER_AGENT
    assert rotation.USER_AGENT is USER_AGENT
    assert _ytdlp.BASE_OPTIONS["http_headers"] == {"User-Agent": USER_AGENT}


def test_the_base_options_replace_the_random_user_agent_of_yt_dlp_and_keep_its_other_headers():
    with YoutubeDL({"quiet": True}) as plain:
        default = plain.params["http_headers"]
    with YoutubeDL(dict(_ytdlp.BASE_OPTIONS)) as ydl:
        headers = ydl.params["http_headers"]

    assert "Chrome/" in default["User-Agent"]
    assert headers["User-Agent"] == USER_AGENT
    assert {name: value for name, value in headers.items() if name != "User-Agent"} == {
        name: value for name, value in default.items() if name != "User-Agent"
    }


def test_the_api_headers_of_the_web_client_leave_the_user_agent_to_the_options():
    with YoutubeDL(dict(_ytdlp.BASE_OPTIONS)) as ydl:
        headers = ydl.get_info_extractor("YoutubeTab").generate_api_headers(ytcfg={})

    assert "User-Agent" not in headers


def test_a_public_read_sends_the_user_agent(sent_agents):
    with pytest.raises(PublicSourceError):
        list(public.iter_channel_uploads(CHANNEL))

    assert sent_agents
    assert set(sent_agents) == {USER_AGENT}


def test_a_session_check_with_the_cookies_sends_the_user_agent(sent_agents):
    client = AccountClient(COOKIES, pause_s=0)

    with pytest.raises(AccountError):
        client.check_session()

    assert sent_agents
    assert set(sent_agents) == {USER_AGENT}
