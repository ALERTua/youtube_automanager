from pathlib import Path

import pytest
from pydantic import ValidationError

from youtube_automanager.settings import HOME_DEFAULT, Settings, get_settings

VARIABLES = [
    "HOME",
    "DB_FILENAME",
    "VERBOSE",
    "HOST",
    "PORT",
    "DATABASE_URL",
    "YTDLP_DIR",
    "TZ",
    "CHILD_TIMEOUT_S",
    "COOKIES_KEY",
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

    assert settings.home == Path("/app/config")
    assert settings.db_filename == "youtube_automanager.sqlite"
    assert settings.verbose is False
    assert settings.host == "0.0.0.0"
    assert settings.port == 8080
    assert settings.database_url == f"sqlite:///{Path('/app/config/youtube_automanager.sqlite')}"
    assert settings.ytdlp_dir == Path("/app/config/yt-dlp")
    assert settings.tz == "UTC"
    assert settings.child_timeout_s == 3600
    assert settings.cookies_key is None


@pytest.mark.parametrize("value", ["", "fakekey1,fakekey2"])
def test_cookies_key_is_secret(env, value):
    env.setenv("COOKIES_KEY", value)

    settings = Settings()

    if value:
        assert settings.cookies_key.get_secret_value() == value
        assert value not in repr(settings)
    else:
        assert settings.cookies_key is None


@pytest.mark.parametrize(
    ("variable", "value", "field", "expected"),
    [
        ("HOME", "/data", "home", Path("/data")),
        ("DB_FILENAME", "my.sqlite", "db_filename", "my.sqlite"),
        ("VERBOSE", "True", "verbose", True),
        ("HOST", "127.0.0.1", "host", "127.0.0.1"),
        ("PORT", "9000", "port", 9000),
        ("DATABASE_URL", "postgresql://user:secret@db/youtube", "database_url", "postgresql://user:secret@db/youtube"),
        ("YTDLP_DIR", "/opt/yt-dlp", "ytdlp_dir", Path("/opt/yt-dlp")),
        ("TZ", "Europe/Kyiv", "tz", "Europe/Kyiv"),
        ("CHILD_TIMEOUT_S", "60", "child_timeout_s", 60),
    ],
)
def test_variable_overrides_its_field(env, variable, value, field, expected):
    env.setenv(variable, value)

    assert getattr(Settings(), field) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("True", True), ("False", False), ("0", False)],
)
def test_bool_variables(env, value, expected):
    env.setenv("VERBOSE", value)

    assert Settings().verbose is expected


def test_empty_home_gives_the_default(env):
    env.setenv("HOME", "")

    assert Settings().home == HOME_DEFAULT == Path("/app/config")


def test_derived_path(env):
    env.setenv("HOME", "/data")
    env.setenv("DB_FILENAME", "my.sqlite")

    assert Settings().db_filepath == Path("/data/my.sqlite")


def test_derived_path_with_the_default_file_name(env):
    assert Settings().db_filepath == Path("/app/config/youtube_automanager.sqlite")


@pytest.mark.parametrize("variable", ["USERNAME", "CONFIG_FILENAME"])
def test_a_removed_variable_is_ignored(env, variable):
    env.setenv(variable, "anything")

    settings = Settings()

    assert variable.lower() not in Settings.model_fields
    assert not hasattr(settings, variable.lower())


def test_database_url_and_ytdlp_dir_follow_home_and_db_filename(env):
    env.setenv("HOME", "/data")
    env.setenv("DB_FILENAME", "my.sqlite")

    settings = Settings()

    assert settings.database_url == f"sqlite:///{settings.db_filepath}" == f"sqlite:///{Path('/data/my.sqlite')}"
    assert settings.ytdlp_dir == Path("/data/yt-dlp")


def test_database_url_and_ytdlp_dir_follow_home_given_as_an_argument(env):
    settings = Settings(home=Path("/other"))

    assert settings.database_url == f"sqlite:///{Path('/other/youtube_automanager.sqlite')}"
    assert settings.ytdlp_dir == Path("/other/yt-dlp")


@pytest.mark.parametrize("variable", ["DATABASE_URL", "YTDLP_DIR", "TZ", "COOKIES_KEY"])
def test_empty_variable_gives_the_default(env, variable):
    env.setenv(variable, "")

    settings = Settings()

    assert settings.database_url == f"sqlite:///{settings.db_filepath}"
    assert settings.ytdlp_dir == settings.home / "yt-dlp"
    assert settings.tz == "UTC"


@pytest.mark.parametrize("value", ["Nowhere/City", "../etc", ":/etc/localtime"])
def test_unknown_time_zone_is_refused(env, value):
    env.setenv("TZ", value)

    with pytest.raises(ValidationError, match="Unknown time zone"):
        Settings()


@pytest.mark.parametrize(("variable", "value"), [("CHILD_TIMEOUT_S", "0"), ("PORT", "70000"), ("PORT", "-1")])
def test_out_of_range_numbers_are_refused(env, variable, value):
    env.setenv(variable, value)

    with pytest.raises(ValidationError):
        Settings()


def test_get_settings_reads_the_environment_once_until_the_cache_is_cleared(env):
    env.setenv("HOST", "127.0.0.1")
    first = get_settings()
    env.setenv("HOST", "127.0.0.2")

    assert get_settings() is first
    assert get_settings().host == "127.0.0.1"

    get_settings.cache_clear()

    assert get_settings().host == "127.0.0.2"
