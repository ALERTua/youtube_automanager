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
- A source that failed in the run keeps all its `seen_video` rows.
- The clean-up of a sync run never deletes an `added_video` row. The daily cleanup in `retention.py` runs at 03:30 and at the start of the service. It deletes a row only when its `added_at` is older than `ADDED_VIDEO_KEEP_DAYS` (732) days and no source keeps its video in `seen_video`. A row whose video a source keeps stays at any age, because a sync run can reach that video again.

## The order of the work

- For each source, the engine first reads the whole window, then evaluates the entries from the oldest to the newest.
- Each add commits its `added_video` row at once. The `seen_video` row of a video comes only after all its adds.
- A failed add stops that source for the run. The video and the newer ones stay without a record, so the next run reads them again.
- A read error of a channel writes an `error` action, sends `channel_error`, and the run continues with the other sources.
- A dead or missing session does not stop the run. The run reads the channels and evaluates the rules. A matching video waits as deferred with the cause, so a later run with a live session adds it while the video stays in the processing window.
- Without the account, the feed source gets an error action and keeps its state. The run also gets one error that counts the matching videos that wait for the account.
- A `NotLoggedInError` during the run goes to `session_died`, and the run tries no more adds.
- When the run stops, the child writes the cause as the last line of stderr. The parent stores the end of stderr as the error text of the run.
- Each run with stored cookies first checks the session. The engine asks for the checked client only at the first need: a read of the feed or an add.
- With a live session, `run_with_youtube` then calls `refresh_if_stale` with the same `AccountSession`, before the sources. A refresh that YouTube refuses as logged out goes to `AccountSession.died`, so the run goes on without the account. An `AccountError` of the refresh writes a warning to the log, and an unexpected error writes its trace. Neither stops the run, because the refresh is a side step.

## Processing modes

- The setting `processing_mode` holds the mode: `{"mode": "stop_at_seen"}` (the default), `{"mode": "newest", "count": N}` or `{"mode": "mark_date"}`.
- Mode `stop_at_seen` stops when it has read at least 20 entries and met a seen entry. A deferred entry is never a stop point.
- After the 20 newest entries, mode `stop_at_seen` also stops at the first dated entry that is older than the day of the last read minus 2 days. A source whose stored videos all vanished thus reads only the recent days, not the whole channel. An entry without a date never stops the read.
- The change tracking of SQLAlchemy compares a JSON value with Python `==`, so a change of a `setting` value from `true` to `1` writes no UPDATE. Replace the row, or compare the types first.
- The setting `processing_mode_of_state` holds the mode of the stored state. When the mode changes, the engine removes all `seen_video` rows, all marks and all read times, so each source gets a baseline. The videos that came between the last run and the change are then not evaluated.
- Mode `mark_date` keeps the mark of a channel in `channel.mark_date`, and the mark of the feed in the setting `feed_mark_date`. A source that was read before but has no mark uses the day of its last read as the mark.
- A flat date comes from relative text, and the text gets less exact with age, for example "1 week ago". An older entry can thus get a date on or after the mark. Mode `mark_date` therefore also stops at the first stored seen video of the mark day, because the upload order is stable. A deferred entry below that stop point gets no new check.

## Rules and Shorts

- A rule with `no_shorts` gets one more condition, "not `is_short`". A video with an unknown `is_short` thus cannot match it.
- A rule needs `is_short` when it has `no_shorts` or its condition reads `is_short`. The engine reads the Shorts of a channel at most once in a run, and only when such a rule gives UNKNOWN.
- A feed video without a channel id has no known `is_short`, so a rule with `no_shorts` defers it.

## The preview and the apply of one rule

- `engine.rule_apply` reads the sources of one rule since a day and evaluates the rule as a sync run does, with the same Shorts check. The read of a source stops at the first dated entry before the day, or after `READ_LIMIT` (500) entries. An entry without a date never stops it.
- The preview writes nothing. It runs in the child `preview-rule` through `run_short_child`, and only a rule that reads the feed needs the account. The draft of the editor goes to the child as ASCII JSON in one argument `--draft=<json>`.
- The apply is a run with the trigger `rule_apply`: `apply_in_child` calls `SyncRunner.claim` without a wait and `SyncRunner.execute` with the command of the child `apply-rule`. It writes `added_video` rows and `added` actions, but never `seen_video`, a mark or a read time. It adds at most `APPLY_LIMIT` (100) videos, the oldest first, and a failed add stops it.
- An add sends two requests with the cookies, a read of the page config and the change, 5 s or more apart. An apply of 100 videos thus takes more than 1000 s. `SCHEDULED_WAIT_S` is 1800 s, so a scheduled run that comes during such an apply starts late, but it is not lost.
- An apply reads back to the day that the user chose, at most `MAX_DAYS_BACK` (365) days. It thus reaches a video also after the sync removes its `seen_video` rows. The `added_video` row is the only record that stops a second add, so the row must stay longer than the state of the sync. `ADDED_VIDEO_KEEP_DAYS` must stay greater than `MAX_DAYS_BACK`, and a test makes sure of it.
- yt-dlp gives a flat entry with the text "1 year ago" the date 12 months back, rounded to the nearest UTC midnight, 364 to 366 days back. The text can name a video that is almost 2 years old, because a relative age counts whole years, so an apply from the earliest day can reach videos up to 2 years old. "2 years ago" gets 729 days back, out of the reach of an apply. So `ADDED_VIDEO_KEEP_DAYS` is 732, more than 2 years, and a test makes sure that it stays above 730.
- `engine.rule_preview` holds the limits, the draft and the result of a preview, and it imports no yt-dlp, so the web process uses it. `engine.rule_apply` imports yt-dlp through `engine.sync`, so only a child imports it.

