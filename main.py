#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import re
from global_logger import Log
from pyyoutube import Activity

import constants
from config import YoutubeAutoManagerConfig
from db import DatabaseController, Video
from youtube_api import YoutubeAPI

log = Log.get_logger()


class YoutubeAutoManager:
    def __init__(self, db_filepath=constants.DB_FILEPATH, access_foken_filepath=constants.TOKEN_FILEPATH,
                 config_filepath=constants.CONFIG_FILEPATH):
        self.youtube = YoutubeAPI(access_foken_filepath=access_foken_filepath)
        self.dbc = DatabaseController(db_filepath=db_filepath)
        self.config = YoutubeAutoManagerConfig(config_filepath=config_filepath)

    def check_config(self):
        if not self.config.ok:
            log.error(f"Config is not ok. Please create a YAML config file @ {self.config.config_filepath}")
            return False

        return True

    def parse_activity(self, activity: Activity):
        video_id = activity.contentDetails.upload.videoId
        db_video = Video.find(self.dbc.db, id=video_id)
        if db_video:
            log.debug(f"Video {video_id} already parsed")
            return

        video_channel_id = activity.snippet.channelId
        video_channel_name = activity.snippet.channelTitle
        video_title = activity.snippet.title

        rules = self.config.config.get('rules', [])
        for rule in rules:
            rule_playlist_id = rule.get('playlist_id')
            rule_playlist_name = rule.get('playlist_name')
            # playlist_parts = ['id', 'status', 'contentDetails', 'snippet', 'player']
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

            rule_channel_id = rule.get('channel_id')
            rule_channel_name = rule.get('channel_name')
            if rule_channel_id and rule_channel_id != video_channel_id:
                continue

            if rule_channel_name and not re.match(rule_channel_name, video_channel_name):
                continue

            rule_video_title_pattern = rule.get('video_title_pattern')
            if rule_video_title_pattern and not re.match(rule_video_title_pattern, video_title):
                log.debug(f"Video {video_id} title does not match pattern {rule_video_title_pattern}")
                continue

            pass

    def run(self):
        if not self.check_config():
            return

        if not self.youtube.authorized:
            self.youtube.authorize()

        if not self.youtube.authorized:
            log.error("Failed to authorize")
            return

        subscriptions = self.youtube.get_subscriptions(count=2)
        for subscription in subscriptions:
            channel_id = subscription.snippet.resourceId.channelId
            activities = self.youtube.api.get_activities_by_channel(channel_id=channel_id,
                                                                    parts=['id', 'snippet', 'contentDetails'])
            for activity in activities.items:
                if activity.snippet.type == 'upload':
                    self.parse_activity(activity)
                    pass
                else:
                    pass
            pass
        pass


def main():
    log.verbose = True
    yam = YoutubeAutoManager()
    yam.run()
    print("")


if __name__ == '__main__':
    main()
    print("")
