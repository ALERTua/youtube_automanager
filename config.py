#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from functools import cached_property
from pathlib import Path
import yaml
from global_logger import Log

import constants

log = Log.get_logger()


class YoutubeAutoManagerConfig:
    def __init__(self, config_filepath=constants.CONFIG_FILEPATH):
        self.config_filepath = Path(config_filepath)

    @property
    def ok(self):
        config = self.config
        if config is None:
            return False

        rules = config.get('rules', [])
        if not rules:
            log.error(f"No rules found in config @ {self.config_filepath}")
            return False

        for rule in rules:
            if not any((rule.get('channel_id'), rule.get('channel_name'))):
                log.error(f"Rule has no channel_id or channel_name:\n{rule}")
                return False

            if not any((rule.get('playlist_id'), rule.get('playlist_name'))):
                log.error(f"Rule has no playlist_id or playlist_name:\n{rule}")
                return False

        return True

    def _config(self):
        path = self.config_filepath
        if not path.exists():
            log.error(f"Config file {path} not found")
            return

        with path.open() as f:
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
    _ = yamc._config()
    print("")


if __name__ == '__main__':
    main()
    print("")