## Account cache

- `refresh_account_cache` in `account_cache.py` copies the subscriptions into `channel`. It copies the library playlists, with Watch Later as `WL`, into `playlist`.
- The refresh reads both lists before it writes, so a failed read changes nothing.
- The refresh writes `channel`, `playlist` and the setting `account_cache_refreshed_at` in one transaction, so a failed write changes nothing.
- The setting `account_cache_refreshed_at` holds the time of the last refresh as ISO text in UTC. `last_refresh` reads it.
- The refresh never deletes a row, because a rule can still point at the row by its id.
- A channel that left the subscriptions gets `is_subscribed` false and keeps its row, its `mark_date` and its `last_checked_at`.
- The refresh never writes `channel.mark_date` or `channel.last_checked_at`. The sync engine owns them.
- A playlist that left the library keeps its row and its old `playlist.last_synced_at`. A `last_synced_at` before `account_cache_refreshed_at` thus shows that the library lost the playlist, so the table needs no flag column. Only the refresh writes `last_synced_at`.
- An empty title from YouTube keeps the stored title. A new row with an empty title stores its id as the title.
- `is_cache_stale` treats a missing, unreadable or future refresh time as stale. A copy that is exactly `max_age` old is still fresh. The default `MAX_AGE` is 20 hours: a limit of 24 hours let a daily run find the copy of the run before a few seconds too young, so the copy refreshed only every second day.
- `refresh_if_stale` calls `open_client` only for a stale copy, so a fresh copy needs no cookies and makes no request.
- Two refreshes at the same time can fail on a duplicate key. The caller holds the sync lock.
- The route `POST /api/account/refresh` calls `refresh_in_child`, which runs `python -m youtube_automanager refresh-account` through `SyncRunner.run_short_child`. This is the same child body that a run and a session check use, so the yt-dlp of the data folder applies, the web process never imports yt-dlp, and `SyncRunner.stop` stops the child.
- `run_short_child` takes the sync lock without a wait and holds it while the child runs. The route answers 409 with `BUSY_TEXT` while a run, a session check, another refresh or a yt-dlp update holds the lock.
- The refresh child works with the account as a run does: it checks the session, reads the lists, and saves the updated cookies with `AccountSession`. A read that YouTube refuses as logged out marks the session `dead` through `AccountSession.died`.
- The child ends with exit code 0 and the counts as one JSON line at the end of stderr. The runner copies and keeps only the stderr of a child, so the counts come on that stream. The log of the child is on stdout.
- A failed child writes its cause as the last line of stderr and ends with a code. `UNAVAILABLE_EXIT_CODE` (3) is for no usable cookies, and a session check child uses the same code. `EXIT_LOGGED_OUT` (4) and `EXIT_YOUTUBE_ERROR` (5) of `short_child` are for each short child. Another exit code is a crash, and the route does not show its stderr.
- `short_child` holds the protocol of each short child: the exit codes, the errors, `work_with_account` for the body of a child that works with the account, and `run_child` for the parent. The refresh, the channel search (`find-channels`) and the subscription change (`subscribe`, `unsubscribe`) follow it.
- The runner keeps the end of stderr, at most `ERROR_TEXT_MAX_CHARS` characters, as `error_text`, and the last stderr line in full as `last_line`. A short child reads its result from `last_line`, because a name search of 20 channels gives a result line of more than 5000 characters.
- The route answers 409 for the codes 3 and 4. It answers 502 for the code 5, a crash, a stop of the service, a timeout and a child without counts.
- The child gets `REFRESH_TIMEOUT_SECONDS`, 300 s. This is shorter than `SCHEDULED_WAIT_S`, the wait of a scheduled run for the sync lock, so a refresh never makes a scheduled run give up.

## Processes

- Only the child of a run imports yt-dlp. `run_once` imports the engine inside the function, and a test makes sure that the parent never imports `yt_dlp`.
- The refresh of the account data follows the same rule: `refresh_once` imports the engine inside the function, and the web process only starts the child. A test makes sure that a refresh through the route leaves `yt_dlp` out of `sys.modules` of the web process.
- The channel search and the subscription change follow the same rule, also the anonymous search: each runs in a child through `run_short_child` and holds the sync lock. A test makes sure that the subscriptions page, a search, a subscribe and an unsubscribe leave `yt_dlp` out of `sys.modules` of the web process.
