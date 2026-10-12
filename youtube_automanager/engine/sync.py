"""One sync run: read the sources of the enabled rules, add each matching video to its target playlist once, and keep
the state and the history in the database.

Each channel of an enabled rule is a source, read anonymously through its uploads playlist. The rules without channels
share one more source: the subscriptions feed of the account, read with the cookies. The state of a source lives in
``seen_video`` under its channel id, or under FEED_SOURCE for the feed, so a video that a channel already showed is
still new to a feed rule.

For each source, the run first collects the entries to evaluate, then evaluates them from the oldest to the newest, and
commits the state of each video after its adds. A video becomes ``seen`` only after each of its adds succeeded. A
failure in the middle thus leaves that video and each newer one without a record, and the next run reads them again.

The first read of a source stores its current videos as seen, without an evaluation and without an add, so a new
rule, a changed rule and a changed processing mode act only on new videos. Each source that a run read in full gets
the time of that read: ``channel.last_checked_at`` for a channel, and the setting FEED_READ_KEY for the feed. A source
with that time was read before, so it never gets a second baseline, also when it has no stored video.

Without a live session of the account, the run still reads the channels and evaluates the rules, but adds nothing. A
matching video waits as deferred, so a later run with a live session adds it while the video stays in the processing
window. The feed needs the account, so it gets an error in such a run.
"""

import datetime
import logging
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager import account_cache, notify
from youtube_automanager.db.models import (
    AddedVideo,
    Channel,
    Playlist,
    Rule,
    RuleChannel,
    Run,
    RunAction,
    RunActionKind,
    SeenVideo,
    SeenVideoState,
    Setting,
    utc_now,
)
from youtube_automanager.engine.account_session import (
    DEAD_SESSION_TEXT,
    AccountSession,
    AccountUnavailable,
    CheckOutcome,
)
from youtube_automanager.engine.modes import (
    Collected,
    Known,
    Processing,
    ProcessingMode,
    Video,
    baseline,
    collect,
    has_state,
    ids_to_keep,
    moved_mark,
)
from youtube_automanager.engine.rules import RuleError, Verdict, evaluate, fields_read, validate
from youtube_automanager.settings import Settings
from youtube_automanager.youtube import public
from youtube_automanager.youtube.account import AccountError, NotLoggedInError
from youtube_automanager.youtube.public import PublicSourceError

LOG = logging.getLogger(__name__)

# The source key of the subscriptions feed in ``seen_video.channel_id``. A channel id always starts with "UC".
FEED_SOURCE = "feed"
FEED_TITLE = "the subscriptions feed"
# The run reads at most this many of the newest Shorts of a channel, once per run.
SHORTS_LIMIT = 100
# The keys of the ``setting`` table that the engine reads and writes.
PROCESSING_MODE_KEY = "processing_mode"
STATE_MODE_KEY = "processing_mode_of_state"
FEED_MARK_KEY = "feed_mark_date"
FEED_READ_KEY = "feed_last_read_at"
# The largest number of values in one SQL IN list.
_IN_CHUNK = 500


class SyncAborted(Exception):
    """The run cannot go on. The text names the cause and what the user can do."""


class _SourceStopped(Exception):
    """The source cannot go on in this run. The history already has the error."""


class _NoAccount(Exception):
    """The add needs the account, but the run goes on without it. The text names the cause."""


class AccountLike(Protocol):
    """The part of ``AccountClient`` that a run uses."""

    def iter_subscription_feed(self) -> Iterable[Any]: ...

    def add_video(self, playlist_id: str, video_id: str) -> None: ...


type ReadUploads = Callable[[str], Iterable[Any]]
type ReadShorts = Callable[[str, int], Iterable[str]]
type OpenAccount = Callable[[], AccountLike]
type SessionDied = Callable[[NotLoggedInError], object]
type Send = Callable[[notify.Notification], object]


@dataclass(frozen=True)
class RuleSpec:
    """An enabled rule, ready for evaluation. ``tree`` holds the Shorts check of a rule with ``no_shorts``."""

    id: int
    name: str
    tree: Any
    playlist_id: str
    needs_is_short: bool


@dataclass(frozen=True)
class Source:
    key: str
    title: str
    rules: tuple[RuleSpec, ...]

    @property
    def is_feed(self) -> bool:
        return self.key == FEED_SOURCE


@dataclass(frozen=True)
class _Outcome:
    """A source that the run read in full: its processing window and its mark after the run."""

    read: tuple[Video, ...]
    mark: datetime.date | None


