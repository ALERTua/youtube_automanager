#!/usr/bin/env python3
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
import sys

LOG = Log.get_logger()


def token_expired(dt: datetime):
    return datetime.now(tz=pendulum.local_timezone()) > dt


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
            LOG.error(
                f"Config is not ok. Please create a properly formatted YAML config file @ {config.config_filepath}",
            )
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
            self.oauth.session.token["refresh_token"] = refresh_token
            LOG.debug(f"Got saved refresh token: {refresh_token}")
        if refresh_token and self.oauth.refresh_token_():
            LOG.green("Authorized using the saved refresh token.")
        else:
            self.oauth.authorize()
            self.save_token()

        # oauth.run_token_refreshing_daemon()  # TODO:  # noqa: ERA001
        LOG.green("Authorization complete")

    @property
    def start_date(self) -> datetime:
        if self._start_date is None:
            last_update = self.db.config.last_update
            cfg_date = self.config.start_date
            if cfg_date and last_update:
                start_date = cfg_date if cfg_date.timestamp() > last_update.timestamp() else last_update
            elif cfg_date:
                start_date = cfg_date
            elif last_update:
                start_date = last_update
            else:
                start_date = datetime.now(tz=pendulum.local_timezone())

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

    @staticmethod
    def _activity_video_date(activity: Activity) -> datetime:
        return pendulum.instance(datetime.fromisoformat(activity.snippet.publishedAt))

    @staticmethod
    def _rule_has_duration_filter(rule: dict) -> bool:
        return rule.get("video_duration_min") is not None or rule.get("video_duration_max") is not None

    @staticmethod
    def _rule_can_match_channel(rule: dict, channel_id: str, channel_name: str) -> bool:
        """Whether a rule could match any video from this channel (title aside)."""
        if rule.get("video_title_pattern"):
            return True  # title-pattern rules can match a video from any channel
        cids = rule.get("channel_id") or []
        if channel_id in (cids if isinstance(cids, list) else [cids]):
            return True
        cnames = rule.get("channel_name") or []
        return any(re.match(name, channel_name) for name in (cnames if isinstance(cnames, list) else [cnames]))

    @staticmethod
    def _rule_matches_metadata(  # noqa: C901, PLR0913
        rule: dict,
        video_id: str,
        video_channel_id: str,
        video_channel_name: str,
        video_title: str,
        *,
        log: bool = True,
    ) -> bool:
        """Whether a video matches a rule by channel id/name or title pattern (duration aside)."""
        # The criteria are combined with OR semantics, mirroring the original matching logic.
        # Pass log=False to evaluate silently (used when pre-selecting videos for a duration
        # lookup, to avoid duplicating the per-criterion debug logs emitted during processing).
        match = False

        rule_channel_id = rule.get("channel_id")
        if rule_channel_id and not isinstance(rule_channel_id, list):
            rule_channel_id = [rule_channel_id]
        if rule_channel_id and any(_ for _ in rule_channel_id if _ == video_channel_id):
            if log:
                LOG.debug(f"Video {video_id} '{video_title}' channel id matches rule: {video_channel_id}")
            match = True
        elif log:
            LOG.debug(
                f"Video {video_id} '{video_title}' doesn't match any of the rule channel ids: {rule_channel_id}",
            )

        rule_channel_name = rule.get("channel_name")
        if rule_channel_name and not isinstance(rule_channel_name, list):
            rule_channel_name = [rule_channel_name]
        if rule_channel_name and any(_ for _ in rule_channel_name if re.match(_, video_channel_name)):
            if log:
                LOG.debug(f"Video {video_id} '{video_title}' matches rule channel name: {video_channel_name}")
            match = True
        elif log:
            LOG.debug(
                f"Video {video_id} '{video_title}' doesn't match any of the rule channel names: {rule_channel_name}",
            )

        rule_video_title_pattern = rule.get("video_title_pattern")
        if rule_video_title_pattern and not isinstance(rule_video_title_pattern, list):
            rule_video_title_pattern = [rule_video_title_pattern]
        if rule_video_title_pattern and any(
            _
            for _ in rule_video_title_pattern
            if re.match(_, video_title, flags=re.IGNORECASE) or re.search(_, video_title, flags=re.IGNORECASE)
        ):
            if log:
                LOG.debug(f"Video {video_id} '{video_title}' matches rule pattern {rule_video_title_pattern}")
            match = True
        elif log:
            LOG.debug(
                f"Video {video_id} '{video_title}' title does not match any of the patterns {rule_video_title_pattern}",
            )

        return match

    def parse_activity(  # noqa: C901, PLR0912
        self,
        activity: Activity,
        start_date: datetime,
        durations: dict[str, int] | None = None,
    ):
        video_id = activity.contentDetails.upload.videoId
        video_channel_id = activity.snippet.channelId
        video_channel_name = activity.snippet.channelTitle
        video_title = activity.snippet.title
        video_date = self._activity_video_date(activity)
        LOG.debug(f"Working on {video_channel_name} : {video_title}")

        if video_date < pendulum.instance(start_date):
            LOG.debug(f"Video {video_id} '{video_title}' is too old")
            return

        rules = self.config.config.get("rules", [])
        for rule in rules:
            match = self._rule_matches_metadata(
                rule,
                video_id,
                video_channel_id,
                video_channel_name,
                video_title,
            )

            if match and self._rule_has_duration_filter(rule):
                duration = (durations or {}).get(video_id)
                duration_min = rule.get("video_duration_min")
                duration_max = rule.get("video_duration_max")
                if duration is None:
                    LOG.warning(
                        f"Video {video_id} '{video_title}' duration is unknown; skipping duration filter for this rule",
                    )
                elif duration_min is not None and duration < duration_min:
                    LOG.debug(
                        f"Video {video_id} '{video_title}' duration {duration}s is below minimum {duration_min}s",
                    )
                    match = False
                elif duration_max is not None and duration > duration_max:
                    LOG.debug(
                        f"Video {video_id} '{video_title}' duration {duration}s exceeds maximum {duration_max}s",
                    )
                    match = False
                else:
                    LOG.debug(f"Video {video_id} '{video_title}' duration {duration}s matches rule")

            if not match:
                continue

            rule_playlist_id = rule.get("playlist_id")
            rule_playlist_name = rule.get("playlist_name")
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
            LOG.green(
                f"Video {video_id} '{video_title}' matches rule:\n{rule}\n"
                f"Adding it to playlist {playlist_id} '{playlist_title}'",
            )
            if self.yt_api.video_in_playlist(video_id=video_id, playlist_id=playlist_id):
                LOG.green(f"Video {video_id} '{video_title}' already in playlist {playlist_id} '{playlist_title}'")
                continue

            LOG.green(f"Adding video {video_id} '{video_title}' to playlist {playlist_id} '{playlist_title}'")
            self.yt_api.add_video_to_playlist(video_id, playlist_id)

    def parse(self):
        LOG.green("Parsing")
        start_date = self.start_date
        start_dt = pendulum.instance(start_date)
        start_date_str = start_dt.to_iso8601_string()
        rules = self.config.config.get("rules", [])
        subscriptions = self.yt_api.get_subscriptions()
        total_subs = len(subscriptions)
        LOG.green(f"Got {total_subs} subscriptions")
        after_date = datetime.now(tz=pendulum.local_timezone())
        after_date_str = pendulum.instance(after_date).to_iso8601_string()

        LOG.green(f"Processing videos from {total_subs} subscriptions")
        for i, subscription in enumerate(subscriptions, start=1):
            LOG.debug(f"Parsing subscription {i}")
            channel_id = subscription.snippet.resourceId.channelId
            channel_name = subscription.snippet.title
            # Skip the activities request for channels no rule could ever match.
            if not any(self._rule_can_match_channel(rule, channel_id, channel_name) for rule in rules):
                LOG.debug(f"{i}/{total_subs} No rule targets channel {channel_id} '{channel_name}'")
                continue
            activities = self.yt_api.get_channel_activities(
                channel_id=channel_id,
                after=start_date_str,
                before=after_date_str,
            )
            activities = [a for a in activities.items if a.snippet.type == "upload"]
            if not activities:
                LOG.debug(f"{i}/{total_subs} No videos found for channel {channel_id} '{channel_name}'")
                continue

            # Pre-fetch durations in a single batched call, only for in-window videos that
            # actually match a duration-bearing rule by channel/title. This avoids fetching
            # durations for videos that no duration filter would ever apply to.
            durations: dict[str, int] = {}
            if any(self._rule_has_duration_filter(rule) for rule in rules):
                needed_ids = [
                    a.contentDetails.upload.videoId
                    for a in activities
                    if self._activity_video_date(a) >= start_dt
                    and any(
                        self._rule_has_duration_filter(rule)
                        and self._rule_matches_metadata(
                            rule,
                            a.contentDetails.upload.videoId,
                            a.snippet.channelId,
                            a.snippet.channelTitle,
                            a.snippet.title,
                            log=False,
                        )
                        for rule in rules
                    )
                ]
                if needed_ids:
                    durations = self.yt_api.get_videos_durations(needed_ids)

            LOG.green(f"{i}/{total_subs} Processing {len(activities)} videos for {channel_name}")
            for _j, activity in enumerate(activities, start=1):
                LOG.debug(f"Processing video {_j}/{len(activities)}")
                self.parse_activity(activity=activity, start_date=start_date, durations=durations)

        LOG.debug(f"Done parsing {total_subs} subscriptions")
        self.start_date = after_date

    def start(self):
        self.authorize()
        try:
            self.parse()
        except Exception as e:
            LOG.exception("an error occured", exc_info=e)

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


if __name__ == "__main__":
    config_ = YoutubeAutoManagerConfig(config_filepath=constants.CONFIG_FILEPATH)
    if not config_.ok:
        sys.exit(1)

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
    if (
        constants.TELEGRAM_ANNOUNCE == "True"
        and (tg_token := constants.TELEGRAM_BOT_TOKEN)
        and (tg_chat := constants.TELEGRAM_CHAT_ID)
    ):
        # noinspection PyUnboundLocalVariable
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
