import constants
import googleapiclient.discovery
api_service_name = "youtube"
api_version = "v3"
youtube = googleapiclient.discovery.build(api_service_name, api_version, developerKey=constants.API_KEY)
playlists = youtube.playlists().list(part="snippet", mine=True).execute()
request = youtube