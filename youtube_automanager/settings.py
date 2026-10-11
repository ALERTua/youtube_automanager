#!/usr/bin/env python3
import functools
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_NAME = "youtube_automanager"
HOME_DEFAULT = Path("/app/config")

YOUTUBE_READ_WRITE_SCOPE = "https://www.googleapis.com/auth/youtube"
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"
TOKEN_URL = "https://accounts.google.com/o/oauth2/token"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    username: str = "user"
    home: Path = HOME_DEFAULT
    secrets_filename: str = "client_secret.json"
    db_filename: str = f"{PROJECT_NAME}.sqlite"
    config_filename: str = f"{PROJECT_NAME}.yaml"
    port: int = 8080
    host: str = "localhost"
    scopes: Annotated[list[str], NoDecode] = [YOUTUBE_READ_WRITE_SCOPE]
    redirect_uri: str | None = None
    verbose: bool = False

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_announce: bool = False
    discord_webhook_url: str = ""
    slack_webhook_url: str = ""
    slack_channel: str = ""
    slack_user_mentions: str = ""
    teams_webhook_url: str = ""
    teams_user_mentions: str = ""

    @field_validator("home", mode="before")
    @classmethod
    def _empty_home_is_default(cls, value):
        return value or HOME_DEFAULT

    @field_validator("scopes", mode="before")
    @classmethod
    def _split_scopes(cls, value):
        if isinstance(value, str):
            return value.split(",")
        return value

    @property
    def secrets_file(self) -> Path:
        return self.home / self.secrets_filename

    @property
    def db_filepath(self) -> Path:
        return self.home / self.db_filename

    @property
    def config_filepath(self) -> Path:
        return self.home / self.config_filename

    @property
    def cert_ca_filepath(self) -> Path:
        return self.home / f"{PROJECT_NAME}_ca.pem"

    @property
    def cert_server_filepath(self) -> Path:
        return self.home / f"{PROJECT_NAME}_server.pem"


@functools.cache
def get_settings() -> Settings:
    return Settings()
