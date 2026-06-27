#!/usr/bin/env python3
from __future__ import annotations
from datetime import datetime
from functools import cached_property
from pathlib import Path

import pendulum
import yaml
from global_logger import Log

from youtube_automanager import constants

log = Log.get_logger()


class YoutubeAutoManagerConfig:
    def __init__(self, config_filepath=constants.CONFIG_FILEPATH):
        self.config_filepath = Path(config_filepath)
        self.__auth_file = None
        self.start_date: pendulum.DateTime | None = None

    @property
    def ok(self):
        config = self.config
        if config is None:
            return False

        start_date = config.get("start_date", None)
        if start_date is not None:
            try:
                self.start_date = pendulum.instance(datetime.fromisoformat(start_date))
            except Exception as e:
                log.exception(f"Failed to parse start_date {start_date}. Please use ISO8601 format", exc_info=e)
                return False

        rules = config.get("rules", [])
        if not rules:
            log.error(f"No rules found in config @ {self.config_filepath}")
            return False

        for rule in rules:
            if not any((rule.get("channel_id"), rule.get("channel_name"))):
                log.error(f"Rule has no channel_id or channel_name:\n{rule}")
                return False

            if not any((rule.get("playlist_id"), rule.get("playlist_name"))):
                log.error(f"Rule has no playlist_id or playlist_name:\n{rule}")
                return False

            if not self._duration_filters_valid(rule):
                return False

        return True

    @staticmethod
    def _duration_filters_valid(rule) -> bool:
        duration_min = rule.get("video_duration_min")
        duration_max = rule.get("video_duration_max")
        for key, value in (("video_duration_min", duration_min), ("video_duration_max", duration_max)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
                log.error(f"Rule has invalid {key}: {value!r}. Must be a non-negative number.\n{rule}")
                return False

        if duration_min is not None and duration_max is not None and duration_min > duration_max:
            log.error(
                f"Rule has video_duration_min ({duration_min}) greater than "
                f"video_duration_max ({duration_max}).\n{rule}",
            )
            return False

        return True

    def _config(self):
        path = self.config_filepath
        if not path or not path.exists():
            log.error(f"Config file {path} not found")
            return None

        with path.open(mode="r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    @cached_property
    def config(self):
        return self._config()

    def re_read_config(self):
        del self.__dict__["config"]
        _ = self.config


def main():
    log.verbose = True
    yamc = YoutubeAutoManagerConfig()
    _ = yamc._config()  # noqa: SLF001
    pass


if __name__ == "__main__":
    main()
    pass
