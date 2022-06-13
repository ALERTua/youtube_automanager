import os
from pathlib import Path

project_name = Path(__file__).parent.name
username = os.environ.get("USERNAME")
HOME = Path.home()
FILENAME_BASE = f'{project_name}_{username}'
TOKEN_FILENAME = f'{FILENAME_BASE}.json'
DB_FILENAME = f'{FILENAME_BASE}.db'
CONFIG_FILENAME = f'{FILENAME_BASE}.yaml'
DB_FILEPATH = HOME / DB_FILENAME
TOKEN_FILEPATH = HOME / TOKEN_FILENAME
CONFIG_FILEPATH = HOME / CONFIG_FILENAME
API_KEY = os.getenv("YOUTUBE_API_KEY")
CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID")
CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET")
