---
name: scheduler
description: "Read this file before you change the scheduler, or the way a sync job runs."
metadata:
  type: reference
---

- The scheduler runs in one process only. One persistent job store for two or more processes gives duplicate runs.
- Each sync runs in a child process, so that a yt-dlp update takes effect at the next run.
- In APScheduler 3.11, `AsyncIOScheduler` runs a synchronous job in its thread pool and a coroutine job in the event loop. `reschedule_job`, `pause_job` and `resume_job` operate while the scheduler runs.
- In APScheduler 4.0.0a6, the import of `apscheduler[sqlalchemy]` fails with `ModuleNotFoundError: No module named 'sniffio'` until you add `sniffio`. Its default executor is `async`, so a synchronous job runs in the event loop thread and blocks it. Give `job_executor="threadpool"` to such a job. Its README forbids production use.
- The service calls APScheduler only through its own async interface, so that a move to APScheduler 4 changes one module.
