import os
from pathlib import Path

project_name = Path(__file__).parent.name
USERNAME = os.environ.get("USERNAME")
HOME = Path(os.environ.get("YAM_HOME", Path.home()))
PROJECT_FOLDERPATH = HOME / project_name
if not PROJECT_FOLDERPATH.exists():
    PROJECT_FOLDERPATH.mkdir()

FILENAME_BASE = f'{project_name}'
TOKEN_FILENAME = f'{FILENAME_BASE}.json'
DB_FILENAME = f'{FILENAME_BASE}.sqlite'
CONFIG_FILENAME = f'{FILENAME_BASE}.yaml'
DB_FILEPATH = PROJECT_FOLDERPATH / DB_FILENAME
TOKEN_FILEPATH = PROJECT_FOLDERPATH / TOKEN_FILENAME
CONFIG_FILEPATH = PROJECT_FOLDERPATH / CONFIG_FILENAME
CERT_CA_FILENAME = f'{FILENAME_BASE}_ca.pem'
CERT_CA_FILEPATH = PROJECT_FOLDERPATH / CERT_CA_FILENAME
CERT_SERVER_FILENAME = f'{FILENAME_BASE}_server.pem'
CERT_SERVER_FILEPATH = PROJECT_FOLDERPATH / CERT_SERVER_FILENAME

YOUTUBE_READ_WRITE_SCOPE = "https://www.googleapis.com/auth/youtube"
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"