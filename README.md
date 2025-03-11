[![Stand With Ukraine](https://raw.githubusercontent.com/vshymanskyy/StandWithUkraine/main/banner-direct-single.svg)](https://stand-with-ukraine.pp.ua)
[![Made in Ukraine](https://img.shields.io/badge/made_in-Ukraine-ffd700.svg?labelColor=0057b7)](https://stand-with-ukraine.pp.ua)
[![Stand With Ukraine](https://raw.githubusercontent.com/vshymanskyy/StandWithUkraine/main/badges/StandWithUkraine.svg)](https://stand-with-ukraine.pp.ua)
[![Russian Warship Go Fuck Yourself](https://raw.githubusercontent.com/vshymanskyy/StandWithUkraine/main/badges/RussianWarship.svg)](https://stand-with-ukraine.pp.ua)

# Youtube AutoManager
Adds videos to your YouTube playlists automatically based on rules
- Create a Project in the [Google Cloud Platform](https://console.cloud.google.com/projectcreate)
- Click Create Credentials

![](docs/images/creds_list.jpg)
- Select OAuth Client ID
- Select Application Type: Web Application
- Enter any Name
- Add Authorized redirect URI: https://localhost/
- Download Credentials JSON file

![](docs/images/creds_create.jpg)
- Create config file @ ~/youtube_automanager/youtube_automanager.yaml using the template file youtube_automanager.yaml.example
- Fill in the config file with your prefered video management rules
- Install the dependencies using "pip install -r requirements.txt"
- Run the program using "python -m youtube_automanager.runners.automanage"

The script:
- Gets all your subscribed channels
- Gets last 20 videos from each channel
- Checks if each video is already in the playlist
- If not, adds the video to the playlist based on rule it meets

Do not forget to run the Docker image with `--init` argument for SIGTERM to correctly forward to child processes.
