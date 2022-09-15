#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from functools import cache
from typing import List

from global_logger import Log
# noinspection PyPackageRequirements
from googleapiclient.discovery import build
from oauth2client.client import AccessTokenCredentials
from pyyoutube import Api, Playlist

from youtube_automanager import constants

LOG = Log.get_logger()


class YoutubeAPI:
    def __init__(self, api: Api, access_token: str):
        self.api = api
        self.access_token = access_token

    @property
    def google_api(self):
        creds = AccessTokenCredentials(self.access_token, '')
        return build(constants.YOUTUBE_API_SERVICE_NAME, constants.YOUTUBE_API_VERSION, credentials=creds)

    @cache
    def video_in_playlist(self, playlist_id, video_id):
        playlist_videos = self.get_playlist_items(playlist_id=playlist_id)
        output = [i for i in playlist_videos if i.id == video_id]
        return len(output) > 0

    def add_video_to_playlist(self, video_id, playlist_id):
        add_video_request = self.google_api.playlistItems().insert(
            part="snippet",
            body={
                'snippet': {
                    'playlistId': playlist_id,
                    'resourceId': {
                        'kind': 'youtube#video',
                        'videoId': video_id
                    }
                    # 'position': 0
                }
            }
        ).execute()
        return add_video_request

    @cache
    def get_playlist_items(self, playlist_id):
        kwargs = dict(playlist_id=playlist_id, limit=50, count=None)
        response = self.api.get_playlist_items(**kwargs)
        output = response.items
        total_results = response.pageInfo.totalResults
        LOG.debug(f"Got {len(output)}/{total_results} playlist items for {playlist_id}")
        while len(output) < total_results:
            page_token = response.nextPageToken
            response = self.api.get_playlist_items(page_token=page_token, **kwargs)
            output_ = response.items
            LOG.debug(f"Got {len(output_)} more playlist items for {playlist_id}")
            output.extend(output_)
        return output

    @cache
    def get_subscriptions(self, mine=True, count=None, limit=50, order='unread', page_token=None, parts=None,
                          **kwargs):
        # https://developers.google.com/youtube/v3/docs/subscriptions/list
        LOG.green("Getting subscriptions")
        parts = parts or ['snippet']
        subs = self.api.get_subscription_by_me(mine=mine, count=count, limit=limit, order=order, page_token=page_token,
                                               parts=parts, **kwargs)
        output = subs.items
        total_results = subs.pageInfo.totalResults
        LOG.debug(f"Got {len(output)}/{total_results} subscriptions")
        while len(output) < total_results:
            page_token = subs.nextPageToken
            LOG.debug(f"Getting next page of subscriptions")
            subs = self.api.get_subscription_by_me(mine=mine, count=count, limit=limit, order=order, parts=parts,
                                                   page_token=page_token)
            subs_ = subs.items
            LOG.debug(f"Got {len(subs_)} more subscriptions")
            output.extend(subs_)
        return output

    @cache
    def get_playlists(self, **kwargs) -> List[Playlist]:
        kwargs.setdefault('mine', True)
        kwargs.setdefault('count', None)
        kwargs.setdefault('parts', ['snippet'])
        LOG.green("Getting playlists")
        response = self.api.get_playlists(**kwargs)
        output = response.items
        return output

    def get_playlist_by_id(self, playlist_id):
        playlists = self.get_playlists()
        playlist = (i for i in playlists if i.id == playlist_id)
        return next(playlist, None)

    @cache
    def get_channel_activities(self, channel_id, **kwargs):
        kwargs.setdefault('parts', ['id', 'snippet', 'contentDetails'])
        return self.api.get_activities_by_channel(channel_id=channel_id, **kwargs)
