#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import datetime
import os
import re
from functools import cached_property

import pendulum
from global_logger import Log
from pyyoutube import Activity

from . import constants
from .config import YoutubeAutoManagerConfig
from .db import DatabaseController, Video
from .youtube_api import YoutubeAPI

log = Log.get_logger()


class YoutubeAutoManager:
    def __init__(self, db_filepath=constants.DB_FILEPATH, config_filepath=constants.CONFIG_FILEPATH):
        self._db_filepath = db_filepath
        self._config_filepath = config_filepath

    @cached_property
    def dbc(self):
        return DatabaseController(db_filepath=self._db_filepath)

    @cached_property
    def config(self):
        return YoutubeAutoManagerConfig(config_filepath=self._config_filepath)

    @cached_property
    def youtube(self):
        return YoutubeAPI(client_id=self.config.client_id, client_secret=self.config.client_secret,
                          database_controller=self.dbc)

    def check_config(self):
        config = self.config
        if not config.ok:
            log.error(f"Config is not ok. "
                      f"Please create a properly formatted YAML config file @ {config.config_filepath}")
            return False

        return True

    def parse_activity(self, activity: Activity):
        video_id = activity.contentDetails.upload.videoId
        video_channel_id = activity.snippet.channelId
        video_channel_name = activity.snippet.channelTitle
        video_title = activity.snippet.title
        video_date = pendulum.instance(datetime.datetime.fromisoformat(activity.snippet.publishedAt))
        log.debug(f"Working on {video_channel_name} : {video_title}")
        db_video = Video.find(self.dbc.db, id=video_id)
        if db_video:
            log.debug(f"Video {video_id} '{video_title}' already parsed")
            return

        start_date = self.config.start_date
        if start_date and video_date < start_date:
            log.debug(f"Video {video_id} '{video_title}' is too old")
            return

        rules = self.config.config.get('rules', [])
        for rule in rules:
            rule_channel_id = rule.get('channel_id')
            if rule_channel_id and not isinstance(rule_channel_id, list):
                rule_channel_id = [rule_channel_id]

            if rule_channel_id and not any([i for i in rule_channel_id if i == video_channel_id]):
                log.debug(f"Video {video_id} '{video_title}' doesn't match any of the rule channel ids: "
                          f"{rule_channel_id}")
                continue

            rule_channel_name = rule.get('channel_name')
            if rule_channel_name and not isinstance(rule_channel_name, list):
                rule_channel_name = [rule_channel_name]
            if rule_channel_name and not any([i for i in rule_channel_name if re.match(i, video_channel_name)]):
                log.debug(f"Video {video_id} '{video_title}' doesn't match any of the rule channel names: "
                          f"{rule_channel_name}")
                continue

            rule_video_title_pattern = rule.get('video_title_pattern')
            if rule_video_title_pattern and not isinstance(rule_video_title_pattern, list):
                rule_video_title_pattern = [rule_video_title_pattern]
            if rule_video_title_pattern and not any([i for i in rule_video_title_pattern if
                                                     re.match(i, video_title, flags=re.I)
                                                     or re.search(i, video_title, flags=re.I)]):
                log.debug(f"Video {video_id} '{video_title}' title does not match any of the patterns "
                          f"{rule_video_title_pattern}")
                continue

            rule_playlist_id = rule.get('playlist_id')
            rule_playlist_name = rule.get('playlist_name')
            playlist_parts = ['snippet']
            if rule_playlist_id:
                playlist = self.youtube.api.get_playlist_by_id(playlist_id=rule_playlist_id, parts=playlist_parts)
            elif rule_playlist_name:
                playlist = self.youtube.api.get_playlists(mine=True, parts=playlist_parts, count=None)
                playlist = next((p for p in playlist.items if p.snippet.localized.title == rule_playlist_name), None)
            else:
                log.error(f"Rule has no playlist_id or playlist_name:\n{rule}")
                continue

            if not playlist:
                log.error(f"Failed to find playlist for rule:\n{rule}")
                continue

            playlist_title = playlist.snippet.localized.title
            playlist_id = playlist.id
            log.green(f"Video {video_id} '{video_title}' matches rule:\n{rule}\n"
                      f"Adding it to playlist {playlist_id} '{playlist_title}'")
            video = Video(id=video_id)
            if self.youtube.video_in_playlist(video_id=video_id, playlist_id=playlist_id):
                log.green(f"Video {video_id} '{video_title}' already in playlist {playlist_id} '{playlist_title}'")
                self.dbc.add(video)
                continue

            log.green(f"Adding video {video_id} '{video_title}' to playlist {playlist_id} '{playlist_title}'")
            self.youtube.add_video_to_playlist(video_id, playlist_id)
            self.dbc.add(video)

    def run(self):
        if not self.check_config():
            return

        if not self.youtube.authorized:
            self.youtube.authorize()

        if not self.youtube.authorized:
            log.error("Failed to authorize")
            return

        start_date = self.config.start_date
        if start_date:
            log.green(f"Using {start_date} as a Start Date")
            start_date = start_date.to_iso8601_string()

        subscriptions = self.youtube.get_subscriptions()
        log.green(f"Processing videos for {len(subscriptions)} subscriptions")
        for i, subscription in enumerate(subscriptions):
            channel_id = subscription.snippet.resourceId.channelId
            channel_name = subscription.snippet.title
            activities = self.youtube.api.get_activities_by_channel(
                channel_id=channel_id, after=start_date, parts=['id', 'snippet', 'contentDetails'])
            activities = [a for a in activities.items if a.snippet.type == 'upload']
            if not activities:
                log.debug(f"No videos found for channel {channel_id} '{channel_name}'")
                continue

            log.green(f"{i + 1}/{len(subscriptions)} Processing {len(activities)} videos for {channel_name}")
            for j, activity in enumerate(activities):
                # log.green(f"Processing video {j+1}/{len(activities)}")
                self.parse_activity(activity)

        self.dbc.commit()


def main():
    log.verbose = os.getenv('YAM_VERBOSE', None) is not None
    yam = YoutubeAutoManager()
    yam.run()


if __name__ == '__main__':
    main()
