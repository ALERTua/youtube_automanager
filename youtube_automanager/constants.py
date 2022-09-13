#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
from pathlib import Path

from global_logger import Log

project_name = Path(__file__).parent.name
USERNAME = os.environ.get("USERNAME", 'user')
HOME = Path(os.environ.get("HOME", '/app/config'))
SECRETS_FILE = HOME / os.getenv('SECRETS_FILENAME', 'client_secret.json')
log_kwargs = dict()
if os.getenv('LOGFILES') == 'True':
    LOGS_FOLDER = HOME / 'logs'
    LOGS_FOLDER.mkdir(exist_ok=True)
    log_kwargs = dict(logs_dir=LOGS_FOLDER, max_log_files=10)
log = Log.get_logger(**log_kwargs)
if os.getenv('VERBOSE') == 'True':
    log.verbose = True

FILENAME_BASE = f'{project_name}'
TOKEN_FILENAME = f'{FILENAME_BASE}.json'
DB_FILENAME = os.getenv('DB_FILENAME', f'{FILENAME_BASE}.sqlite')
CONFIG_FILENAME = os.getenv('CONFIG_FILENAME', f'{FILENAME_BASE}.yaml')
DB_FILEPATH = HOME / DB_FILENAME
TOKEN_FILEPATH = HOME / TOKEN_FILENAME
CONFIG_FILEPATH = HOME / CONFIG_FILENAME
CERT_CA_FILENAME = f'{FILENAME_BASE}_ca.pem'
CERT_CA_FILEPATH = HOME / CERT_CA_FILENAME
CERT_SERVER_FILENAME = f'{FILENAME_BASE}_server.pem'
CERT_SERVER_FILEPATH = HOME / CERT_SERVER_FILENAME

YOUTUBE_READ_WRITE_SCOPE = "https://www.googleapis.com/auth/youtube"
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"
TOKEN_URL = "https://accounts.google.com/o/oauth2/token"
PORT = os.getenv('PORT', 8080)
HOST = os.getenv('HOST', 'localhost')
SCOPES = os.getenv('SCOPES', YOUTUBE_READ_WRITE_SCOPE)
SCOPES = SCOPES.split(',')
REDIRECT_URI = os.getenv('REDIRECT_URI')

