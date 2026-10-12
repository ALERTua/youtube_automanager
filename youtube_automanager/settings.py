#!/usr/bin/env python3
import functools
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_core import PydanticUseDefault
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_NAME = "youtube_automanager"
HOME_DEFAULT = Path("/app/config")
YTDLP_DIRNAME = "yt-dlp"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    username: str = "user"
    home: Path = HOME_DEFAULT
    db_filename: str = f"{PROJECT_NAME}.sqlite"
    config_filename: str = f"{PROJECT_NAME}.yaml"
    verbose: bool = False
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=0, le=65535)
    # The default is the SQLite file `db_filepath`. A default factory gets the fields above it, already validated.
    database_url: str = Field(default_factory=lambda data: f"sqlite:///{data['home'] / data['db_filename']}")
    ytdlp_dir: Path = Field(default_factory=lambda data: data["home"] / YTDLP_DIRNAME)
    # The field reads the variable TZ, which also sets the time zone of the C library. An unknown name stops the start.
    tz: str = "UTC"
    child_timeout_s: int = Field(default=3600, gt=0)

    @field_validator("home", mode="before")
    @classmethod
    def _empty_home_is_default(cls, value):
        return value or HOME_DEFAULT

    @field_validator("database_url", "ytdlp_dir", "tz", mode="before")
    @classmethod
    def _empty_value_is_default(cls, value):
        if value == "":
            raise PydanticUseDefault()
        return value

    @field_validator("tz")
    @classmethod
    def _tz_is_a_known_time_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"Unknown time zone {value!r}: {error}") from None
        return value

    @property
    def db_filepath(self) -> Path:
        return self.home / self.db_filename

    @property
    def config_filepath(self) -> Path:
        return self.home / self.config_filename


@functools.cache
def get_settings() -> Settings:
    return Settings()
