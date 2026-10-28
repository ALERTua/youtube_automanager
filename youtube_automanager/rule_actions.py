"""The rule actions of the rules page: the preview of a rule, and the apply of a saved rule to the videos since a date.

Both read YouTube through yt-dlp, so each runs in a child process, and the web process never imports yt-dlp:

- ``python -m youtube_automanager preview-rule [--since=<day>] [--rule-id=<id>] [--draft=<json>]`` previews a rule.
  With a day, it lists the videos since that day, and without a day, it tells what the next sync run does. It
  follows the protocol of ``short_child`` and holds the sync lock like each short child. A preview of a rule with
  channels reads them anonymously, and a preview that reads the subscriptions feed works with the account as a refresh
  does. A preview writes nothing.
- ``apply-rule --run-id=<id> --rule-id=<id> --since=<day>`` is the child of a run with the trigger ``rule_apply``. The
  runner writes the ``run`` row and its end as for a sync run, so the history shows each apply.

The engine of both is ``engine.rule_apply``. The result of a preview is a ``Preview`` of ``engine.rule_preview``.
"""

import datetime
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db import make_engine, make_session_factory
from youtube_automanager.db.models import Run, RunAction, RunActionKind, RunStatus, RunTrigger
from youtube_automanager.engine.rule_preview import DraftRule, Preview
from youtube_automanager.runner import BUSY_TEXT, SyncRunner
from youtube_automanager.settings import Settings
from youtube_automanager.short_child import ChildBusy, ChildFailed, read_json, run_child, succeed, work_with_account

LOG = logging.getLogger(__name__)

# A preview reads each source of the rule, up to five pages of one source, and the Shorts of a channel when a rule
# needs them. The timeout stays shorter than ``SCHEDULED_WAIT_S`` of the runner, so a preview never makes a scheduled
# run give up.
PREVIEW_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class AppliedRun:
    """The end of an apply: its run, the status of the run, the number of the added videos, the error text of the
    run, and the number of its ``error`` actions."""

    run_id: int
    status: RunStatus
    added_count: int
    error_text: str | None
    error_count: int


def preview_command(
    since: datetime.date | None, *, rule_id: int | None = None, draft: DraftRule | None = None
) -> list[str]:
    command = [sys.executable, "-m", "youtube_automanager", "preview-rule"]
    if since is not None:
        command.append(f"--since={since.isoformat()}")
    if rule_id is not None:
        command.append(f"--rule-id={rule_id}")
    if draft is not None:
        # The form with "=" keeps a draft that starts with "-" a value, not an option.
        command.append(f"--draft={draft.to_json()}")
    return command


def apply_command(run_id: int, rule_id: int, since: datetime.date) -> list[str]:
    return [
        sys.executable,
        "-m",
        "youtube_automanager",
        "apply-rule",
        f"--run-id={run_id}",
        f"--rule-id={rule_id}",
        f"--since={since.isoformat()}",
    ]


# The bodies of the children.


def preview_once(
    settings: Settings, since: datetime.date | None, rule_id: int | None = None, draft: DraftRule | None = None
) -> int:
    """The body of a preview in the child process. Return the exit code of the child.

    ``since`` is the first day of the listed videos, and None previews the next sync run. The child writes the
    ``Preview`` as one JSON line at the end of stderr. A rule that is gone or not valid gives a
    preview with only its error. A preview that reads the feed checks the session first, and a failed read of the feed
    ends the child with the exit code of its kind.
    """
    # The engine imports yt-dlp, so only the child loads it.
    from youtube_automanager.engine import rule_apply
    from youtube_automanager.engine.account_session import AccountUnavailable, load_stored
    from youtube_automanager.youtube import public

    def read_shorts(channel_id: str, limit: int) -> set[str]:
        return public.shorts_video_ids(channel_id, limit=limit)

    def preview(sessions: sessionmaker[Session], account: str | None = None, **feed: rule_apply.ReadFeed) -> Preview:
        if since is None:
            return rule_apply.next_run_preview(
                sessions,
                request,
                account=account,
                read_uploads=public.iter_channel_uploads,
                read_shorts=read_shorts,
                **feed,
            )
        return rule_apply.preview(
            sessions, request, since, read_uploads=public.iter_channel_uploads, read_shorts=read_shorts, **feed
        )

    engine = make_engine(settings.database_url)
    try:
        sessions = make_session_factory(engine)
        try:
            request = rule_apply.load_request(sessions, rule_id, draft)
        except rule_apply.RuleRefused as error:
            return succeed(Preview((), {}, errors=(str(error),)).to_value())
        if not request.reads_feed:
            # A run without usable cookies defers each match. The stored cookies tell it without a request to YouTube.
            try:
                load_stored(sessions, settings)
                account = None
            except AccountUnavailable as error:
                account = str(error)
            return succeed(preview(sessions, account).to_value())
    finally:
        engine.dispose()

    def work(sessions: sessionmaker[Session], client: rule_apply.AccountLike) -> dict:
        return preview(sessions, read_feed=client.iter_subscription_feed).to_value()

    return work_with_account(settings, "The rule preview", work)


