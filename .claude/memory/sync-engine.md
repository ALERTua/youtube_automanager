---
name: sync-engine
description: "Read this file before you change the sync engine, its processing modes, or the state that it keeps in the database."
metadata:
  type: reference
---

## Sources and state

- Each channel of an enabled rule is a source. The engine reads it through its uploads playlist without cookies. The rules without channels share one more source: the subscriptions feed, which the engine reads with the cookies.
- `seen_video.channel_id` holds the source key: the `UC...` id of a channel, or the fixed value `feed`. A video that a channel showed is thus still new to a feed rule.
- Each full read of a source writes the time of the read: `channel.last_checked_at` for a channel, and the setting `feed_last_read_at` for the feed. A baseline and a read that gives no entries also write it.
- A source that stops at a failed add, or that fails to read, keeps the time of its last full read. A newer time would let the next run stop above the videos that stay without a record.
- A source that was read before has state, also without `seen_video` rows and without a mark. An empty channel thus gets no second baseline, and its first video gets an evaluation.
- A source without stored state and without a read time gets a baseline: the engine stores the window of the mode as seen, with no evaluation and no add. An upcoming or live entry of a baseline is stored as deferred.
- At the end of a finished run, a source that no enabled rule reads loses its `seen_video` rows, its mark and its read time. A channel that a rule reads again thus starts with a baseline.
- An `added_video` row goes when the clean-up removes the last `seen_video` row of its video. A source that failed in the run keeps all its rows.

## The order of the work

- For each source, the engine first reads the whole window, then evaluates the entries from the oldest to the newest.
- Each add commits its `added_video` row at once. The `seen_video` row of a video comes only after all its adds.
- A failed add stops that source for the run. The video and the newer ones stay without a record, so the next run reads them again.
- A read error of a channel writes an `error` action, sends `channel_error`, and the run continues with the other sources.
- `NotLoggedInError` sets `account.session_state` to `dead` and stops the run.
- When the run stops, the child writes the cause as the last line of stderr. The parent stores the end of stderr as the error text of the run.
- The engine opens the account only at the first need: a read of the feed or an add. A run that adds nothing needs no cookies.

## Processing modes

- The setting `processing_mode` holds the mode: `{"mode": "stop_at_seen"}` (the default), `{"mode": "newest", "count": N}` or `{"mode": "mark_date"}`.
- Mode `stop_at_seen` stops when it has read at least 20 entries and met a seen entry. A deferred entry is never a stop point.
- After the 20 newest entries, mode `stop_at_seen` also stops at the first dated entry that is older than the day of the last read minus 2 days. A source whose stored videos all vanished thus reads only the recent days, not the whole channel. An entry without a date never stops the read.
- The setting `processing_mode_of_state` holds the mode of the stored state. When the mode changes, the engine removes all `seen_video` rows, all marks and all read times, so each source gets a baseline. The videos that came between the last run and the change are then not evaluated.
- Mode `mark_date` keeps the mark of a channel in `channel.mark_date`, and the mark of the feed in the setting `feed_mark_date`. A source that was read before but has no mark uses the day of its last read as the mark.
- A flat date comes from relative text, and the text gets less exact with age, for example "1 week ago". An older entry can thus get a date on or after the mark. Mode `mark_date` therefore also stops at the first stored seen video of the mark day, because the upload order is stable. A deferred entry below that stop point gets no new check.

## Rules and Shorts

- A rule with `no_shorts` gets one more condition, "not `is_short`". A video with an unknown `is_short` thus cannot match it.
- A rule needs `is_short` when it has `no_shorts` or its condition reads `is_short`. The engine reads the Shorts of a channel at most once in a run, and only when such a rule gives UNKNOWN.
- A feed video without a channel id has no known `is_short`, so a rule with `no_shorts` defers it.

## Account cache

- `refresh_account_cache` in `account_cache.py` copies the subscriptions into `channel`. It copies the library playlists, with Watch Later as `WL`, into `playlist`.
- The refresh reads both lists before it writes, so a failed read changes nothing.
- The refresh writes `channel` in one transaction. It writes `playlist` and the setting `account_cache_refreshed_at` in a second transaction, so the time is stored only when both tables are written.
- The setting `account_cache_refreshed_at` holds the time of the last refresh as ISO text in UTC. `last_refresh` reads it.
- The refresh never deletes a row, because a rule can still point at the row by its id.
- A channel that left the subscriptions gets `is_subscribed` false and keeps its row, its `mark_date` and its `last_checked_at`.
- The refresh never writes `channel.mark_date` or `channel.last_checked_at`. The sync engine owns them.
- A playlist that left the library keeps its row and its old `playlist.last_synced_at`. A `last_synced_at` before `account_cache_refreshed_at` thus shows that the library lost the playlist, so the table needs no flag column. Only the refresh writes `last_synced_at`.
- An empty title from YouTube keeps the stored title. A new row with an empty title stores its id as the title.
- `is_cache_stale` treats a missing, unreadable or future refresh time as stale. A copy that is exactly `max_age` old is still fresh. The default `MAX_AGE` is 24 hours.
- `refresh_if_stale` calls `open_client` only for a stale copy, so a fresh copy needs no cookies and makes no request.
- Two refreshes at the same time can fail on a duplicate key. The caller holds the sync lock.
- The route `POST /api/account/refresh` runs `python -m youtube_automanager refresh-account` in a child process. It starts the child with `child_environment`, like a run, so the yt-dlp of the data folder applies and the web process never imports yt-dlp.
- The route takes the sync lock without a wait and holds it while the child runs. It answers 409 while a run or a yt-dlp update holds the lock.
- The child ends with exit code 0 and the counts as one JSON line at the end of stdout. The log of the child is on stdout too, so the parent reads the last line.
- A failed child ends with exit code 3 (account not available), 4 (logged out) or 5 (another account error), and the cause is the last line of stderr. Another exit code is a crash, and the route does not show its stderr.
- The route answers 409 for the exit codes 3 and 4. It answers 502 for the exit code 5, a crash, a timeout and a child without counts.
- The child gets `REFRESH_TIMEOUT_SECONDS`, 300 s, and `subprocess.run` kills it after that. This is shorter than the wait of a scheduled run for the sync lock.
- A stop of the service does not stop a refresh child. The child ends by itself, at the latest at its timeout.
- The route does not write `account.session_state`. Only the sync engine marks the session `dead`.
- The texts of `open_account` start with "The run needs the YouTube account", and the route gives them as they are.

## Processes

- Only the child of a run imports yt-dlp. `run_once` imports the engine inside the function, and a test makes sure that the parent never imports `yt_dlp`.
- The refresh of the account data follows the same rule: `refresh_once` imports the engine inside the function, and the web process only starts the child. A test makes sure that a refresh through the route leaves `yt_dlp` out of `sys.modules` of the web process.
