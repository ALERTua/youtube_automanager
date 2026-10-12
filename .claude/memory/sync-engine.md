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
- A dead or missing session does not stop the run. The run reads the channels and evaluates the rules. A matching video waits as deferred with the cause, so a later run with a live session adds it while the video stays in the processing window.
- Without the account, the feed source gets an error action and keeps its state. The run also gets one error that counts the matching videos that wait for the account.
- A `NotLoggedInError` during the run goes to `session_died`, and the run tries no more adds.
- When the run stops, the child writes the cause as the last line of stderr. The parent stores the end of stderr as the error text of the run.
- Each run with stored cookies first checks the session. The engine asks for the checked client only at the first need: a read of the feed or an add.

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

## Processes

- Only the child of a run imports yt-dlp. `run_once` imports the engine inside the function, and a test makes sure that the parent never imports `yt_dlp`.
