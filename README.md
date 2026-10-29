[![Stand With Ukraine](https://raw.githubusercontent.com/vshymanskyy/StandWithUkraine/main/banner-direct-single.svg)](https://stand-with-ukraine.pp.ua)
[![Made in Ukraine](https://img.shields.io/badge/made_in-Ukraine-ffd700.svg?labelColor=0057b7)](https://stand-with-ukraine.pp.ua)
[![Stand With Ukraine](https://raw.githubusercontent.com/vshymanskyy/StandWithUkraine/main/badges/StandWithUkraine.svg)](https://stand-with-ukraine.pp.ua)
[![Russian Warship Go Fuck Yourself](https://raw.githubusercontent.com/vshymanskyy/StandWithUkraine/main/badges/RussianWarship.svg)](https://stand-with-ukraine.pp.ua)

# YouTube AutoManager

YouTube AutoManager puts new YouTube videos into your playlists for you. You tell it which channels to watch and which videos you want. It then adds each matching video to a playlist of your choice, or to Watch Later. The service runs on your own computer or server in Docker, and you control it from a web page.

![The rule editor with two channels, a condition and the preview of the next run](media/rule.png)

## What you can do with it

- Make rules such as "each video of these three channels that is longer than 10 minutes goes to Watch Later".
- Pick videos by the title, the channel, the length, the upload date, live streams and Shorts.
- See which videos a rule picks before you save it.
- Add the older videos of a channel when you make a new rule.
- Manage your subscriptions: find channels on YouTube, subscribe to many channels or unsubscribe from them at once, and see the date of the newest video of each channel.
- Get a message in Telegram, Discord or another app after each run, or when something breaks.
- Use the pages in English or in Ukrainian.

The service adds each video only once. If you remove a video from the playlist, the service does not add it again.

## Before you start

The service does not use the official YouTube API. It acts like your browser, with the cookies of your YouTube account. The YouTube Terms of Service forbid access by automated means. The yt-dlp wiki warns that YouTube can ban such an account for some time or forever. Use the service only if you accept this risk.

<details>
<summary>What the service reads with your account, and what it reads without it</summary>

The service needs no YouTube Data API key and no Google Cloud project. It reads public data, for example the uploads of a channel, without an account. It uses the cookies only for your subscriptions and your playlists. It stores the cookies in its database, encrypted with a key that you give it.

</details>

## Install

You need a computer or a server that runs all the time, with Docker and Docker Compose.

1. Make a new folder. Copy two files of this repository into it: [`compose.yaml`](compose.yaml) and [`.env.example`](.env.example).
2. Rename `.env.example` to `.env`.
3. Make a secret key. The service uses this key to encrypt your cookies:
   ```
   docker compose run --rm youtube-automanager python -c "from youtube_automanager.youtube.session import generate_key; print(generate_key())"
   ```
4. Open `.env`, and set two lines. Remove the `#` at the start of each line that you change.
   ```
   COOKIES_KEY=<the key of step 3>
   TZ=Europe/Kyiv
   ```
   `TZ` is your time zone. Keep a copy of the key. If you lose the key, you must upload your cookies again.
5. Start the service:
   ```
   docker compose up -d
   ```
6. In your browser, open `http://<host>:8080`. Here `<host>` is the name or the IP address of the computer with the service.

<details>
<summary>Where the service keeps its data, and the owner of the data folder</summary>

The service keeps its database and its yt-dlp releases in the folder `data` next to `compose.yaml`. It runs as the user `1000:1000`. If the folder `data` belongs to another user, set that user in `compose.yaml`, for example `user: "99:100"` on Unraid.

</details>

### Protect the page

By default, the page has no password. Anybody who can open the page can change your rules and see the address of each notification target.

- To ask for a password, set `UI_PASSWORD` in `.env`. The browser asks for the password once, and any user name works.
- Do not open the port to the internet, because the password goes over plain HTTP. To reach the service from outside, use a VPN or a reverse proxy with HTTPS.

<details>
<summary>The check of the host name, and <code>ALLOWED_HOSTS</code></summary>

The service accepts only the host names of a home network: IP addresses, `localhost`, names without a dot such as `mini`, and names under `.ts.net`, `.local`, `.lan`, `.home.arpa` and `.internal`. It refuses each other host name with HTTP 400. This check stops a web page on the internet that tries to reach the service through your browser.

If you open the service under another domain name, for example through a reverse proxy, add that name to `ALLOWED_HOSTS`. A name that starts with a dot also allows the names under it. The value `*` turns the check off.

</details>

## Connect your YouTube account

The service needs the cookies of a YouTube session. Follow these steps exactly, because a wrong export gives cookies that stop working in a few minutes or hours.

1. Use Firefox. Chrome and other Chromium browsers can give cookies that stop working a few hours after the export.
2. Close each private window of Firefox.
3. Open a new private window. Sign in to YouTube there with the account that the service will use.
4. In the same window, open https://www.youtube.com/robots.txt in a new tab. Then close the YouTube tab.
5. Export the cookies of youtube.com to a `cookies.txt` file in the Netscape format. Use a cookie export add-on that can run in private windows.
6. Close the private window. Do not sign out, because a sign-out ends the session of the exported cookies.
7. In the service, open the Settings page. Upload the file, or paste its text.

The service checks the cookies with YouTube first, and it saves them only when YouTube accepts them. If your cookies stop working later, the service sends you a notification. Then repeat these steps.

<details>
<summary>Why the export needs these steps</summary>

YouTube keeps an exported session alive only while no browser tab uses it. A YouTube tab that stays open can end the session of an export within about 20 minutes. All private windows of Firefox share one session, so only a new private window after you close all of them gives a new session.

A dead export never replaces the stored cookies. Before each run, the service checks the session again. It sends a notification when the session ends or when the cookies expire soon.

The cookie rotation on the Settings page renews the session about every 10 minutes, as an open YouTube tab does. It is on by default.

</details>

## Make your first rule

1. On the Rules page, click "Refresh lists". The service reads your subscriptions and your playlists.
2. Click "New rule". Choose the channels, add the conditions, and choose the target playlist.
3. Click "Preview" to see which videos the rule picks.
4. Click "Save".

The service runs once a day at 18:00 in your time zone. You can change this time on the Scheduler page. To run at once, click "Run now" on the Dashboard.

A new rule also adds the matching videos of the last 7 days. To add older videos, open a saved rule and click "Apply to videos from a date". This job runs in the background, so you can close the page.

<details>
<summary>How a rule reads videos, and the conditions of a rule</summary>

A rule reads the uploads of each of its channels. A rule without channels reads the subscriptions feed of your account. The service checks each new video against each enabled rule.

A condition of a rule is a tree of groups: "all of", "any of" and "none of". A condition reads the title, the channel, the duration, the upload date, the live status or Shorts. A title condition can be a regular expression.

</details>

## The pages

The menu on the left opens each page. In the header, you change the language and the theme. Until you choose a language, the pages follow the language of your browser, and English is the default.

| Page | What you do there |
|---|---|
| Dashboard | See at a glance whether everything works, and start a run. |
| Rules | See, turn on and off, and edit your rules. |
| Subscriptions | Find channels, subscribe and unsubscribe, and add channels to rules. |
| Scheduler | Choose when the service runs. |
| History | See each run and what it did with each video. |
| Settings | Upload cookies, set up notifications, and manage yt-dlp. |

The pages show a run while it runs, with no reload.

<details>
<summary>Dashboard</summary>

The Dashboard shows four cards: the cookie session, the scheduler, the last run and yt-dlp. The scheduler card has the button "Run now". The title of the last run card opens that run on the History page. If the cookies are dead or expire soon, the cookie card links to the Settings page with "Upload new cookies".

</details>

<details>
<summary>Rules and the rule editor</summary>

The Rules page lists the rules in a table, with the filters "Search", "Playlist" and "State" and the button "New rule". The switch in the column "Enabled" turns a rule on or off at once. A click on a column header sorts the table. The column "Added 7 d / 30 d" counts the videos that each rule added in the last 7 and the last 30 days. The filters stay in the address, for example `/rules?state=enabled`, so a reload or a shared link shows the same rules. While an apply runs, a line above the table names its rule and shows the count of added videos.

A click on a row, or on its button "Edit", opens the rule editor. The editor has its own address: `/rules/new` for a new rule and `/rules/<id>` for a saved rule. It has the name, the channels, the target playlist, the condition, the switch "No Shorts", the field "Also add videos from", the switch "Enabled" and the button "Preview".

A new rule starts with "Also add videos from" set to 7 days back, so the next run also adds the matching videos of these days. Clear the field to add only new videos. The preview stays on the page when you change the form. Click "Preview" again to see the result of the changed form.

A saved rule also has the buttons "Duplicate", "Apply to videos from a date" and "Delete". "Duplicate" opens a new rule with a copy of the saved rule, with the name "Copy of" and the old name. If the form has changes that you did not save, the editor asks "Discard the changes?" before you leave it, and the browser warns before a reload. The Back button of the browser leaves without this question.

</details>

<details>
<summary>Subscriptions</summary>

The Subscriptions page has the button "Refresh lists", a search for channels on YouTube, and the table "Stored channels". The table holds the subscriptions of the last refresh and each channel that a rule reads. A load of the page sends no request to YouTube.

The filters of the table are "Name or @handle", "Subscription", "Rules" and the switch "Only selected". The column "Last video" shows the day of the newest video that the service saw on the channel. A dash means that the service does not read the channel.

A click on the title of a channel opens the channel on YouTube. The menu at the end of a row subscribes, unsubscribes, or adds the channel to a rule.

A click on a row selects the channel. The checkbox in the header selects each channel that the filters show, also on the other pages of the table. The selection stays when you change the filters or the search. The bar of the selection shows the count of the selected channels and how many of them the filters hide. Its buttons subscribe to the selected channels, unsubscribe from them, add them to a rule, or clear the selection.

YouTube limits how fast an account can send requests, so the service paces its requests. While a change of many channels runs, the bar shows the progress, the requests that can go without a wait, and the time of the next request. Click "Stop" to stop the change. If YouTube does not confirm a change, the page lists each channel that did not change, with the cause.

</details>

<details>
<summary>Scheduler</summary>

The Scheduler page shows the state of the schedule and its next 5 runs, with the buttons "Pause", "Resume" and "Run now". The form "Schedule" sets a "Cron" or an "Interval" schedule, the time zone and the switch "Enabled". The form shows the next runs of the edited schedule before you click "Save".

</details>

<details>
<summary>History</summary>

The History page lists the runs, 50 on a page and the newest first, with the filters "Status", "Trigger", "From" and "To". A click on a run opens its actions below the table, one line for each decision about a video. The filter "Action" narrows them. The filters, the page and the open run stay in the address, for example `/history?run=12`. A run in progress shows its new counts and actions as they come. The card "Retention" sets how many days the service keeps the history.

</details>

<details>
<summary>Settings</summary>

The Settings page has a block for each of these: the YouTube cookies, the cookie rotation, the processing mode, yt-dlp and the notification targets. A change of the rotation, the processing mode or the yt-dlp settings waits until you click "Save" at the bottom right of the page. "Discard" drops the change. The cookie upload, the session check, "Update now" and the buttons of the notification targets act at once.

The service sends notifications through [Apprise](https://appriseit.com/services/), for example to Telegram or Discord. Examples are a summary of each run, a failed run, and cookies that stop working.

</details>

## When something breaks

YouTube changes its pages often. The service uses yt-dlp to talk to YouTube, and yt-dlp follows these changes with new releases. The service installs the newest release of yt-dlp each day at 04:00, so most problems go away by themselves.

You see a problem in these places:

- A notification, for example "cannot read the channel", a run summary with errors, or "the run failed".
- The History page, with each error of a run and its cause.
- The yt-dlp card of the Settings page, with the result of the last update.

If a new release of yt-dlp breaks the service, use the yt-dlp card of the Settings page:

1. To go back to the release before the last change, click "Go back to the previous version" and confirm.
2. To use the yt-dlp of the image, click "Use the image version" and confirm.
3. To stop the daily update, turn off "Update each day" and click "Save".
4. When a fix comes out, click "Update now" to install the newest release.

<details>
<summary>How the service checks a new release of yt-dlp</summary>

Before a new release replaces the active one, the service checks it in a separate process. The check imports the release and makes sure that it has each part of yt-dlp that the account client uses. Then it reads the newest upload of the official YouTube channel and, with stored cookies, checks the session. If any step fails, the active release stays, also when the network fails. The service then sends the notification "the yt-dlp update failed" with the cause.

The yt-dlp card shows the active version of yt-dlp, its source, the previous version, the skipped version, and the result of the last update.

If an error names a version of yt-dlp, for example "yt-dlp 2026.08.19 does not fit the client", that release changed a part that the service uses. The run then goes on without the account. It still reads the public channels, but it adds no video.

After "Go back to the previous version" or "Use the image version", the daily update skips the version that you left. It installs a release again only when a newer one comes out. "Update now" always installs the newest release, also the skipped one.

</details>

## Update and back up

To update the service, run these commands in the folder of `compose.yaml`:

```
docker compose pull
docker compose up -d
```

To make a backup, stop the service and copy the folder `data` together with `.env`. Without the key in `.env`, the service cannot read the stored cookies.

<details>
<summary>Automatic copies of the database, and a return to an old version</summary>

As a smaller backup, copy the database file, by default `data/youtube_automanager.sqlite`, and the line `COOKIES_KEY` of `.env`.

Before a new version changes the schema of a SQLite database, the service copies the database into the folder `data/backups`. The name of a copy holds the old schema revision and the time in UTC. The service keeps the newest 3 copies and deletes the older ones. If the copy fails, the service does not change the database and does not start. The error names the folder of the copies.

To go back to the old version, stop the service, replace the database file with the copy, and start the image of the old version.

The service makes no copy of a PostgreSQL database. Before you update the image, make a dump with `pg_dump`, for example `pg_dump -Fc -f youtube_automanager.dump <database URL>`.

</details>

<details>
<summary>Image tags</summary>

The image is `ghcr.io/alertua/youtube_automanager`.

| Tag | What it is |
|---|---|
| `latest` | The newest release. A run without a tag uses it. |
| `1`, `1.1`, `1.1.0` | A major line, a minor line, or one exact release. `1.1` gets each new `1.1.x` release. |
| `0.1`, `0.1.2` | Version 0.1, the old service before the rewrite. |
| `edge` | The newest commit of `main`. It can break at any time. |

</details>

## Reference

<details>
<summary>All settings of <code>.env</code></summary>

Each line of `.env.example` explains its setting and shows the default value.

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
| `UI_PASSWORD` | none | The password of the web UI and of the REST API, with any user name. `/health` needs no password. |
| `ALLOWED_HOSTS` | none | More host names of the web UI, separated by commas. A name that starts with a dot also allows the names under it. `*` turns the check of the host name off. |

</details>

<details>
<summary>How a run works, and the processing modes</summary>

A run reads each source back to the videos that it already saw. A source is a channel of an enabled rule, or the subscriptions feed for the rules without channels. A run checks only the videos that came since the last run. So a new or changed rule acts only on new videos.

The first run of a source stores its newest videos as seen and adds none of them. This first read is a baseline, so a new channel never fills the playlist with its old videos. "Apply to videos from a date" adds older videos on request.

The processing mode of the Settings page decides how far a run reads each source:

| Mode | A run reads |
|---|---|
| Stop at a seen video (default) | The newest videos down to the first video that a run saw before, and at least the 20 newest. |
| A fixed number of the newest videos | The newest videos, as many as the count that you set. |
| Stop at the date mark | The videos from the day of the newest video that a run saw. Each source keeps its own date mark. |

A change of the mode starts each source again with a baseline.

</details>

<details>
<summary>REST API</summary>

The REST API lets a script do what the pages do: start a run, read the runs, and read or change the rules and the schedule. The page `/docs` of the service lists each route with its fields. If you set `UI_PASSWORD`, it protects the API like the pages, except `/health`. A client sends the password with HTTP Basic authentication, with any user name. For example, this command reads the status of the service:

```
curl -u user:password http://<host>:8080/api/status
```

The routes come in these groups:

- Status: `GET /health` and `GET /api/status`.
- Runs: `GET /api/runs`, `GET /api/runs/<id>` with the actions of the run, and `POST /api/runs` to start a run.
- Rules: `GET /api/rules` and `POST /api/rules`, and `GET`, `PUT` and `DELETE` on `/api/rules/<id>`. A rule needs a playlist and channels that the last refresh of the lists stored.
- Schedule: `GET /api/schedule` and `PUT /api/schedule`.
- Account: `POST /api/session/check` checks the cookies, and `POST /api/account/refresh` refreshes the lists.

A route that starts a run, checks the session, refreshes the lists or deletes a rule answers 409 while another task with YouTube is active. The error texts of the API are always in English, also when the pages show Ukrainian.

</details>

## Contributing

To run the service from the source code, run the tests or update the translations, read [CONTRIBUTING.md](CONTRIBUTING.md).
