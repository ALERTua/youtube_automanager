[![Stand With Ukraine](https://raw.githubusercontent.com/vshymanskyy/StandWithUkraine/main/banner-direct-single.svg)](https://stand-with-ukraine.pp.ua)
[![Made in Ukraine](https://img.shields.io/badge/made_in-Ukraine-ffd700.svg?labelColor=0057b7)](https://stand-with-ukraine.pp.ua)
[![Stand With Ukraine](https://raw.githubusercontent.com/vshymanskyy/StandWithUkraine/main/badges/StandWithUkraine.svg)](https://stand-with-ukraine.pp.ua)
[![Russian Warship Go Fuck Yourself](https://raw.githubusercontent.com/vshymanskyy/StandWithUkraine/main/badges/RussianWarship.svg)](https://stand-with-ukraine.pp.ua)

# YouTube AutoManager

YouTube AutoManager adds new YouTube videos to your playlists by your rules. It runs all the time as one service with a web UI, and it checks the channels on a schedule. A rule names the channels to watch, the conditions that a video must meet, and the target playlist, also Watch Later.

## What the service does

- It reads the uploads of each channel of a rule, or the subscriptions feed of your account for a rule without channels.
- It checks each new video against each enabled rule, and it adds a matching video to the target playlist once. A video that you removed from the playlist does not come back.
- A rule condition is a tree of groups: "all of", "any of" and "none of". Its conditions read the title, the channel, the duration, the upload date, the live status and Shorts. A title condition can be a regular expression.
- The rules page shows a preview of what the next run does with a rule. The button "Apply to videos from a date" adds the matching older videos of a saved rule. The apply runs in the background of the service, so you can close the page, and the Rules page and the History page show it while it runs.
- The subscriptions page lists your subscriptions, finds channels on YouTube, and subscribes or unsubscribes.
- The scheduler page sets the schedule of the runs, and the history page shows each run with each decision about a video.
- The service sends notifications through [Apprise](https://appriseit.com/services/), for example to Telegram or Discord. Examples are a summary of each run, a failed run, and cookies that stop to work.
- The service updates yt-dlp by itself from the settings page or on a schedule, without a new image.

## How the service reaches YouTube

The service uses no YouTube Data API key and no Google Cloud project. It reads public data, for example the uploads of a channel, without an account. For your subscriptions and your playlists, it uses the cookies of a YouTube session in your browser. It stores the cookies in its database, encrypted with a key that you give it.

The YouTube Terms of Service forbid access by automated means. The yt-dlp wiki warns that an account that yt-dlp uses can get a temporary or permanent ban. Use the service only if you accept this risk.

## Start with Docker Compose

You need Docker with Docker Compose.

1. Copy `compose.yaml` and `.env.example` of this repository into a new folder. Rename `.env.example` to `.env`.
2. Make a key for the cookies:
   ```
   docker compose run --rm youtube-automanager python -c "from youtube_automanager.youtube.session import generate_key; print(generate_key())"
   ```
3. In `.env`, set `COOKIES_KEY` to that key, and set `TZ` to your time zone, for example `TZ=Europe/Kyiv`. Keep the key: with another key, the service cannot read the stored cookies.
4. Start the service:
   ```
   docker compose up -d
   ```
5. Open `http://<host>:8080` in a browser.

The service keeps its database and its yt-dlp releases in the folder `data` next to `compose.yaml`. It runs as the user `1000:1000`. If the folder `data` belongs to another user, set that user in `compose.yaml`, for example `user: "99:100"` on Unraid.

The web UI has no password by default. Anybody who can open the page can change the rules and see the URL of each notification target. To ask for a password, set `UI_PASSWORD` in `.env`. The browser then asks for it once, and any user name works. The password goes over plain HTTP, so do not publish the port on the internet. Reach the service through a VPN, or through a reverse proxy with HTTPS.

The service accepts only the host names of a home network: IP addresses, `localhost`, names without a dot such as `mini`, and names under `.ts.net`, `.local`, `.lan`, `.home.arpa` and `.internal`. It refuses each other host name with HTTP 400. This check stops a web page on the internet that tries to reach the service through your browser. If you open the service under another domain name, for example through a reverse proxy, add that name to `ALLOWED_HOSTS`.

## Give the service your YouTube session

A session lives only as long as no browser tab of it stays open. A YouTube tab that stays open can end the session of an export within about 20 minutes. Export the cookies this way:

1. Use Firefox. Chrome and other Chromium browsers can give cookies that stop to work a few hours after the export.
2. Close each private window of Firefox. All private windows share one session, so only a new private window after that gives a new session.
3. Open a new private window, and sign in to YouTube there with the account that the service uses.
4. In the same window, open https://www.youtube.com/robots.txt in a new tab, and close the YouTube tab.
5. Export the cookies of youtube.com as a `cookies.txt` file in the Netscape format, with a cookie export add-on that can run in private windows.
6. Close the private window without a sign-out. A sign-out ends the session of the exported cookies.
7. On the settings page of the service, upload the file or paste its text.

The service first checks the cookies with YouTube, and it stores them only when YouTube accepts them. A dead export never replaces the stored cookies. Before each run, the service checks the session again, and it sends a notification when the session ends or the cookies expire soon. The cookie rotation of the settings page renews the session about every 10 minutes, as an open YouTube tab does. It is on by default.

## First steps in the web UI

1. On the rules page, click "Refresh lists", so the service reads your subscriptions and your playlists.
2. Create a rule: choose the channels, add the conditions, and choose the target playlist.
3. Click "Preview" to see what the next run does with the rule.
4. Save the rule. The next run on the schedule uses it, or click "Run now" on the scheduler page.
5. To add older videos of a saved rule, open the rule and click "Apply to videos from a date".

The default schedule runs once a day at 18:00 in the time zone of `TZ`.

## How a run works

A run reads each source back to the videos that it already saw. A source is a channel of an enabled rule, or the subscriptions feed for the rules without channels. A run checks only the videos that came since the last run. A new or changed rule thus acts only on new videos.

The first run of a source stores its newest videos as seen and adds none of them. This first read is a baseline, so a new channel never fills the playlist with its old videos. "Apply to videos from a date" adds older videos on request.

The processing mode of the settings page decides how far a run reads each source:

| Mode | A run reads |
|---|---|
| Stop at a seen video (default) | The newest videos down to the first video that a run saw before, and at least the 20 newest. |
| A fixed number of the newest videos | The newest videos, as many as the count that you set. |
| Stop at the date mark | The videos from the day of the newest video that a run saw. Each source keeps its own date mark. |

A change of the mode starts each source again with a baseline.

## When YouTube or yt-dlp breaks

YouTube changes its pages often, and yt-dlp follows with new releases. The service installs the newest release of the chosen channel each day at 04:00.

Before a new release replaces the active one, the service checks it in a separate process. The check imports the release, makes sure that it has each part of yt-dlp that the account client uses, reads the newest upload of the official YouTube channel, and, with stored cookies, checks the session. If any step fails, the active release stays, also when the network fails. The service then sends the notification "the yt-dlp update failed" with the cause.

When a run meets a problem, you see it in these places:

- The notifications: "cannot read the channel" for one source, the run summary with the count of its errors, or "the run failed".
- The History page: each run with its errors and the cause of each error.
- The yt-dlp card of the Settings page: the active version of yt-dlp, its source, the previous version, the skipped version, and the result of the last update.

If an error names a version of yt-dlp, for example "yt-dlp 2026.08.19 does not fit the client", that release changed a part that the service uses. The run then goes on without the account, so it still reads the public channels, but it adds no video.

To fix the problem, use the yt-dlp card of the settings page:

1. To return to the release that the last change replaced, click "Go back to the previous version" and confirm.
2. To use the yt-dlp that came with the image, click "Use the image version" and confirm.
3. To stop the daily update, turn off "Update each day" and click Save.
4. To install the newest release when a fix comes out, click "Update now".

After each of the first two actions, the daily update skips the version that you left. It installs a release again only when a newer one comes out. "Update now" always installs the newest release, also the skipped one.

## Settings

The file `.env` holds the settings. Each line of `.env.example` explains its setting and shows the default value.

| Setting | Default | What it does |
|---|---|---|
| `COOKIES_KEY` | none | The key that encrypts the cookies in the database. Without it, the service cannot store cookies. |
| `TZ` | `UTC` | The time zone of the schedule and of the times on the pages. |
| `VERBOSE` | `False` | More detail in the log, for example each request of yt-dlp. |
| `DATABASE_URL` | a SQLite file in `/data` | The database. For PostgreSQL, give for example `postgresql://user:password@host:5432/youtube_automanager`. |
| `DB_FILENAME` | `youtube_automanager.sqlite` | The file name of the SQLite database, when `DATABASE_URL` is not set. |
| `YTDLP_DIR` | `/data/yt-dlp` | The folder of the yt-dlp releases that the updater installs. |
| `CHILD_TIMEOUT_S` | `3600` | The longest time of one run in seconds. |
| `HOST`, `PORT` | `0.0.0.0`, `8080` | The address and the port inside the container. Change the port mapping in `compose.yaml` instead. |
| `UI_PASSWORD` | none | The password of the web UI, with any user name. `/health` needs no password. |
| `ALLOWED_HOSTS` | none | More host names of the web UI, separated by commas. A name that starts with a dot also allows the names under it. `*` turns the check of the host name off. |

## Backup

The service keeps all its data in the folder `data`. To make a backup, stop the service and copy the folder `data` together with `.env`. As a smaller backup, copy the database file, by default `data/youtube_automanager.sqlite`, and the line `COOKIES_KEY` of `.env`. Without the key, the service cannot read the stored cookies.

Before a new version changes the schema of a SQLite database, the service copies the database into the folder `data/backups`. The name of a copy holds the old schema revision and the time in UTC. The service keeps the newest 3 copies and deletes the older ones. If the copy fails, the service does not change the database and does not start. The error names the folder of the copies.

To go back to the old version, stop the service, replace the database file with the copy, and start the image of the old version.

The service makes no copy of a PostgreSQL database. Before you update the image, make a dump with `pg_dump`, for example `pg_dump -Fc -f youtube_automanager.dump <database URL>`.

## Migration from version 0.1

Version 1.0 replaces version 0.1 completely. It reads no YAML configuration, and it uses no Data API. The old data folder and the old configuration do not move to the new service, so you make the rules again in the web UI.

What changed:

- The rules live in the database, and you edit them in the web UI. The file `youtube_automanager.yaml` is not read any more.
- The service uses the cookies of your browser session, not an OAuth client of Google Cloud. The client secrets file and the stored tokens of version 0.1 are not used any more.
- A rule matches a channel only by its channel ID, which you choose from your subscriptions or from a search. A channel name or a handle can change, and another channel can take it later.
- The criteria of a rule now combine with "all of" by default. In version 0.1, a channel and a title pattern of one rule combined with "or". So the rule added each video of the channel, and each video with that title from any subscription.
- Watch Later works as a target playlist.
- The service runs all the time with its own schedule. It needs no cron job and no external scheduler.

How to move:

1. Write down the rules of your `youtube_automanager.yaml`: the channels, the title patterns, the minimum duration and the target playlist of each rule.
2. Stop and remove the container of version 0.1.
3. Start version 1.0 with a new folder, as "Start with Docker Compose" tells.
4. Upload your cookies on the settings page, as "Give the service your YouTube session" tells.
5. On the rules page, click "Refresh lists", then make each rule again. A minimum duration of 120 seconds becomes the condition "Duration is at least 2:00", or use the switch "No Shorts".
6. Check each rule with "Preview". The first run of each channel takes a baseline. So use "Apply to videos from a date" for the videos since the stop of the old service.
7. When the new service works, delete the old data folder and the credentials of the Google Cloud project.

The image tags `0.1` and `0.1.2` keep version 0.1, if you need it again.

## Image tags

The image is `ghcr.io/alertua/youtube_automanager`.

| Tag | What it is |
|---|---|
| `latest` | The newest release. A run without a tag uses it. |
| `1`, `1.0`, `1.0.0` | A major line, a minor line, or one exact release. `1.0` gets each new `1.0.x` release. |
| `0.1`, `0.1.2` | Version 0.1, the old service before this rewrite. |
| `edge` | The newest commit of `main`. It can break at any time. |

## Development

You need [uv](https://docs.astral.sh/uv/) and [just](https://github.com/casey/just). The recipes run in the folder of the repository.

| Recipe | What it does |
|---|---|
| `just install` | Installs the dependencies of the service and of the development tools. |
| `just serve` | Starts the service on http://localhost:8080, with its data in the folder `data`. It ignores `DATABASE_URL`, `YTDLP_DIR` and `HOME` of `.env`, because these values are for the container. To use another database, set `LOCAL_DATABASE_URL`. |
| `just test` | Runs the tests on each core of the machine. |
| `just test-serial` | Runs the tests in one process, for example `just test-serial --durations=0`. |
| `just cov` | Runs the tests with the coverage report, and fails when the coverage of the service drops below the limit of the justfile. |
| `just lint` | Formats the code, fixes the lint findings and checks the types with ty. |
| `just pot` | Extracts the texts of the web UI into `youtube_automanager/i18n/messages.pot` again. |
| `just po` | Updates the Ukrainian catalog `youtube_automanager/i18n/locale/uk/LC_MESSAGES/messages.po` from `messages.pot`. Translate each new message and check each fuzzy one in it: the tests fail on both. |
| `just mo` | Compiles the catalog into the `messages.mo` file that the service loads. Run it once before `just serve`, so the pages can show Ukrainian. Git ignores the `.mo` file, and the image build makes it. |
| `just pre` | Runs each pre-commit hook on each file. |
| `just smoke` | Builds the image with wslc, starts a container of it on port 18080 with the settings of `.env`, waits until wslc reports it healthy, and calls `/health`. It removes the container at the end. |

The tests of the database, the migrations, the sync engine and the runner also run on PostgreSQL when `TEST_POSTGRES_URL` names a server, for example `postgresql://postgres@localhost:5432/postgres`. Each of these tests then runs once on SQLite and once on PostgreSQL. Each worker of a parallel run makes a template database of its own on that server, and each test gets a copy of the template. The tests of the web pages stay on SQLite. The CI workflow `Tests` runs the tests so on each pull request. On Windows, give `127.0.0.1` instead of `localhost`, because each connection to `localhost` first waits for IPv6.
