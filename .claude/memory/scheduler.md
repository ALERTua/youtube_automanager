---
name: scheduler
description: "Read this file before you change the scheduler, or the way a sync job runs."
metadata:
  type: reference
---

- The scheduler runs in one process only. One persistent job store for two or more processes gives duplicate runs.
- Each sync runs in a child process, so that a yt-dlp update takes effect at the next run.
- In APScheduler 3.11, `AsyncIOScheduler` runs a synchronous job in the default thread pool of the event loop (`loop.run_in_executor(None, ...)`) and a coroutine job in the event loop. `reschedule_job`, `pause_job` and `resume_job` operate while the scheduler runs.
- In APScheduler 3.11, `AsyncIOScheduler.shutdown(wait=True)` does not wait for a job in a thread. The shutdown itself runs at the next turn of the event loop.
- In APScheduler 3.11, the shutdown of `AsyncIOExecutor` cancels each job that still runs, and the callback of the executor logs an ERROR for it: "Error running job" with `CancelledError` for a job in a thread, and "Job ... raised an exception" for a coroutine job. So the cleanup is a coroutine job of `Scheduler`, and `Scheduler.shutdown` waits for its task before it shuts APScheduler down. CI run 40684109956 failed on this: a test stopped the service while the first cleanup still ran. The run job, the yt-dlp update and a rotation turn have the same cut in a narrow window, and they are not waited for.
- At its exit, `asyncio.run` waits up to 300 s for the threads of the default thread pool, so an active run thread holds back the exit of uvicorn. Stop the child of the run before you stop the scheduler.
- In APScheduler 3.11, `reschedule_job` sets a new next run time, so it also resumes a paused job. To change the trigger of a paused job, use `modify_job(job_id, trigger=..., next_run_time=None)`.
- APScheduler 3.11 skips a run that starts more than `misfire_grace_time` late, 1 s by default, and it makes this check in the worker thread. A skipped one-off job never releases a lock that its caller took, so each job of the service has `misfire_grace_time=None`.
- In APScheduler 3.11, `CronTrigger.from_crontab` counts day of the week 0 as Monday, but standard cron counts 0 and 7 as Sunday. The service gives APScheduler the day names, which mean the same day in both.
- When a cron expression sets both the day of the month and the day of the week, standard cron runs on a day that matches one of them, but an APScheduler 3.11 `CronTrigger` runs only on a day that matches both. The service uses an `OrTrigger` of two cron triggers for such an expression.
- In APScheduler 3.11, `IntervalTrigger` changes an interval of 0 to 1 s without an error. Its first run time is its creation time plus the interval.
- In APScheduler 3.11 on Windows, an `IntervalTrigger` of 100000000000 s gives an `OSError` at its first run time, and one of 1000000000000 s gives an `OverflowError` at its creation. So `ScheduleSpec` refuses an interval above `MAX_INTERVAL_S`, one year. A schedule row that fails like this stops the start of the service.
- An interval schedule needs `MIN_INTERVAL_S`, 900 s, at least. The old code accepted 1 s, and a typo could make the service read each channel every few seconds from the home IP (outside review, PR #107). A stored shorter row from an older version must not stop the start, so the first `load_schedule` writes 900 s into the row and logs one WARNING.
- In APScheduler 3.11, `add_job` with an explicit `next_run_time` of now runs the job once at once, and then follows its trigger. The cleanup of the history uses this for its run at the start of the service.
- A cron expression for a day that never comes, for example `0 0 31 2 *`, passes `ScheduleSpec` and has no run time. The scheduler page refuses such a schedule, because the job would never run while the row says enabled.
- The child of a run imports the service from `SOURCE_ROOT` in `PYTHONPATH`, because the project is not installed as a package: `python -m` finds it only when the current folder is the repository root.
- Starlette 1.7 warns that its `TestClient` with `httpx` is deprecated, and it asks for `httpx2`.
- In APScheduler 4.0.0a6, the import of `apscheduler[sqlalchemy]` fails with `ModuleNotFoundError: No module named 'sniffio'` until you add `sniffio`. Its default executor is `async`, so a synchronous job runs in the event loop thread and blocks it. Give `job_executor="threadpool"` to such a job. Its README forbids production use.
- The service calls APScheduler only through its own async interface, so that a move to APScheduler 4 changes one module.
- In APScheduler 3.11, a job that plans its successor at the end of its run still counts as a running instance. With `max_instances=1`, a successor that is due at once gets skipped with "maximum number of running instances reached", and the chain ends. The cookie rotation is such a chain, so its job has `max_instances=2`.
- The cookie rotation, the session check on request and each sync run take the same sync lock, so only one of them works on the cookies at a time. The rotation skips its turn without a wait when the lock is taken.
- A scheduled run waits up to `SCHEDULED_WAIT_S` for the sync lock, because a rotation turn or a session check keeps the lock for seconds. Without the wait, a scheduled run that fires during a rotation turn is lost. A stop of the service ends the wait.
- The daily yt-dlp update replaces the folder that each child imports yt-dlp from, so it holds the sync lock through `SyncRunner.lock_held`. The scheduled update waits up to `SCHEDULED_WAIT_S`, as a scheduled run does, and "Update now" does not wait. A stop of the service ends a wait in `lock_held` and refuses the lock after it, so no update starts while the service stops.
- `ZoneInfo(name)` raises `OSError` for a name that is no valid file name, for example `*` or `Europe/Kyiv?` on Windows, or a name of 300 letters on each system. On Windows it also accepts `Europe/Kyiv ` and `Europe\Kyiv`, which a Linux host refuses. So `_zone` maps `OSError` to `ValueError`, and the HTTP API takes only a name of `zoneinfo.available_timezones()`, the list of the scheduler page.
