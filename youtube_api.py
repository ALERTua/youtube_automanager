#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import webbrowser
from functools import cached_property
from typing import TYPE_CHECKING

import trustme
import uvicorn
from fastapi import FastAPI
from fastapi import Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from global_logger import Log
# noinspection PyPackageRequirements
from googleapiclient.discovery import build
from oauth2client.client import AccessTokenCredentials
from pyyoutube import Api, AccessToken

import constants

if TYPE_CHECKING:
    from db import DatabaseController
log = Log.get_logger()


class YoutubeAPI:
    def __init__(self, client_id, client_secret, database_controller):
        self._client_id = client_id
        self._client_secret = client_secret
        self._dbc = database_controller  # type: DatabaseController
        self._access_token = None
        self._api = None
        self._auth_response = None
        self.__authorized = False

    @property
    def access_token(self):
        if self._access_token is None:
            self._access_token = self._dbc.config.token
            self.__authorized = False
        return self._access_token

    @access_token.setter
    def access_token(self, value):
        if isinstance(value, AccessToken):
            value = value.access_token
        if self._access_token == value:
            return

        log.debug(f"Saving Access Token {value}")
        self._access_token = value
        self.__authorized = True
        self._dbc.config.token = value
        self._dbc.save_config()

    def _fastapi_server(self):
        ca = trustme.CA()
        cert_ca_filepath = str(constants.CERT_CA_FILEPATH)
        cert_server_filepath = str(constants.CERT_SERVER_FILEPATH)
        cert = ca.issue_cert("localhost")
        ca.cert_pem.write_to_path(cert_ca_filepath)
        cert.private_key_and_cert_chain_pem.write_to_path(cert_server_filepath)
        app = FastAPI()

        def get_request(request: Request) -> Request:
            return request

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        app.add_middleware(HTTPSRedirectMiddleware)

        @app.get("/")
        def root(request=Depends(get_request)):
            self._auth_response = str(request.url)
            server.should_exit = True
            server.force_exit = True
            # server.shutdown()

        config = uvicorn.Config(app, host="0.0.0.0", port=443, log_level="info", ssl_certfile=cert_server_filepath)
        server = uvicorn.Server(config=config)
        return server

    def _request_auth(self, api=None):
        api = api or self.api
        log.debug("Requesting auth")
        auth_url, project = api.get_authorization_url()
        webbrowser.open_new_tab(auth_url)
        server = self._fastapi_server()
        server.run()

        auth_response = self._auth_response
        log.debug(f"auth_response: {auth_response}")
        return auth_response

    def _authorized(self, api=None):
        api = api or self.api
        # noinspection PyBroadException
        try:
            api.get_profile()
            output = True
        except Exception:
            output = False

        return output

    @property
    def authorized(self):
        if not self._api:
            return False

        if self.__authorized:
            return True

        self.__authorized = output = self._authorized()
        log.debug(f"Authorized: {output}")
        return output

    def authorize(self):
        if self.authorized:
            return

        log.green("Authorizing")
        _ = self.api

    def _generate_access_token(self, api=None):
        log.debug("Generating access token")
        api = api or self.api
        auth_response = self._request_auth(api)
        output = api.generate_access_token(authorization_response=auth_response)
        return output

    @property
    def api(self):
        if self.authorized:
            return self._api

        access_token = self.access_token
        api = Api(client_id=self._client_id, client_secret=self._client_secret, access_token=access_token)
        if not access_token or not self._authorized(api):
            log.debug("No Access Token")
            self._api = None
            access_token = self._generate_access_token(api)
        elif access_token and not self._authorized(api):
            log.debug("Access Token is invalid")
            self._api = None
            access_token = self._generate_access_token(api)
        if self._authorized(api):
            log.green("Authorized.")
            self.access_token = access_token
            self._api = api
            self.__authorized = True
        return self._api

    @api.setter
    def api(self, value):
        self._api = value
        self.__authorized = False

    @cached_property
    def google_api(self):
        creds = AccessTokenCredentials(self.access_token, '')
        return build(constants.YOUTUBE_API_SERVICE_NAME, constants.YOUTUBE_API_VERSION, credentials=creds)

    @cached_property
    def playlists(self):
        return self.get_playlists(mine=True, count=None)

    def get_playlists(self, mine=True, count=None, **kwargs):
        log.green("Getting playlists")
        return self.api.get_playlists(mine=mine, count=count, **kwargs)

    @cached_property
    def subscriptions(self):
        return self.get_subscriptions()

    def get_subscriptions(self, mine=True, count=None, limit=50, order='unread', page_token=None, parts=None,
                          **kwargs):
        # https://developers.google.com/youtube/v3/docs/subscriptions/list
        log.green("Getting subscriptions")
        parts = parts or ['snippet']
        subs = self.api.get_subscription_by_me(mine=mine, count=count, limit=limit, order=order, page_token=page_token,
                                               parts=parts, **kwargs)
        output = subs.items
        total_items = count or 0
        total_results = subs.pageInfo.totalResults
        if total_results < total_items:
            total_items = total_results
        got_items = len(subs.items)
        log.debug(f"Got {got_items}/{total_items}")
        while got_items < total_items:
            page_token = subs.nextPageToken
            log.debug(f"Getting next page of subscriptions")
            subs = self.api.get_subscription_by_me(mine=mine, count=count, limit=limit, order=order, parts=parts,
                                                   page_token=page_token)
            output.extend(subs.items)
        return output

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


def main():
    log.verbose = True
    youtube = YoutubeAPI()
    print("")


if __name__ == '__main__':
    main()
    print("")
