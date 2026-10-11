#!/usr/bin/env python3
import functools
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_NAME = "youtube_automanager"
HOME_DEFAULT = Path("/app/config")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    username: str = "user"
    home: Path = HOME_DEFAULT
    db_filename: str = f"{PROJECT_NAME}.sqlite"
    config_filename: str = f"{PROJECT_NAME}.yaml"
    verbose: bool = False

    @field_validator("home", mode="before")
    @classmethod
    def _empty_home_is_default(cls, value):
        return value or HOME_DEFAULT

    @property
    def db_filepath(self) -> Path:
        return self.home / self.db_filename

    @property
    def config_filepath(self) -> Path:
        return self.home / self.config_filename


@functools.cache
def get_settings() -> Settings:
    return Settings()
