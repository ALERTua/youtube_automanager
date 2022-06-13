#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import webbrowser
from pathlib import Path

from global_logger import Log
from pyyoutube import Api, AccessToken

import constants

log = Log.get_logger()


class YoutubeAPI:
    def __init__(self, access_token_filepath=constants.TOKEN_FILEPATH, client_id=constants.CLIENT_ID,
                 client_secret=constants.CLIENT_SECRET):
        self._access_token_filepath = Path(access_token_filepath)
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token = None
        self._api = None

    @property
    def access_token(self):
        if not self._access_token:
            self._access_token = self._get_access_token()
        return self._access_token

    @access_token.setter
    def access_token(self, value):
        if isinstance(value, AccessToken):
            value = value.access_token
        if self._access_token == value:
            return

        log.debug(f"Saving Access Token {value}")
        self._access_token = value
        self._access_token_filepath.write_text(value)

    def _request_auth(self, api=None):
        api = api or self.api
        log.debug("Requesting auth")
        auth_url, project = api.get_authorization_url()
        webbrowser.open_new_tab(auth_url)
        auth_response = input("Full URL:").strip()
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

        output = self._authorized()
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
        if self._api and self._authorized(self._api):
            return self._api

        access_token = self.access_token
        api = Api(client_id=self._client_id, client_secret=self._client_secret, access_token=access_token)
        if not access_token or not self._authorized(api):
            log.debug("No Access Token")
            access_token = self._generate_access_token(api)
        elif access_token and not self._authorized(api):
            log.debug("Access Token is invalid")
            access_token = self._generate_access_token(api)
        if self._authorized(api):
            log.green("Authorized.")
            self.access_token = access_token
            self._api = api
        return api

    @property
    def playlists(self, mine=True, count=None, **kwargs):
        log.green("Getting playlists")
        return self.api.get_playlists(mine=mine, count=count, **kwargs)

    @property
    def subscriptions(self):
        log.green("Getting subscriptions")
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

    def _get_access_token(self, token_filepath=None):
        token_filepath = token_filepath or self._access_token_filepath
        if token_filepath.exists():
            log.debug(f"Getting access token from {token_filepath}")
            return token_filepath.read_text()


def main():
    log.verbose = True
    youtube = YoutubeAPI()
    print("")


if __name__ == '__main__':
    main()
    print("")
