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
- A flat entry of a normal video has `live_status` null. Upcoming entries are not only at the top of the uploads playlist: they can sit between normal videos.
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
- A live `ytdlp_updater.update` of the stable channel into an empty folder took about 8 s when the uv cache already held the release. A second call that finds the same version makes one request to PyPI and takes about 0.3 s. `uv pip install --target` also writes `.lock`, `bin` and `share` into the target folder, next to `yt_dlp` and its `.dist-info`.
- The extraction functions are synchronous. A direct call blocks the event loop for the whole call.
- A PO Token is necessary only for video streams, player requests and subtitles of some clients. Flat extraction and search do not start the PO Token code.
- `YoutubeDLCookieJar.load` and `YoutubeDLCookieJar.save` accept a text stream, because their `open` helper gives a file object back as it is. Load the cookie text through `io.StringIO`.
- A cookie text with CRLF line ends in `io.StringIO` gives each cookie value a trailing CR. Split the text into lines before the load.
- yt-dlp writes each cookie line that it skips to stderr, with the cookie value. Python `MozillaCookieJar` puts a line that it refuses into the text of its `LoadError`. Check the lines before the load, and name only the line number in the error.
- `YoutubeDLCookieJar.save` sets `expires` of each session cookie to 0 in the jar itself. A cookie with `expires` 0 counts as expired, so the jar stops to send it, for example `YSC`. Save a copy of the jar.
- `http.cookiejar` removes each expired cookie from the jar when it builds the `Cookie` header of a request. The `ST-*` cookies and `CONSISTENCY` of an export are often expired already.
- The YouTube extractors set `hl=en` and `tz=UTC` in the `PREF` cookie of the jar when they start.
- Without the option `http_headers`, yt-dlp sends a random Chrome user agent, chosen once for each process (`yt_dlp.utils.networking.std_headers`). `YoutubeDL` puts `params["http_headers"]` above these defaults, and each request handler merges them under the headers of the request. The `web` client of the YouTube extractors sets no user agent of its own: `generate_api_headers` takes it from `INNERTUBE_CONTEXT.client.userAgent` of the page config, and YouTube puts the user agent of the page request there with `,gzip(gfe)` at the end. `BASE_OPTIONS` therefore gives the Firefox user agent of `youtube/user_agent.py`, and the rotation uses the same constant. A live check of the session with this user agent passed.
- A subclass of `YoutubeDL` can give its own `cookiejar` property. Each request handler and each extractor takes the jar from that property.
- Each HTTP request of yt-dlp goes through `YoutubeDL.urlopen`. An override of that method can pace the requests and see the final URL after a redirect.
- `_extract_response` sends a failed API call again up to `extractor_retries` times, also a call that changes data and a call that gets HTTP 401. With `extractor_retries` 0, it sends the call once.
- When `ytInitialData` of a page is incomplete, the `YoutubeTab` extractor downloads the page again up to three times before it reads `ytcfg`. A check of `LOGGED_IN` right after each page download stops at the first page.
- A flat entry of a video of two or more channels together has no `channel_id`. Its `creators` field has the names of the channels.
- A flat read of a playlist makes one more request, because yt-dlp reads the playlist again with its unavailable videos.
- An anonymous channel search by name gives up to 20 channels in one request. Some results have no subscriber count, for example 7 of 20 for one name, so the count stays optional.

## YouTube with cookies

