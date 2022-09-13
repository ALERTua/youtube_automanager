#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import asyncio
import contextlib
import pprint
from datetime import datetime
import threading
from copy import copy
from pathlib import Path
from typing import Optional, List
from urllib.request import Request

import pendulum
from pendulum import UTC
from google_auth_oauthlib.flow import InstalledAppFlow
from time import sleep
from functools import cached_property, cache

import trustme
import uvicorn
from atexit import register as atexit_register
from fastapi import FastAPI
from fastapi import Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from global_logger import Log
# noinspection PyPackageRequirements
from worker import async_worker

from youtube_automanager import constants

LOG = Log.get_logger()
LOCAL = pendulum.local_timezone()


class Server(uvicorn.Server):
    def install_signal_handlers(self):
        pass

    @contextlib.contextmanager
    def run_in_thread_context(self):
        thread = threading.Thread(target=self.run)
        thread.start()
        try:
            while not self.started:
                sleep(1e-3)
            yield
        finally:
            self.should_exit = True
            thread.join()

    def run_in_thread(self):
        # noinspection PyAttributeOutsideInit
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def thread_exit(self):
        self.should_exit = True
        self.thread.join()


class OAuth:
    def __init__(self, client_secrets_file: str | Path, scopes: List[str], host: str, port: int, redirect_uri: str,
                 token_url: str):
        self.host = host
        self.port = port
        self.scopes = scopes
        self.client_secrets_file = client_secrets_file
        self.token_url = token_url
        self.redirect_uri = redirect_uri
        self.__flow: Optional[InstalledAppFlow] = None
        self._web_server: Optional[uvicorn.Server] = None
        # atexit_register(self.exit)

    @atexit_register
    def exit(self):
        if self._web_server is not None and self.web_server.started:
            self.web_server.force_exit = True
            self.web_server.should_exit = True
        self.web_server.thread_exit()
        self.session.close()

    def _token_updater(self, token):
        self.session.access_token = token

    def _flow(self) -> InstalledAppFlow:
        if self.__flow is None:
            output = InstalledAppFlow.from_client_secrets_file(
                client_secrets_file=self.client_secrets_file,
                scopes=self.scopes,
                auto_refresh_url=self.token_url,
                redirect_uri=self.redirect_uri,
            )
            extra = {
                'client_id': output.client_config.get('client_id'),
                'client_secret': output.client_config.get('client_secret'),
            }
            output.oauth2session.auto_refresh_kwargs = extra
            output.oauth2session.token_updater = self._token_updater
            self.__flow = output
        return self.__flow

    @cached_property
    def flow(self) -> InstalledAppFlow:
        return self._flow()

    @property
    def session(self):
        return self.flow.oauth2session

    @property
    def authorized(self):
        return self.session.authorized

    def generate_auth_url(self):
        auth_url, _ = self.flow.authorization_url(access_type="offline", prompt="select_account")
        return auth_url

    def _fastapi_server(self) -> Server:
        ca = trustme.CA()
        cert_ca_filepath = str(constants.CERT_CA_FILEPATH)
        cert_server_filepath = str(constants.CERT_SERVER_FILEPATH)
        cert = ca.issue_cert(self.host)
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
            self.fetch_token(self._auth_response)
            return {"success": "You can now close the tab"}

        LOG.debug(f"Starting webserver @ {self.host}:{self.port}")
        config = uvicorn.Config(app, host=self.host, port=int(self.port),
                                log_level="info" if not LOG.verbose else 'debug', ssl_certfile=cert_server_filepath)
        server = Server(config=config)
        return server

    @property
    def web_server(self) -> Server:
        if self._web_server is None or not self._web_server.started:
            if self._web_server:
                self._web_server.should_exit = True
                self._web_server.force_exit = True

            self._web_server = self._fastapi_server()
            self._web_server.run_in_thread()
        return self._web_server

    def fetch_token(self, response):
        self.session.token = output = self.flow.fetch_token(authorization_response=response)
        LOG.debug(f"Fetched token:\n{pprint.pformat(output)}")

    def authorize(self, refresh_token=None):
        if self.__flow is None:
            self._flow()
            if refresh_token:
                self.refresh_token_()

        if self.authorized:
            return

        auth_url = self.generate_auth_url()
        LOG.yellow(auth_url)
        _ = self.web_server
        while not self.authorized:
            sleep(1)
        self.web_server.thread_exit()
        LOG.green("Authorized")

    @property
    def client_config(self):
        return self.flow.client_config

    @property
    def client_id(self):
        return self.client_config.get('client_id')

    @property
    def client_secret(self):
        return self.client_config.get('client_secret')

    def refresh_token_(self):
        self.session.token = output = self.session.refresh_token(token_url=self.token_url)
        LOG.debug(f"Token refresh result:\n{pprint.pformat(output)}")
        return output

    @property
    def refresh_token(self):
        return self.session.token.get('refresh_token')

    @property
    def access_token(self):
        return self.session.token.get('access_token')

    def token_expires(self):
        if (expires_at := self.token_expires_at) is None:
            return True

        expires_at_ = pendulum.instance(expires_at, UTC)
        diff = expires_at_ - pendulum.now(UTC)
        output = diff < pendulum.Duration(minutes=15)
        return output

    @property
    def token_expires_at(self):
        if (expires_at := self.session.token.get('expires_at')) is None:
            return

        return datetime.fromtimestamp(expires_at, tz=UTC)

    @async_worker
    async def token_refreshing_daemon(self):
        while True:
            while not self.token_expires():
                if (expires_at := self.token_expires_at) is not None:
                    diff = pendulum.instance(expires_at, pendulum.UTC) - pendulum.now(pendulum.UTC)
                    LOG.green(f"Token expires in {diff.in_words()}")
                await asyncio.sleep(60)

            LOG.green("Refreshing token")
            refresh_token = copy(self.refresh_token)
            LOG.debug(f"Using refresh token {refresh_token}")
            self.session.refresh_token(token_url=self.token_url)
            new_refresh_token = copy(self.refresh_token)
            LOG.debug(f"New refresh token: {new_refresh_token}")
            LOG.green(f"Refresh Token updated: {refresh_token != new_refresh_token}")
            expires_local = pendulum.instance(self.token_expires_at, tz=UTC).astimezone(tz=LOCAL)
            LOG.green(f"New Token expiration date: {expires_local.to_datetime_string()}")
            LOG.debug("token_refreshing_daemon Sleeping 600 seconds")
            await asyncio.sleep(600)

    @cache
    def run_token_refreshing_daemon(self):
        asyncio.run(self.token_refreshing_daemon())

    def _expire_token(self):
        self.session.token['expires_at'] = datetime.now(tz=UTC).timestamp()
        self.session.token['expires_in'] = 0


if __name__ == '__main__':
    pass