@dataclass
class RunReport:
    added: list[notify.AddedVideo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    seen_count: int = 0


def _chunks(values: Iterable[str]) -> Iterator[list[str]]:
    items = sorted(values)
    for start in range(0, len(items), _IN_CHUNK):
        yield items[start : start + _IN_CHUNK]


def rule_spec(rule: Rule) -> RuleSpec:
    """Build the evaluation form of ``rule``. Raise RuleError when its condition is not valid.

    A rule with ``no_shorts`` gets one more condition, "the video is not a Short", so a Short never matches it, and a
    video with an unknown ``is_short`` cannot match it either.
    """
    tree = rule.condition
    validate(tree)
    if rule.no_shorts:
        tree = {"and": [tree, {"!": [{"var": "is_short"}]}]}
    return RuleSpec(
        id=rule.id,
        name=rule.name,
        tree=tree,
        playlist_id=rule.target_playlist_id,
        needs_is_short=rule.no_shorts or "is_short" in fields_read(rule.condition),
    )


def load_processing(session: Session) -> Processing:
    """Return the processing mode of the service. Without the setting, the mode is "stop at seen"."""
    row = session.get(Setting, PROCESSING_MODE_KEY)
    if row is None:
        return Processing()
    try:
        return Processing.from_value(row.value)
    except ValueError as error:
        raise SyncAborted(f"The setting {PROCESSING_MODE_KEY!r} is not valid: {error}") from None


class SyncRun:
    """One run of the sync. ``run`` does the whole run once.

    ``read_uploads`` and ``read_shorts`` read public channel data. ``open_account`` gives the account client, and the
    run calls it only when a rule needs the account: to read the feed, or to add a video. It raises AccountUnavailable
    when the run must go on without the account. ``session_died`` takes a session that YouTube refused during the run.
    ``send`` sends a notification and never raises.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        run_id: int,
        *,
        read_uploads: ReadUploads,
        read_shorts: ReadShorts,
        open_account: OpenAccount,
        send: Send,
        session_died: SessionDied | None = None,
        clock: Callable[[], datetime.datetime] = utc_now,
    ) -> None:
        self._sessions = sessions
        self._run_id = run_id
        self._read_uploads = read_uploads
        self._read_shorts = read_shorts
        self._open_account = open_account
        self._session_died = session_died
        self._send = send
        self._clock = clock
        self._account: AccountLike | None = None
        # The cause when the run goes on without the account, and the number of matching videos that wait for it.
        self._no_account: str | None = None
        self._waiting = 0
        self._shorts: dict[str, frozenset[str] | None] = {}
        self._channel_titles: dict[str, str] = {}
        self._playlist_titles: dict[str, str] = {}
        self.report = RunReport()

    def run(self) -> RunReport:
        processing, sources = self._prepare()
        outcomes: dict[str, _Outcome | None] = {}
        for source in sources:
            outcomes[source.key] = self._sync_source(processing, source)
        self._clean_up(processing, [source.key for source in sources], outcomes)
        if self._waiting:
            self._error(f"{self._waiting} matching video(s) wait for the YouTube account: {self._no_account}")
        LOG.info(
            "Run %s finished: %d video(s) added, %d video(s) seen, %d error(s)",
            self._run_id,
            len(self.report.added),
            self.report.seen_count,
            len(self.report.errors),
        )
        self._notify(notify.run_summary(self.report.added, self.report.errors))
        return self.report

    # The preparation of the run.

    def _prepare(self) -> tuple[Processing, list[Source]]:
        with self._sessions.begin() as session:
            processing = load_processing(session)
            self._apply_mode(session, processing)
            rules = session.scalars(select(Rule).where(Rule.enabled).order_by(Rule.position, Rule.id)).all()
            links = session.execute(
                select(RuleChannel.rule_id, RuleChannel.channel_id).where(
                    RuleChannel.rule_id.in_([rule.id for rule in rules])
                )
            ).all()
            self._channel_titles = {row.channel_id: row.title for row in session.scalars(select(Channel))}
            self._playlist_titles = {row.playlist_id: row.title for row in session.scalars(select(Playlist))}
        channels_of: dict[int, set[str]] = {}
        for rule_id, channel_id in links:
            channels_of.setdefault(rule_id, set()).add(channel_id)

        by_channel: dict[str, list[RuleSpec]] = {}
        feed_rules: list[RuleSpec] = []
        for rule in rules:
            try:
                spec = rule_spec(rule)
            except RuleError as error:
                self._error(f"Rule {rule.name}: {error}", rule_id=rule.id)
                continue
            channels = channels_of.get(rule.id)
            if channels:
                for channel_id in channels:
                    by_channel.setdefault(channel_id, []).append(spec)
            else:
                feed_rules.append(spec)
        sources = [
            Source(channel_id, self._channel_titles.get(channel_id, channel_id), tuple(specs))
            for channel_id, specs in sorted(by_channel.items())
        ]
        if feed_rules:
            sources.append(Source(FEED_SOURCE, FEED_TITLE, tuple(feed_rules)))
        LOG.info("Run %s: %d source(s), processing mode %s", self._run_id, len(sources), processing.describe())
        return processing, sources

    def _apply_mode(self, session: Session, processing: Processing) -> None:
        """Record the mode of the stored state. A new mode removes the state of each source: its videos, its mark and
        the time of its last read.

        The state of one mode does not tell another mode which videos are new, so each source starts again with a
        baseline. The ``added_video`` rows stay.
        """
        value = processing.to_value()
        row = session.get(Setting, STATE_MODE_KEY)
        if row is None:
            session.add(Setting(key=STATE_MODE_KEY, value=value))
            return
        if row.value == value:
            return
        LOG.warning(
            "The processing mode changed from %s to %s, so each source starts with a baseline",
            row.value,
            processing.describe(),
        )
        session.execute(delete(SeenVideo))
        session.execute(update(Channel).values(mark_date=None, last_checked_at=None))
        session.execute(delete(Setting).where(Setting.key.in_([FEED_MARK_KEY, FEED_READ_KEY])))
        row.value = value

    # One source.

    def _sync_source(self, processing: Processing, source: Source) -> _Outcome | None:
        """Sync one source. Return None when the source failed, so the clean-up leaves its state as it is.

        A source that stops at a failed add keeps the time of its last full read. A newer time would let the next run
        stop above the videos that this run left without a record.
        """
        known, mark, read_at = self._load_state(source)
        read_before = None if read_at is None else read_at.date()
        starts = not has_state(processing, known, mark, read_before)
        try:
            videos = (Video.from_entry(entry) for entry in self._listing(source))
            if starts:
                collected = Collected(read=baseline(processing, videos), todo=())
            else:
                collected = collect(processing, videos, known, mark, read_before)
        except NotLoggedInError as error:
            self._source_error(source, self._lose_session(error))
            return None
        except (PublicSourceError, AccountError, AccountUnavailable) as error:
            self._source_error(source, str(error))
            return None

        if starts:
            recorded = collected.read
            LOG.info("Run %s: %s starts with a baseline of %d video(s)", self._run_id, source.title, len(recorded))
        else:
            recorded = []
            try:
                # The oldest first, so a failure leaves only newer videos without a record.
                for video in reversed(collected.todo):
                    self._process(source, video, known.get(video.video_id))
                    recorded.append(video)
            except _SourceStopped:
                return None
        new_mark = moved_mark(mark, recorded) if processing.mode is ProcessingMode.MARK_DATE else mark
        self._finish_source(source, collected.read if starts else (), mark=new_mark)
        return _Outcome(read=collected.read, mark=new_mark)

    def _listing(self, source: Source) -> Iterable[Any]:
        if source.is_feed:
            return self._account_client().iter_subscription_feed()
        try:
            return self._read_uploads(source.key)
        except ValueError as error:
            raise PublicSourceError(str(error)) from error

    def _load_state(self, source: Source) -> tuple[dict[str, Known], datetime.date | None, datetime.datetime | None]:
        """Return the stored videos of the source, its mark, and the time of its last full read."""
        with self._sessions() as session:
            known = self._known(session, source.key)
            if source.is_feed:
                mark_row = session.get(Setting, FEED_MARK_KEY)
                read_row = session.get(Setting, FEED_READ_KEY)
                mark = datetime.date.fromisoformat(mark_row.value) if mark_row is not None and mark_row.value else None
                read_at = (
                    datetime.datetime.fromisoformat(read_row.value) if read_row is not None and read_row.value else None
                )
                return known, mark, read_at
            channel = session.get(Channel, source.key)
            if channel is None:
                return known, None, None
            return known, channel.mark_date, channel.last_checked_at

    @staticmethod
    def _known(session: Session, key: str) -> dict[str, Known]:
        rows = session.execute(
            select(SeenVideo.video_id, SeenVideo.state, SeenVideo.upload_date).where(SeenVideo.channel_id == key)
        )
        return {video_id: Known(state, upload_date) for video_id, state, upload_date in rows}

    def _finish_source(self, source: Source, baseline_videos: Sequence[Video], *, mark: datetime.date | None) -> None:
        """Write the end of a full read in one transaction: the videos of a baseline, the mark and the read time.

        The read time is also written for a read that gave no entries.
        """
        now = self._clock()
        with self._sessions.begin() as session:
            if baseline_videos:
                self._record_baseline(session, source, baseline_videos, now)
            if source.is_feed:
                if mark is not None:
                    self._set(session, FEED_MARK_KEY, mark.isoformat())
                self._set(session, FEED_READ_KEY, now.isoformat())
                return
            channel = session.get(Channel, source.key)
            if channel is not None:
                channel.mark_date = mark
                channel.last_checked_at = now

    @staticmethod
    def _set(session: Session, key: str, value: Any) -> None:
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value

    # One video.

    def _process(self, source: Source, video: Video, known: Known | None) -> None:
        verdicts = self._verdicts(source, video)
        targets: dict[str, RuleSpec] = {}
        for rule, verdict in verdicts:
            if verdict is Verdict.MATCH:
                # Several rules with the same target add the video once, for the first rule.
                targets.setdefault(rule.playlist_id, rule)
        waits_for: str | None = None
        for playlist_id, rule in targets.items():
            try:
                self._add(video, rule, playlist_id, first_time=known is None)
            except _NoAccount as error:
                waits_for = str(error)
                break
        undecided = [rule for rule, verdict in verdicts if verdict is Verdict.UNKNOWN]
        if waits_for is None and video.is_finished and not undecided:
            self._record(source, video, SeenVideoState.SEEN)
            return
        if waits_for is not None:
            self._waiting += 1
            reason = f"It matches, but it waits for the YouTube account. {waits_for}"
        elif undecided:
            reason = f"No value yet for a field of rule {', '.join(rule.name for rule in undecided)}"
        else:
            reason = f"The video is {video.live_status}"
        # A deferred video gets one history entry, when it is deferred for the first time.
        action = None
        if known is None:
            action = RunAction(
                run_id=self._run_id,
                at=self._clock(),
                action=RunActionKind.DEFERRED,
                video_id=video.video_id,
                channel_id=video.channel_id,
                rule_id=undecided[0].id if undecided else None,
                detail=f"{video.title}: {reason}",
            )
        self._record(source, video, SeenVideoState.DEFERRED, action)

    def _verdicts(self, source: Source, video: Video) -> list[tuple[RuleSpec, Verdict]]:
        """Evaluate each rule of the source. A rule that needs ``is_short`` reads the Shorts only when it must."""
        fields = video.fields()
        verdicts = []
        for rule in source.rules:
            verdict = self._evaluate(rule, fields, video)
            if verdict is Verdict.UNKNOWN and rule.needs_is_short and fields["is_short"] is None:
                is_short = self._is_short(video)
                if is_short is not None:
                    fields = video.fields(is_short)
                    verdict = self._evaluate(rule, fields, video)
            verdicts.append((rule, verdict))
        return verdicts

    def _evaluate(self, rule: RuleSpec, fields: dict[str, Any], video: Video) -> Verdict:
        try:
            return evaluate(rule.tree, fields)
        except RuleError as error:
            self._error(f"Rule {rule.name}, video {video.video_id}: {error}", rule_id=rule.id, video=video)
            return Verdict.UNKNOWN

    def _is_short(self, video: Video) -> bool | None:
        """Tell whether the video is a Short. None when the video has no channel, or its Shorts cannot be read."""
        channel_id = video.channel_id
        if channel_id is None:
            return None
        if channel_id not in self._shorts:
            try:
                self._shorts[channel_id] = frozenset(self._read_shorts(channel_id, SHORTS_LIMIT))
            except (PublicSourceError, ValueError) as error:
                self._shorts[channel_id] = None
                title = self._channel_titles.get(channel_id, channel_id)
                self._channel_error(title, f"Cannot read the Shorts of the channel: {error}", channel_id)
        ids = self._shorts[channel_id]
        return None if ids is None else video.video_id in ids

    def _add(self, video: Video, rule: RuleSpec, playlist_id: str, *, first_time: bool) -> None:
        """Add the video to the playlist, unless the service added it there before.

        A video that the user removed from the playlist by hand is not added again. Raise _NoAccount when the run goes
        on without the account.
        """
        playlist_title = self._playlist_titles.get(playlist_id, playlist_id)
        with self._sessions() as session:
            added_before = session.get(AddedVideo, (playlist_id, video.video_id)) is not None
        if added_before:
            if first_time:
                self._action(
                    RunActionKind.SKIPPED,
                    video=video,
                    rule_id=rule.id,
                    playlist_id=playlist_id,
                    detail=f"{video.title}: the service added it to {playlist_title} before",
                )
            return
        try:
            self._account_client().add_video(playlist_id, video.video_id)
        except NotLoggedInError as error:
            raise _NoAccount(self._lose_session(error)) from error
        except AccountUnavailable as error:
            raise _NoAccount(str(error)) from error
        except (AccountError, ValueError) as error:
            self._error(
                f"Cannot add video {video.video_id} to {playlist_title}: {error}",
                rule_id=rule.id,
                playlist_id=playlist_id,
                video=video,
            )
            raise _SourceStopped from error
        now = self._clock()
        with self._sessions.begin() as session:
            session.add(AddedVideo(playlist_id=playlist_id, video_id=video.video_id, rule_id=rule.id, added_at=now))
            session.add(
                RunAction(
                    run_id=self._run_id,
                    at=now,
                    action=RunActionKind.ADDED,
                    video_id=video.video_id,
                    channel_id=video.channel_id,
                    rule_id=rule.id,
                    playlist_id=playlist_id,
                    detail=video.title,
                )
            )
            session.execute(update(Run).where(Run.id == self._run_id).values(added_count=Run.added_count + 1))
        self.report.added.append(notify.AddedVideo(video.title, playlist_title, video.url))
        LOG.info("Run %s: added %s (%s) to %s", self._run_id, video.video_id, video.title, playlist_title)

    def _record(self, source: Source, video: Video, state: SeenVideoState, action: RunAction | None = None) -> None:
        with self._sessions.begin() as session:
            self._store(session, source, video, state, self._clock())
            if action is not None:
                session.add(action)
            session.execute(update(Run).where(Run.id == self._run_id).values(seen_count=Run.seen_count + 1))
        self.report.seen_count += 1

    def _record_baseline(
        self, session: Session, source: Source, videos: Sequence[Video], now: datetime.datetime
    ) -> None:
        """Store the videos of a first read without an evaluation. An upcoming or live entry waits as deferred."""
        for video in videos:
            state = SeenVideoState.SEEN if video.is_finished else SeenVideoState.DEFERRED
            self._store(session, source, video, state, now)
        session.execute(update(Run).where(Run.id == self._run_id).values(seen_count=Run.seen_count + len(videos)))
        self.report.seen_count += len(videos)

    @staticmethod
    def _store(session: Session, source: Source, video: Video, state: SeenVideoState, now: datetime.datetime) -> None:
        row = session.get(SeenVideo, (source.key, video.video_id))
        if row is None:
            session.add(
                SeenVideo(
                    channel_id=source.key,
                    video_id=video.video_id,
                    state=state,
                    upload_date=video.upload_date,
                    first_seen_at=now,
                    last_checked_at=now,
                )
            )
            return
        row.state = state
        row.upload_date = video.upload_date
        # A check that changes no other column must still move the check time.
        row.last_checked_at = now

    # The account.

    def _account_client(self) -> AccountLike:
        """Return the account client. Raise AccountUnavailable when the run goes on without the account."""
        if self._no_account is not None:
            raise AccountUnavailable(self._no_account)
        if self._account is None:
            try:
                self._account = self._open_account()
            except AccountUnavailable as error:
                self._no_account = str(error)
                raise
        return self._account

    def _lose_session(self, error: NotLoggedInError) -> str:
        """Take a session that YouTube refused during the run. The rest of the run goes on without the account.

        Return the cause for the history.
        """
        LOG.warning("Run %s: YouTube refused the session: %s", self._run_id, error)
        if self._session_died is not None:
            self._session_died(error)
        self._no_account = DEAD_SESSION_TEXT
        return self._no_account

    # The history and the notifications.

    def _action(
        self,
        kind: RunActionKind,
        *,
        video: Video | None = None,
        channel_id: str | None = None,
        rule_id: int | None = None,
        playlist_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self._sessions.begin() as session:
            session.add(
                RunAction(
                    run_id=self._run_id,
                    at=self._clock(),
                    action=kind,
                    video_id=video.video_id if video is not None else None,
                    channel_id=video.channel_id if video is not None else channel_id,
                    rule_id=rule_id,
                    playlist_id=playlist_id,
                    detail=detail,
                )
            )

    def _error(self, text: str, **where: Any) -> None:
        LOG.warning("Run %s: %s", self._run_id, text)
        self.report.errors.append(text)
        self._action(RunActionKind.ERROR, detail=text, **where)

    def _source_error(self, source: Source, error_text: str) -> None:
        if source.is_feed:
            self._error(f"Cannot read {FEED_TITLE}: {error_text}")
        else:
            self._channel_error(source.title, error_text, source.key)

    def _channel_error(self, title: str, error_text: str, channel_id: str) -> None:
        self._error(f"Channel {title}: {error_text}", channel_id=channel_id)
        self._notify(notify.channel_error(title, error_text))

    def _notify(self, notification: notify.Notification) -> None:
        try:
            self._send(notification)
        except Exception as error:  # noqa: BLE001
            # A failed notification must never fail the run.
            LOG.warning("Run %s: cannot send %s: %s", self._run_id, notification.event.value, type(error).__name__)

    # The clean-up of the state.

    def _clean_up(self, processing: Processing, keys: Sequence[str], outcomes: dict[str, _Outcome | None]) -> None:
        """Remove the stored state that no later run can reach.

        A source outside this run loses its whole state, also its mark and the time of its last read, so a rule that
        reads it again starts with a baseline. A source that the run read in full keeps what its mode can still reach.
        A source that failed keeps its state as it is.

        The ``added_video`` rows stay, because an apply of a rule reaches a video also after the sync removed its
        state. The daily cleanup in ``retention`` deletes an old row whose video no source keeps.
        """
        with self._sessions.begin() as session:
            session.execute(delete(SeenVideo).where(SeenVideo.channel_id.not_in(keys)))
            session.execute(
                update(Channel)
                .where(
                    or_(Channel.mark_date.is_not(None), Channel.last_checked_at.is_not(None)),
                    Channel.channel_id.not_in(keys),
                )
                .values(mark_date=None, last_checked_at=None)
            )
            if FEED_SOURCE not in keys:
                session.execute(delete(Setting).where(Setting.key.in_([FEED_MARK_KEY, FEED_READ_KEY])))

            for key, outcome in outcomes.items():
                if outcome is None:
                    continue
                known = self._known(session, key)
                gone = set(known) - ids_to_keep(processing, outcome.read, known, outcome.mark)
                for chunk in _chunks(gone):
                    session.execute(delete(SeenVideo).where(SeenVideo.channel_id == key, SeenVideo.video_id.in_(chunk)))


def run_with_youtube(sessions: sessionmaker[Session], run_id: int, settings: Settings) -> RunReport:
    """Do the run ``run_id`` with the real YouTube sources and the notification targets of the database.

    The run first checks the session of the stored cookies. With a live session, it then refreshes the stored copy of
    the subscriptions and the playlists when that copy is stale. At its end, the run saves the updated cookies.
    """

    def send(notification: notify.Notification) -> object:
        return notify.send_to_stored_targets(sessions, notification)

    account = AccountSession(sessions, settings, send=send)
    result = account.check()
    LOG.info("Run %s: the session check gave %s", run_id, result.outcome.value)
    try:
        if result.outcome is CheckOutcome.ALIVE:
            _refresh_account_data(sessions, account, run_id)
        return SyncRun(
            sessions,
            run_id,
            read_uploads=public.iter_channel_uploads,
            read_shorts=lambda channel_id, limit: public.shorts_video_ids(channel_id, limit=limit),
            open_account=account.open,
            session_died=account.died,
            send=send,
        ).run()
    finally:
        account.write_back()


def _refresh_account_data(sessions: sessionmaker[Session], account: AccountSession, run_id: int) -> None:
    """Refresh the stored copy of the account data when it is stale. A failed refresh does not stop the run.

    A refresh that YouTube refuses as logged out marks the session dead, so the run goes on without the account. An
    unexpected error writes its trace to the log, because the refresh is a side step and must not cost the adds of the
    run.
    """
    try:
        refreshed = account_cache.refresh_if_stale(sessions, account.open)
    except NotLoggedInError as error:
        account.died(error)
        return
    except AccountError as error:
        LOG.warning("Run %s: the account data did not refresh: %s", run_id, error)
        return
    except Exception:
        LOG.exception("Run %s: the refresh of the account data failed", run_id)
        return
    if refreshed is not None:
        LOG.info("Run %s: refreshed the stored subscriptions and playlists of the account", run_id)