- With browser cookies, yt-dlp reads the subscribed channels (`/feed/channels`), the playlists of the user with `WL` (`/feed/playlists`), Watch Later (`:ytwatchlater`) and the subscriptions feed (`:ytsubs`).
- In `/feed/playlists`, the owner field of most flat entries is the text "View full playlist". It does not tell an own playlist from a saved one.
- In `/feed/playlists`, only the flat entry of a saved playlist of another channel has `channel_id` and `channel_url` of the owner. An own playlist, Watch Later (`WL`), Liked videos (`LL`) and Saved Shorts (`YS`) have none.
- `playlist/get_add_to_playlist` with `{"videoIds": [<video id>], "excludeWatchLater": false}` lists Watch Later and each playlist that the account can add videos to. Each `playlistAddToOptionRenderer` has `playlistId`, `title.simpleText`, `privacy` and `containsSelectedVideos`. In a sample, it listed exactly the entries of `/feed/playlists` without an owner channel, except `LL` and `YS`.
- `account/account_menu` with an empty body gives `activeAccountHeaderRenderer`. Its `accountName.simpleText` is the channel title, `channelHandle.simpleText` is the handle, and `manageAccountTitle.runs[0].navigationEndpoint.browseEndpoint.browseId` is the channel id. The `ytcfg` has no channel id, and its `USER_ACCOUNT_NAME` is not the channel title.
- The `ytcfg` of a logged-in page holds the values of the cookies `LOGIN_INFO` and `__Secure-ROLLOUT_TOKEN`, and `INNERTUBE_CONTEXT.client.remoteHost` holds the IP address. Never log or store a `ytcfg` or a raw page.
- An anonymous request to `/feed/subscriptions` gives `LOGGED_IN` false and an empty list without an error. Each account read must check `LOGGED_IN`.
- An anonymous request to `/feed/channels` goes to the Google sign-in page on `accounts.google.com`. `/feed/playlists` and `WL` answer with `LOGGED_IN` false.
- `/feed/channels` gives all subscribed channels in one request, also for 554 channels. `/feed/playlists` gives all playlists in one request.
- `browse/edit_playlist` with `ACTION_ADD_VIDEO` puts the video at the top of Watch Later. Its answer has `status` `STATUS_SUCCEEDED` and `playlistEditResults[0].playlistEditVideoAddedResultData.videoId`. The answer of `ACTION_REMOVE_VIDEO_BY_VIDEO_ID` has `status` and no `playlistEditResults`.
- In `/feed/channels`, the `subscribeEndpoint` of a channel has `channelIds` and `params` `EgIIARgAUAE%3D`, and its `unsubscribeEndpoint` has `params` `CgIIARgA`.
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
- A fresh Firefox export holds cookies that live minutes (`GPS` 30 min, `CONSISTENCY` 10 min), `ST-*` cookies that are already expired, and `YSC` as a session cookie. The other cookies live 180 days (`VISITOR_INFO1_LIVE`, `VISITOR_PRIVACY_METADATA`, `__Secure-YNID`, `__Secure-BUCKET`, `__Secure-ROLLOUT_TOKEN`), 365 days (`PSIDTS` and `SIDCC` cookies) or 400 days (`SID` cookies, `LOGIN_INFO`, `PREF`).
- The earliest expiry of all cookies of an export is thus always minutes away. The service takes the earliest expiry of the login cookies only (`cookie_text.LOGIN_COOKIES`).
- One read check of the home page made the jar lose `GPS`, `CONSISTENCY` and the `ST-*` cookies, gave no new cookie name, and gave new values of exactly `SIDCC`, `__Secure-1PSIDCC` and `__Secure-3PSIDCC`. The text with these three new values passed a second check 5 s later.
- A later check of an upload gave new values of five known cookies: the three above, `__Secure-YNID` and `__Secure-ROLLOUT_TOKEN`. The set of the cookies with a new value thus changes from check to check.
- yt-dlp `YoutubeDLCookieJar.save` writes no `#HttpOnly_` mark. The write-back keeps each line of the stored text and changes only the value and the expiry date of a known cookie.

## The cookie session of the service

- The process of the service never imports yt-dlp. So a session check on request runs in the child command `check-session`, and the rotation uses `urllib` with `http.cookiejar`.
- Each stored state of the cookies is a row of `cookie_version`. The current version is the newest version that is not dead, and `account.cookies` holds a copy of it.
- An upload deletes the versions of the older upload, because they can belong to another account. A rollback thus never goes back to another export.
- An upload goes into the database only after its own session check passed, so a dead export never replaces the stored cookies. The settings page first refuses a text without each login cookie, or with a login cookie that expired by now. Then the child `check-upload` checks the text and stores it as a good version with the state of a passed check, and saves the new values of the check as a write-back version. A refused upload or a check without a clear answer changes nothing and sends no `cookie_session_dead`.
- SQLite gives an `INTEGER PRIMARY KEY` again after the row with the highest id went, unless the table has `AUTOINCREMENT`. The write-back compares the id of its base version with the current one, so `cookie_version` has `sqlite_autoincrement`.
- When a version dies, each newer version dies with it, and the newest good version becomes current. A version is good after it passed a session check. A rotation does not make a version good.
- A run checks the session at its start when cookies are stored, and that check costs one request with cookies. A session that dies during a run kills the version and the session. The next check tries the newest good version.
- The store keeps the 10 newest versions, the current version and the newest good version. A rotation every 10 minutes makes many versions between two runs, so the newest good version must stay outside the 10.
- The rotation is the setting `cookie_rotation` (true or false), on by default. Only a missing row gives the default: a stored `false` stays off, and a value that is not true or false also keeps the rotation off. The setting `cookie_rotation_next_at` keeps the time of the next rotation across a restart.
- `POST https://accounts.google.com/RotateCookies` with the cookies of `.youtube.com` returns HTTP 401, because those cookies belong to another domain.
- The YouTube Terms of Service forbid access by automated means. The yt-dlp wiki warns that an account that yt-dlp uses can get a temporary or permanent ban.
- Music Assistant and Youtarr do not refresh cookies. When the session ends, each one needs a new export from the user.

## YouTube Data API

- The API does not support `playlistItems.insert` for Watch Later, and `playlists.list` gives an empty list for `WL`. The default quota is 10,000 units per day. `playlistItems.insert` costs 50 units. `search.list` has its own limit of 100 calls per day.
