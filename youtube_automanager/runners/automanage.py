#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import re
from datetime import datetime

import pendulum
from global_logger import Log
from pyyoutube import Api, Activity
from knockknock import telegram_sender, discord_sender, slack_sender, teams_sender

from youtube_automanager import constants
from youtube_automanager.config import YoutubeAutoManagerConfig
from youtube_automanager.db import DatabaseController
from youtube_automanager.oauth import OAuth
from youtube_automanager.youtube_api import YoutubeAPI

LOG = Log.get_logger()


def token_expired(dt: datetime):
    return datetime.now() > dt


class YoutubeAutoManager:
    def __init__(self, oauth: OAuth, db: DatabaseController, config: YoutubeAutoManagerConfig):
        self.oauth: OAuth = oauth
        self.db: DatabaseController = db
        self.config: YoutubeAutoManagerConfig = config
        self._yt_api = None
        self._start_date = None

    def check_config(self):
        config = self.config
        if not config.ok:
            LOG.error(f"Config is not ok. "
                      f"Please create a properly formatted YAML config file @ {config.config_filepath}")
            return False

        return True

    def save_token(self):
        saved_refresh_token = self.db.config.refresh_token
        new_refresh_token = self.oauth.refresh_token
        if saved_refresh_token == new_refresh_token:
            return

        LOG.debug(f"Saving new refresh token {new_refresh_token}")
        self.db.config.refresh_token = new_refresh_token
        self.db.save_config()
        self.db.commit()

    def authorize(self):
        refresh_token = self.db.config.refresh_token
        if refresh_token:
            self.oauth.session.token['refresh_token'] = refresh_token
            LOG.debug(f"Got saved refresh token: {refresh_token}")
        if refresh_token and (success := self.oauth.refresh_token_()):
            LOG.green(f"Authorized using the saved refresh token.")
        else:
            self.oauth.authorize()
            self.save_token()

        # oauth.run_token_refreshing_daemon()  # todo:
        LOG.green("Authorization complete")

    @property
    def start_date(self) -> datetime:
        if self._start_date is None:
            last_update = self.db.config.last_update
            cfg_date = self.config.start_date
            if cfg_date and last_update:
                if cfg_date.timestamp() > last_update.timestamp():
                    start_date = cfg_date
                else:
                    start_date = last_update
            elif cfg_date:
                start_date = cfg_date
            elif last_update:
                start_date = last_update
            else:
                start_date = datetime.now()

            LOG.green(f"Using {pendulum.instance(start_date).to_datetime_string()} as a Start Date")
            self._start_date = start_date
        return self._start_date

    @start_date.setter
    def start_date(self, value: datetime):
        LOG.green(f"Saving new start date {pendulum.instance(value).to_datetime_string()}")
        self._start_date = value
        self.db.config.last_update = value
        self.db.save_config()
        self.db.commit()

    def parse_activity(self, activity: Activity, start_date: datetime):
        video_id = activity.contentDetails.upload.videoId
        video_channel_id = activity.snippet.channelId
        video_channel_name = activity.snippet.channelTitle
        video_title = activity.snippet.title
        video_date = pendulum.instance(datetime.fromisoformat(activity.snippet.publishedAt))
        LOG.debug(f"Working on {video_channel_name} : {video_title}")

        if video_date < pendulum.instance(start_date):
            LOG.debug(f"Video {video_id} '{video_title}' is too old")
            return

        rules = self.config.config.get('rules', [])
        for rule in rules:  # todo: video duration filter
            rule_channel_id = rule.get('channel_id')
            if rule_channel_id and not isinstance(rule_channel_id, list):
                rule_channel_id = [rule_channel_id]

            if rule_channel_id and not any([i for i in rule_channel_id if i == video_channel_id]):
                LOG.debug(f"Video {video_id} '{video_title}' doesn't match any of the rule channel ids: "
                          f"{rule_channel_id}")
                continue

            rule_channel_name = rule.get('channel_name')
            if rule_channel_name and not isinstance(rule_channel_name, list):
                rule_channel_name = [rule_channel_name]
            if rule_channel_name and not any([i for i in rule_channel_name if re.match(i, video_channel_name)]):
                LOG.debug(f"Video {video_id} '{video_title}' doesn't match any of the rule channel names: "
                          f"{rule_channel_name}")
                continue

            rule_video_title_pattern = rule.get('video_title_pattern')
            if rule_video_title_pattern and not isinstance(rule_video_title_pattern, list):
                rule_video_title_pattern = [rule_video_title_pattern]
            if rule_video_title_pattern and not any([i for i in rule_video_title_pattern if
                                                     re.match(i, video_title, flags=re.I)
                                                     or re.search(i, video_title, flags=re.I)]):
                LOG.debug(f"Video {video_id} '{video_title}' title does not match any of the patterns "
                          f"{rule_video_title_pattern}")
                continue

            rule_playlist_id = rule.get('playlist_id')
            rule_playlist_name = rule.get('playlist_name')
            if rule_playlist_id:
                playlist = self.yt_api.get_playlist_by_id(playlist_id=rule_playlist_id)
            elif rule_playlist_name:
                playlists = self.yt_api.get_playlists()
                playlist = next((p for p in playlists if p.snippet.localized.title == rule_playlist_name), None)
            else:
                LOG.error(f"Rule has no playlist_id or playlist_name:\n{rule}")
                continue

            if not playlist:
                LOG.error(f"Failed to find playlist for rule:\n{rule}")
                continue

            playlist_title = playlist.snippet.localized.title
            playlist_id = playlist.id
            LOG.green(f"Video {video_id} '{video_title}' matches rule:\n{rule}\n"
                      f"Adding it to playlist {playlist_id} '{playlist_title}'")
            if self.yt_api.video_in_playlist(video_id=video_id, playlist_id=playlist_id):
                LOG.green(f"Video {video_id} '{video_title}' already in playlist {playlist_id} '{playlist_title}'")
                continue

            LOG.green(f"Adding video {video_id} '{video_title}' to playlist {playlist_id} '{playlist_title}'")
            self.yt_api.add_video_to_playlist(video_id, playlist_id)

    def parse(self):
        LOG.green("Parsing")
        start_date = self.start_date
        start_date_str = pendulum.instance(start_date).to_iso8601_string()
        subscriptions = self.yt_api.get_subscriptions()
        total_subs = len(subscriptions)
        LOG.green(f"Got {total_subs} subscriptions")
        after_date = datetime.now()
        after_date_str = pendulum.instance(after_date).to_iso8601_string()

        LOG.green(f"Processing videos from {total_subs} subscriptions")
        for i, subscription in enumerate(subscriptions):
            LOG.debug(f"Parsing subscription {i + 1}")
            channel_id = subscription.snippet.resourceId.channelId
            channel_name = subscription.snippet.title
            activities = self.yt_api.get_channel_activities(channel_id=channel_id, after=start_date_str,
                                                            before=after_date_str)
            activities = [a for a in activities.items if a.snippet.type == 'upload']
            if not activities:
                LOG.debug(f"{i + 1}/{total_subs} No videos found for channel {channel_id} '{channel_name}'")
                continue

            LOG.green(f"{i + 1}/{total_subs} Processing {len(activities)} videos for {channel_name}")
            for j, activity in enumerate(activities):
                # log.green(f"Processing video {j+1}/{len(activities)}")
                self.parse_activity(activity=activity, start_date=start_date)

        LOG.debug(f"Done parsing {total_subs} subscriptions")
        self.start_date = after_date

    def start(self):
        self.authorize()
        try:
            self.parse()
        except Exception as e:
            LOG.exception("an error occured", exc_info=e)

        # LOG.debug("Parsing Done. Preparing to sleep")
        # sleep = 60 * 60
        # LOG.green(f"Sleeping {sleep} seconds")
        # time.sleep(sleep)
        # self.config.re_read_config()
        # self.check_config()

    @property
    def api(self):
        return Api(
            client_id=self.oauth.client_id,
            client_secret=self.oauth.client_secret,
            access_token=self.oauth.access_token,
        )

    @property
    def yt_api(self):
        if self._yt_api is None:
            self._yt_api = YoutubeAPI(self.api, self.oauth.access_token)
        self._yt_api.access_token = self.oauth.access_token
        return self._yt_api


