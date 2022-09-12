from functools import cached_property

# noinspection PyPackageRequirements
from googleapiclient.discovery import build
from oauth2client.client import AccessTokenCredentials
from pyyoutube import Api

from global_logger import Log

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

    def video_in_playlist(self, playlist_id, video_id):
        return len(self.api.get_playlist_items(playlist_id=playlist_id, video_id=video_id).items) > 0

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

    def get_subscriptions(self, mine=True, count=None, limit=50, order='unread', page_token=None, parts=None,
                          **kwargs):
        # https://developers.google.com/youtube/v3/docs/subscriptions/list
        LOG.green("Getting subscriptions")
        parts = parts or ['snippet']
        subs = self.api.get_subscription_by_me(mine=mine, count=count, limit=limit, order=order, page_token=page_token,
                                               parts=parts, **kwargs)
        output = subs.items
        total_items = count or 0
        total_results = subs.pageInfo.totalResults
        if total_results < total_items:
            total_items = total_results
        got_items = len(subs.items)
        LOG.debug(f"Got {got_items}/{total_items}")
        while got_items < total_items:
            page_token = subs.nextPageToken
            LOG.debug(f"Getting next page of subscriptions")
            subs = self.api.get_subscription_by_me(mine=mine, count=count, limit=limit, order=order, parts=parts,
                                                   page_token=page_token)
            output.extend(subs.items)
        return output

    @cached_property
    def playlists(self):
        return self.get_playlists(mine=True, count=None)

    def get_playlists(self, mine=True, count=None, **kwargs):
        LOG.green("Getting playlists")
        return self.api.get_playlists(mine=mine, count=count, **kwargs)

    @cached_property
    def subscriptions(self):
        return self.get_subscriptions()

