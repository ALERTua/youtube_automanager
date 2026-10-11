import logging
from types import SimpleNamespace

import pytest

from youtube_automanager.oauth import OAuth
from youtube_automanager.runners.automanage import YoutubeAutoManager

SECRET_REFRESH = "SECRET-REFRESH-123"
SECRET_ACCESS = "SECRET-ACCESS-456"
SECRET_SAVED = "SECRET-SAVED-789"
SECRETS = (SECRET_REFRESH, SECRET_ACCESS, SECRET_SAVED)


def token():
    return {"access_token": SECRET_ACCESS, "refresh_token": SECRET_REFRESH, "expires_at": 1.0, "token_type": "Bearer"}


class FakeSession:
    def __init__(self):
        self.token = None

    def refresh_token(self, token_url):
        return token()


class FakeFlow:
    def __init__(self):
        self.oauth2session = FakeSession()

    def fetch_token(self, authorization_response):
        return token()


class FakeOAuth:
    """Replaces OAuth of the manager: refreshes without a network and hands out the secret tokens."""

    def __init__(self):
        self.session = SimpleNamespace(token={})
        self.refresh_token = SECRET_REFRESH

    def refresh_token_(self):
        return token()


@pytest.fixture
def debug_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="youtube_automanager")
    return caplog


@pytest.fixture
def manager(fake_db):
    config = SimpleNamespace(config={"rules": []}, start_date=None)
    return YoutubeAutoManager(oauth=FakeOAuth(), db=fake_db, config=config)


@pytest.fixture
def oauth():
    oauth = OAuth(
        client_secrets_file="client_secret.json",
        scopes=[],
        host="localhost",
        port=8080,
        redirect_uri="https://localhost/",
        token_url="https://token.example/",
    )
    oauth.__dict__["flow"] = FakeFlow()
    return oauth


def assert_debug_line_without_secrets(caplog, logger_name):
    # a debug record must exist, or the check below would pass on a silent logger
    assert any(r.levelno == logging.DEBUG and r.name == logger_name for r in caplog.records)
    for secret in SECRETS:
        assert secret not in caplog.text


def test_save_token_logs_no_token(manager, fake_db, debug_logs):
    manager.save_token()

    assert fake_db.config.refresh_token == SECRET_REFRESH
    assert_debug_line_without_secrets(debug_logs, "youtube_automanager.runners.automanage")


def test_authorize_with_a_saved_token_logs_no_token(manager, fake_db, debug_logs):
    fake_db.config.refresh_token = SECRET_SAVED

    manager.authorize()

    assert manager.oauth.session.token["refresh_token"] == SECRET_SAVED
    assert any("Authorized using the saved refresh token" in r.getMessage() for r in debug_logs.records)
    assert_debug_line_without_secrets(debug_logs, "youtube_automanager.runners.automanage")


def test_fetch_token_logs_no_token(oauth, debug_logs):
    oauth.fetch_token("https://localhost/?code=abc")

    assert oauth.session.token["refresh_token"] == SECRET_REFRESH
    assert_debug_line_without_secrets(debug_logs, "youtube_automanager.oauth")


def test_refresh_token_logs_no_token(oauth, debug_logs):
    result = oauth.refresh_token_()

    assert result["access_token"] == SECRET_ACCESS
    assert oauth.session.token["access_token"] == SECRET_ACCESS
    assert_debug_line_without_secrets(debug_logs, "youtube_automanager.oauth")