def apply_once(run_id: int, rule_id: int, since: datetime.date, settings: Settings) -> int:
    """The body of an apply in the child process of the run ``run_id``. Return the exit code of the child.

    The parent owns the status of the ``run`` row, as for a sync run. The child writes the ``added_video`` rows, the
    ``run_action`` rows and the count of the added videos. When the apply cannot start or go on, the child writes the
    cause as the last line of its stderr, because the parent takes the error text of the run from there.
    """
    from youtube_automanager.engine.rule_apply import ApplyAborted, apply_with_youtube

    engine = make_engine(settings.database_url)
    try:
        sessions = make_session_factory(engine)
        with sessions() as session:
            if session.get(Run, run_id) is None:
                LOG.error("Run %s is not in the database", run_id)
                return 1
        try:
            apply_with_youtube(sessions, run_id, rule_id, since, settings)
        except ApplyAborted as error:
            LOG.error("Run %s stopped: %s", run_id, error)
            print(" ".join(str(error).split()), file=sys.stderr, flush=True)
            return 1
        return 0
    finally:
        engine.dispose()


# The parent.


def preview_in_child(
    runner: SyncRunner, since: datetime.date | None, *, rule_id: int | None = None, draft: DraftRule | None = None
) -> Preview:
    """Preview a rule in a child process of ``runner`` and wait for it. Raise a ``ChildError`` when it fails.

    ``rule_id`` alone previews the saved rule as an apply treats it, ``draft`` alone previews a new rule, and both
    preview the change of the saved rule to the draft. ``since`` is the first day of the videos, and None previews
    the next sync run. This call blocks.
    """
    if rule_id is None and draft is None:
        raise ValueError("a preview needs a saved rule, a draft or both")
    line = run_child(runner, preview_command(since, rule_id=rule_id, draft=draft), "preview", PREVIEW_TIMEOUT_SECONDS)
    preview = Preview.from_value(read_json(line))
    if preview is None:
        raise ChildFailed("The preview process ended without the result of the preview.")
    return preview


def apply_in_child(
    runner: SyncRunner,
    sessions: sessionmaker[Session],
    rule_id: int,
    since: datetime.date,
    on_claimed: Callable[[int], object] | None = None,
) -> AppliedRun:
    """Apply the saved rule ``rule_id`` to the videos since ``since`` as a run of ``runner``, and wait for its end.

    The run has the trigger ``rule_apply``. It does not wait for the sync lock: raise ``ChildBusy`` and write no run
    when a run, a session check, a short child or a yt-dlp update holds the lock. ``on_claimed`` gets the id of the run
    after the claim and before the start of the child, in the thread of this call. A failed ``on_claimed`` does not stop
    the apply, because only the end of the run releases the sync lock. This call blocks for the whole apply.
    """
    run_id = runner.claim(RunTrigger.RULE_APPLY)
    if run_id is None:
        raise ChildBusy(BUSY_TEXT)
    if on_claimed is not None:
        try:
            on_claimed(run_id)
        except Exception:
            LOG.exception("Run %s: the caller of the apply cannot learn the id of the run", run_id)
    runner.execute(run_id, apply_command(run_id, rule_id, since))
    applied = read_applied(sessions, run_id)
    if applied is None:
        raise ChildFailed(f"Run {run_id} of the apply is not in the database any more.")
    return applied


def read_applied(sessions: sessionmaker[Session], run_id: int) -> AppliedRun | None:
    """Read the state of the apply run ``run_id`` from its ``run`` row. Return None when the row is gone."""
    with sessions() as session:
        run = session.get(Run, run_id)
        if run is None:
            return None
        errors = session.scalar(
            select(func.count())
            .select_from(RunAction)
            .where(RunAction.run_id == run_id, RunAction.action == RunActionKind.ERROR)
        )
        return AppliedRun(run_id, run.status, run.added_count, run.error_text, errors or 0)