if __name__ == '__main__':
    config_ = YoutubeAutoManagerConfig(config_filepath=constants.CONFIG_FILEPATH)
    if not config_.ok:
        exit(1)

    oauth_ = OAuth(
        client_secrets_file=constants.SECRETS_FILE,
        scopes=constants.SCOPES,
        host=constants.HOST,
        port=constants.PORT,
        redirect_uri=constants.REDIRECT_URI,
        token_url=constants.TOKEN_URL,
    )
    db_ = DatabaseController(
        db_filepath=constants.DB_FILEPATH,
        username=constants.USERNAME,
    )

    manager = YoutubeAutoManager(oauth=oauth_, db=db_, config=config_)

    fnc = manager.start  # https://github.com/huggingface/knockknock
    if constants.TELEGRAM_ANNOUNCE == 'True':
        if (tg_token := constants.TELEGRAM_BOT_TOKEN) and (tg_chat := constants.TELEGRAM_CHAT_ID):
            fnc = telegram_sender(token=tg_token, chat_id=int(tg_chat))(fnc)

    if discord_webhook := constants.DISCORD_WEBHOOK_URL:
        fnc = discord_sender(discord_webhook)(fnc)

    if (slack_webhook := constants.SLACK_WEBHOOK_URL) and (slack_channel := constants.SLACK_CHANNEL):
        if slack_user_mentions := constants.SLACK_USER_MENTIONS:
            slack_user_mentions = slack_user_mentions.split()
        fnc = slack_sender(slack_webhook, slack_channel, slack_user_mentions)(fnc)

    if teams_webhook := constants.TEAMS_WEBHOOK_URL:
        if teams_user_mentions := constants.TEAMS_USER_MENTIONS:
            teams_user_mentions = teams_user_mentions.split()
        fnc = teams_sender(teams_webhook, teams_user_mentions)(fnc)

    fnc()
    pass
