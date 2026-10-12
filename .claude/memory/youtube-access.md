---
name: youtube-access
description: "Read this file before you write code that reads or writes YouTube data, or that handles the cookie session."
metadata:
  type: reference
---

## Principles

- The service uses no YouTube Data API. Public reads go through yt-dlp without cookies. Account reads and writes go through yt-dlp with the cookies of the user.
- Pause 5 s between two requests that carry cookies. Anonymous reads need no pause.
- A check of the session reads `ytcfg` of `https://www.youtube.com` and looks at `LOGGED_IN`. A dying session first answers as an anonymous user, and only later gives HTTP 401. The expiry dates in the cookie file do not show the end of a session.

## yt-dlp

- Anonymous flat extraction of `https://www.youtube.com/playlist?list=UU<channel id without the UC prefix>` gives all uploads of a channel in one request. The uploads include videos, Shorts and past live streams, each with a duration. A past stream has `live_status=was_live`. A Short has no mark.
- The uploads playlist lists the newest video first. A sample of 1800 entries from 18 channels had no entry out of order.
- Upcoming and live entries occur, and they have no date and no duration. In one sample of 1800 entries, about one entry in nine had no date, and nearly all of them were 180 s or shorter. In a later sample of 1700 entries, only the upcoming and live entries had no date. Keep the date optional.
- A flat entry gets a date only with the extractor argument `youtubetab:approximate_date`. The date comes as `timestamp` at UTC midnight, never as `upload_date`. It is only as exact as the relative text of YouTube, for example "1 month ago". The channel tab `/shorts` gives no duration, no date and no channel fields.
- One request returns up to 100 entries in 1 to 3 s.
- To read a long playlist page by page, call `extract_info(url, download=False, process=False)`. Its `entries` is a generator that requests the next page only when the iteration reaches it. With `extract_flat` and `lazy_playlist`, `extract_info` returns only after it read the whole playlist, for example 9.5 s and 20 requests for 1852 uploads.
- A missing handle gives a `DownloadError` whose cause is an `HTTPError` with status 404. A missing channel ID gives an `ExtractorError` with the text "YouTube said: This channel does not exist." A channel without Shorts gives "This channel does not have a shorts tab".
- yt-dlp writes `ERROR:` lines to stderr also with `quiet` and `no_warnings`. Only a `logger` option stops them.
- `YoutubeDL` has `close()` and `__exit__`, but no `__del__`, so an instance that nobody closes keeps its connections until the garbage collector finds it. With lazy `entries`, close the instance when the entries end, not when `extract_info` returns.
- yt-dlp has no write action on YouTube. A write goes through internal methods of the `YoutubeTab` extractor: `_extract_data` for `ytcfg`, `generate_api_headers`, and `_extract_response` with `ep='browse/edit_playlist'`. A method name with a leading underscore is internal, and a new release of yt-dlp can change it.
- `browse/edit_playlist` with `{"action": "ACTION_ADD_VIDEO", "addedVideoId": <id>}` and `playlistId` `WL` adds a video to Watch Later and returns `status` `STATUS_SUCCEEDED`. `ACTION_REMOVE_VIDEO_BY_VIDEO_ID` with `removedVideoId` removes it.
- To read a cookie file without a change to the file, do not set `cookiefile`. Load the file with `ydl.cookiejar.load(path, ignore_discard=True, ignore_expires=True)`.
- A yt-dlp that pip or uv installed cannot update itself. Its self-updater operates only on release builds, for example the standalone executable. A new version of an imported module takes effect only in a new process.
- The extraction functions are synchronous. A direct call blocks the event loop for the whole call.
- A PO Token is necessary only for video streams, player requests and subtitles of some clients. Flat extraction and search do not start the PO Token code.

## YouTube with cookies

- With browser cookies, yt-dlp reads the subscribed channels (`/feed/channels`), the playlists of the user with `WL` (`/feed/playlists`), Watch Later (`:ytwatchlater`) and the subscriptions feed (`:ytsubs`).
- In `/feed/playlists`, the owner field of most flat entries is the text "View full playlist". It does not tell an own playlist from a saved one.
- The subscriptions feed does not have most videos of 180 s or shorter. In a sample, it had 5 of 82 such videos and all 82 longer videos.
- A session from a browser tab that stays open can end about 20 minutes after the export of its cookies. The yt-dlp wiki gives the export method: a private window with only `https://www.youtube.com/robots.txt` open, which you close after the export. All private windows of Firefox share one session, so a second independent session needs a new private window after all private windows are closed.
- Chromium with Device Bound Session Credentials gives cookies that live a few hours after export. Export from Firefox.
- Google checks `PSIDTS` against the IP address of the session.
- A read check makes YouTube send new values of `SIDCC`, `__Secure-1PSIDCC` and `__Secure-3PSIDCC`, and remove the short-lived `ST-*` cookies.
- A cookie write-back without a filter can save the removal of `LOGIN_INFO` and `SAPISID`. Accept only new values of known cookies, and never a removal.
- A browser tab rotates the session of `.youtube.com` in two requests, and a server with only `.youtube.com` cookies can repeat them:
    1. `GET https://accounts.youtube.com/RotateCookiesPage?origin=https://www.youtube.com&yt_pid=1` with the `.youtube.com` cookies. The page holds `init('<id>', ...)`.
    2. `POST https://accounts.youtube.com/RotateCookies` with the body `[null,"<id>",1]`, `Content-Type: application/json`, `Origin: https://accounts.youtube.com` and the page URL as `Referer`. The answer is HTTP 200 with new `__Secure-1PSIDTS`, `__Secure-3PSIDTS`, `SIDCC`, `__Secure-1PSIDCC` and `__Secure-3PSIDCC`. Its body, after a prefix of 5 characters, is JSON with the next interval in seconds, for example `[["identity.hfcr",600],...]`.
- The browser repeats the rotation at the interval from the answer, 600 s. A second rotation within 1 to 10 minutes can give HTTP 429.
- `POST https://accounts.google.com/RotateCookies` with the cookies of `.youtube.com` returns HTTP 401, because those cookies belong to another domain.
- The YouTube Terms of Service forbid access by automated means. The yt-dlp wiki warns that an account that yt-dlp uses can get a temporary or permanent ban.
- Music Assistant and Youtarr do not refresh cookies. When the session ends, each one needs a new export from the user.

## YouTube Data API

- The API does not support `playlistItems.insert` for Watch Later, and `playlists.list` gives an empty list for `WL`. The default quota is 10,000 units per day. `playlistItems.insert` costs 50 units. `search.list` has its own limit of 100 calls per day.
