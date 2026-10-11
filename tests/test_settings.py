from pathlib import Path

import pytest

from youtube_automanager.settings import (
    HOME_DEFAULT,
    TOKEN_URL,
    YOUTUBE_API_SERVICE_NAME,
    YOUTUBE_API_VERSION,
    YOUTUBE_READ_WRITE_SCOPE,
    Settings,
    get_settings,
)

VARIABLES = [
    "USERNAME",
    "HOME",
    "SECRETS_FILENAME",
    "DB_FILENAME",
    "CONFIG_FILENAME",
    "PORT",
    "HOST",
    "SCOPES",
    "REDIRECT_URI",
    "VERBOSE",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_ANNOUNCE",
    "DISCORD_WEBHOOK_URL",
    "SLACK_WEBHOOK_URL",
    "SLACK_CHANNEL",
    "SLACK_USER_MENTIONS",
    "TEAMS_WEBHOOK_URL",
    "TEAMS_USER_MENTIONS",
]


@pytest.fixture
def env(monkeypatch):
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_variables_cover_each_field():
    assert sorted(VARIABLES) == sorted(name.upper() for name in Settings.model_fields)


def test_defaults_with_an_empty_environment(env):
    settings = Settings()

    assert settings.username == "user"
    assert settings.home == Path("/app/config")
    assert settings.secrets_filename == "client_secret.json"
    assert settings.db_filename == "youtube_automanager.sqlite"
    assert settings.config_filename == "youtube_automanager.yaml"
    assert settings.port == 8080
    assert settings.host == "localhost"
    assert settings.scopes == [YOUTUBE_READ_WRITE_SCOPE]
    assert settings.redirect_uri is None
    assert settings.verbose is False
    assert settings.telegram_bot_token == ""
    assert settings.telegram_chat_id == ""
    assert settings.telegram_announce is False
    assert settings.discord_webhook_url == ""
    assert settings.slack_webhook_url == ""
    assert settings.slack_channel == ""
    assert settings.slack_user_mentions == ""
    assert settings.teams_webhook_url == ""
    assert settings.teams_user_mentions == ""


@pytest.mark.parametrize(
    ("variable", "value", "field", "expected"),
    [
        ("USERNAME", "alice", "username", "alice"),
        ("HOME", "/data", "home", Path("/data")),
        ("SECRETS_FILENAME", "my_secrets.json", "secrets_filename", "my_secrets.json"),
        ("DB_FILENAME", "my.sqlite", "db_filename", "my.sqlite"),
        ("CONFIG_FILENAME", "my.yaml", "config_filename", "my.yaml"),
        ("PORT", "443", "port", 443),
        ("HOST", "0.0.0.0", "host", "0.0.0.0"),
        ("SCOPES", "scope-a", "scopes", ["scope-a"]),
        ("REDIRECT_URI", "https://example.com/", "redirect_uri", "https://example.com/"),
        ("VERBOSE", "True", "verbose", True),
        ("TELEGRAM_BOT_TOKEN", "bot", "telegram_bot_token", "bot"),
        ("TELEGRAM_CHAT_ID", "42", "telegram_chat_id", "42"),
        ("TELEGRAM_ANNOUNCE", "True", "telegram_announce", True),
        ("DISCORD_WEBHOOK_URL", "https://discord.example/hook", "discord_webhook_url", "https://discord.example/hook"),
        ("SLACK_WEBHOOK_URL", "https://slack.example/hook", "slack_webhook_url", "https://slack.example/hook"),
        ("SLACK_CHANNEL", "#general", "slack_channel", "#general"),
        ("SLACK_USER_MENTIONS", "a b", "slack_user_mentions", "a b"),
        ("TEAMS_WEBHOOK_URL", "https://teams.example/hook", "teams_webhook_url", "https://teams.example/hook"),
        ("TEAMS_USER_MENTIONS", "c d", "teams_user_mentions", "c d"),
    ],
)
def test_variable_overrides_its_field(env, variable, value, field, expected):
    env.setenv(variable, value)

    assert getattr(Settings(), field) == expected


def test_scopes_are_split_on_commas(env):
    env.setenv("SCOPES", "a,b")

    assert Settings().scopes == ["a", "b"]


@pytest.mark.parametrize("variable", ["VERBOSE", "TELEGRAM_ANNOUNCE"])
@pytest.mark.parametrize(
    ("value", "expected"),
    [("True", True), ("False", False), ("0", False)],
)
def test_bool_variables(env, variable, value, expected):
    env.setenv(variable, value)

    assert getattr(Settings(), variable.lower()) is expected


def test_empty_home_gives_the_default(env):
    env.setenv("HOME", "")

    assert Settings().home == HOME_DEFAULT == Path("/app/config")


def test_derived_paths(env):
    env.setenv("HOME", "/data")
    env.setenv("SECRETS_FILENAME", "my_secrets.json")
    env.setenv("DB_FILENAME", "my.sqlite")
    env.setenv("CONFIG_FILENAME", "my.yaml")

    settings = Settings()

    assert settings.secrets_file == Path("/data/my_secrets.json")
    assert settings.db_filepath == Path("/data/my.sqlite")
    assert settings.config_filepath == Path("/data/my.yaml")
    assert settings.cert_ca_filepath == Path("/data/youtube_automanager_ca.pem")
    assert settings.cert_server_filepath == Path("/data/youtube_automanager_server.pem")


def test_derived_paths_with_default_file_names(env):
    settings = Settings()

    assert settings.secrets_file == Path("/app/config/client_secret.json")
    assert settings.db_filepath == Path("/app/config/youtube_automanager.sqlite")
    assert settings.config_filepath == Path("/app/config/youtube_automanager.yaml")


def test_get_settings_reads_the_environment_once_until_the_cache_is_cleared(env):
    env.setenv("USERNAME", "first")
    first = get_settings()
    env.setenv("USERNAME", "second")

    assert get_settings() is first
    assert get_settings().username == "first"

    get_settings.cache_clear()

    assert get_settings().username == "second"


def test_constants_that_are_not_settings():
    assert YOUTUBE_READ_WRITE_SCOPE == "https://www.googleapis.com/auth/youtube"
    assert YOUTUBE_API_SERVICE_NAME == "youtube"
    assert YOUTUBE_API_VERSION == "v3"
    assert TOKEN_URL == "https://accounts.google.com/o/oauth2/token"
