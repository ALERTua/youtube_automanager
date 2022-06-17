#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import datetime
import json
from functools import cached_property
from pathlib import Path

import pendulum
import yaml
from global_logger import Log

from . import constants

log = Log.get_logger()


class YoutubeAutoManagerConfig:
    def __init__(self, config_filepath=constants.CONFIG_FILEPATH):
        self.config_filepath = Path(config_filepath)
        self.__auth_file = None
        self.start_date = None  # type: pendulum.DateTime

    @property
    def ok(self):
        config = self.config
        if config is None:
            return False

        auth_file = config.get('auth_file')
        if not auth_file:
            log.error(f"No auth_file found in config @ {self.config_filepath}")
            return False

        if not self._auth_file:
            return False

        start_date = config.get('start_date', None)
        if start_date is not None:
            try:
                self.start_date = pendulum.instance(datetime.datetime.fromisoformat(start_date))
            except Exception as e:
                log.exception(f"Failed to parse start_date {start_date}. Please use ISO8601 format", exc_info=e)
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

    @property
    def _auth_file(self):
        if self.__auth_file is None:
            auth_file = self.config.get('auth_file')
            if not auth_file:
                log.error(f"No auth_file found in config @ {self.config_filepath}")
                return

            candidates = [Path(auth_file), Path(constants.PROJECT_FOLDERPATH / auth_file)]
            for candidate in candidates:
                if candidate.exists():
                    log.debug(f"Found auth_file @ {candidate}")
                    self.__auth_file = candidate
                    return self.__auth_file

            candidates_str = '\n'.join([str(c) for c in candidates])
            log.error(f"Auth file not found among {candidates_str}")
        return self.__auth_file

    @property
    def _auth_data(self):
        path = self._auth_file
        if not path or not path.exists():
            return {}

        with path.open(mode='r', encoding='utf-8') as f:
            return json.loads(f.read())

    @property
    def client_id(self):
        return self._auth_data.get('web', {}).get('client_id')

    @property
    def client_secret(self):
        return self._auth_data.get('web', {}).get('client_secret')

    def _config(self):
        path = self.config_filepath
        if not path or not path.exists():
            log.error(f"Config file {path} not found")
            return

        with path.open(mode='r', encoding='utf-8') as f:
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
