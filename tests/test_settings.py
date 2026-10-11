from pathlib import Path

import pytest

from youtube_automanager.settings import HOME_DEFAULT, Settings, get_settings

VARIABLES = [
    "USERNAME",
    "HOME",
    "DB_FILENAME",
    "CONFIG_FILENAME",
    "VERBOSE",
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
    assert settings.db_filename == "youtube_automanager.sqlite"
    assert settings.config_filename == "youtube_automanager.yaml"
    assert settings.verbose is False


@pytest.mark.parametrize(
    ("variable", "value", "field", "expected"),
    [
        ("USERNAME", "alice", "username", "alice"),
        ("HOME", "/data", "home", Path("/data")),
        ("DB_FILENAME", "my.sqlite", "db_filename", "my.sqlite"),
        ("CONFIG_FILENAME", "my.yaml", "config_filename", "my.yaml"),
        ("VERBOSE", "True", "verbose", True),
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


def test_derived_paths(env):
    env.setenv("HOME", "/data")
    env.setenv("DB_FILENAME", "my.sqlite")
    env.setenv("CONFIG_FILENAME", "my.yaml")

    settings = Settings()

    assert settings.db_filepath == Path("/data/my.sqlite")
    assert settings.config_filepath == Path("/data/my.yaml")


def test_derived_paths_with_default_file_names(env):
    settings = Settings()

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
