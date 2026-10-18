"""The read and the evaluation of one rule since a date: the preview of a rule, and the apply of a saved rule.

The sources of a rule are its channels, read anonymously through their uploads, or without channels the subscriptions
feed of the account. The read takes each source newest first, and it stops at the first dated entry that is older than
the date, or after READ_LIMIT entries. An entry without a date, for example an upcoming one, never stops the read.

The evaluation is the evaluation of a sync run: a rule with ``no_shorts`` gets the Shorts check of ``rule_spec``, and a
rule that needs ``is_short`` reads the Shorts of a channel at most once, and only when it gives UNKNOWN.

The preview of the next run reads each source as the next sync run does, with the processing mode and the stored
state, and it lists the videos that the run evaluates. A rule with a catch-up day also lists the videos since that day,
because the next sync run first adds them as an apply does. ``SyncRun`` does that catch-up with ``RuleReader`` and
``videos_to_add`` of this module.

An apply adds the matching videos to the target playlist of the rule, the oldest first, at most APPLY_LIMIT of them. It
writes an ``added_video`` row and an ``added`` action for each add, as a sync run does, but it never writes
``seen_video``, a mark or a read time, because those belong to the processing window of the sync. An upcoming or live
entry is never added.
"""

import datetime
import logging
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager import notify
from youtube_automanager.db.models import (
    AddedVideo,
    Channel,
    Playlist,
    Rule,
    RuleChannel,
    Run,
    RunAction,
    RunActionKind,
    utc_now,
)
from youtube_automanager.engine.account_session import (
    DEAD_SESSION_TEXT,
    AccountSession,
    AccountUnavailable,
    CheckOutcome,
)
from youtube_automanager.engine.modes import Known, Processing, Video, collect, has_state
from youtube_automanager.engine.rule_preview import (
    APPLY_LIMIT,
    PREVIEW_LIMIT,
    READ_LIMIT,
    DraftRule,
    Mark,
    Preview,
    PreviewVideo,
)
from youtube_automanager.engine.rules import RuleError, Verdict, evaluate
from youtube_automanager.engine.sync import (
    FEED_SOURCE,
    FEED_TITLE,
    SHORTS_LIMIT,
    RuleSpec,
    SyncAborted,
    load_processing,
    load_source_state,
    resets_state,
    rule_spec,
)
from youtube_automanager.settings import Settings
from youtube_automanager.youtube import public
from youtube_automanager.youtube.account import AccountError, NotLoggedInError
from youtube_automanager.youtube.public import PublicSourceError

LOG = logging.getLogger(__name__)

# The name of the draft of a rule in an error text.
DRAFT_NAME = "draft"


class RuleRefused(Exception):
    """The rule cannot be read: it does not exist any more, or its condition is not valid. The text names the cause."""


class ApplyAborted(Exception):
    """The apply cannot start or go on. The text names the cause and what the user can do."""


class _Stopped(Exception):
    """The apply adds no more videos. The history already has the error."""


class ReportError(Protocol):
    """Takes the text of an error and the place where it came from."""

    def __call__(
        self,
        text: str,
        *,
        channel_id: str | None = None,
        rule_id: int | None = None,
        video: Video | None = None,
    ) -> None: ...


class AccountLike(Protocol):
    """The part of ``AccountClient`` that an apply uses."""

    def iter_subscription_feed(self) -> Iterable[Any]: ...

    def add_video(self, playlist_id: str, video_id: str) -> None: ...


type ReadUploads = Callable[[str], Iterable[Any]]
type ReadShorts = Callable[[str, int], Iterable[str]]
type ReadFeed = Callable[[], Iterable[Any]]


@dataclass(frozen=True)
class RuleVersion:
    """One version of a rule, the saved one or a draft: its evaluation form, the channels that it reads, and the first
    day of the older videos that the next run also adds."""

    spec: RuleSpec
    channel_ids: frozenset[str]
    catch_up_since: datetime.date | None = None

    @property
    def source_keys(self) -> frozenset[str]:
        """The keys of the sources: the channel ids, or the key of the feed for a rule without channels."""
        return self.channel_ids or frozenset({FEED_SOURCE})


@dataclass(frozen=True)
class PreviewRequest:
    """What a preview compares. The saved version alone is the preview of an apply, the draft alone is a new rule, and
    both are a change of a saved rule."""

    saved: RuleVersion | None
    draft: RuleVersion | None

    @property
    def source_keys(self) -> frozenset[str]:
        return frozenset().union(*(version.source_keys for version in (self.saved, self.draft) if version is not None))

    @property
    def reads_feed(self) -> bool:
        return FEED_SOURCE in self.source_keys


@dataclass(frozen=True)
class Found:
    """A video that the read found, with the title of its channel and the keys of each source that listed it."""

    video: Video
    channel_title: str | None
    sources: frozenset[str]


@dataclass(frozen=True)
class SourceState:
    """The stored state of a source that the next sync run reads: its videos, its mark and the day of its last read."""

    known: Mapping[str, Known]
    mark: datetime.date | None
    read_before: datetime.date | None


@dataclass(frozen=True)
class SourcesRead:
    """The videos of the sources since the date, each once, newest first, and the keys of the sources whose read
    stopped after READ_LIMIT entries."""

    found: tuple[Found, ...]
    cut: tuple[str, ...]


@dataclass
class ApplyReport:
    added: list[notify.AddedVideo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # The number of the matching videos that the limit of one apply left for the next apply.
    left: int = 0


def saved_version(session: Session, rule_id: int) -> RuleVersion:
    """Return the saved version of the rule ``rule_id``. Raise RuleRefused when it is gone or not valid."""
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise RuleRefused("The rule does not exist any more.")
    try:
        spec = rule_spec(rule)
    except RuleError as error:
        raise RuleRefused(f"Rule {rule.name}: {error}") from None
    channels = session.scalars(select(RuleChannel.channel_id).where(RuleChannel.rule_id == rule_id))
    return RuleVersion(spec, frozenset(channels), rule.catch_up_since)


def draft_version(draft: DraftRule, rule_id: int | None = None) -> RuleVersion:
    """Return the version of a draft from the editor. Raise RuleRefused when its condition is not valid.

    A draft without a target playlist gets an empty playlist id, which no stored add has.
    """
    # A rule that no session holds, so ``rule_spec`` builds the draft exactly as it builds a saved rule.
    rule = Rule(
        id=rule_id,
        name=DRAFT_NAME,
        condition=draft.condition,
        no_shorts=draft.no_shorts,
        target_playlist_id=draft.playlist_id or "",
    )
    try:
        spec = rule_spec(rule)
    except RuleError as error:
        raise RuleRefused(f"The condition is not valid: {error}") from None
    return RuleVersion(spec, frozenset(draft.channel_ids), draft.catch_up_since)


def load_request(sessions: sessionmaker[Session], rule_id: int | None, draft: DraftRule | None) -> PreviewRequest:
    """Return what a preview of the saved rule ``rule_id`` and of ``draft`` compares. Raise RuleRefused."""
    if rule_id is None and draft is None:
        raise ValueError("a preview needs a saved rule, a draft or both")
    saved = None
    if rule_id is not None:
        with sessions() as session:
            saved = saved_version(session, rule_id)
    return PreviewRequest(saved, None if draft is None else draft_version(draft, rule_id))


def _no_feed() -> Iterable[Any]:
    raise AccountUnavailable("The preview of a rule with channels does not read the subscriptions feed.")


class RuleReader:
    """Reads the sources of a rule since a date, and evaluates the versions of a rule as a sync run does.

    ``read_uploads`` and ``read_shorts`` read public channel data, and ``read_feed`` reads the subscriptions feed of
    the account. A channel that fails to read goes to ``error``, and the read goes on with the other sources. A failed
    read of the feed raises its error to the caller. ``titles`` gives the title of each stored channel for the error
    texts.
    """

    def __init__(
        self,
        *,
        read_uploads: ReadUploads,
        read_shorts: ReadShorts,
        read_feed: ReadFeed = _no_feed,
        error: ReportError,
        titles: Mapping[str, str] | None = None,
    ) -> None:
        self._read_uploads = read_uploads
        self._read_shorts = read_shorts
        self._read_feed = read_feed
        self._error = error
        self._titles = dict(titles or {})
        self._shorts: dict[str, frozenset[str] | None] = {}

    def title(self, key: str) -> str:
        return FEED_TITLE if key == FEED_SOURCE else self._titles.get(key, key)

    # The read.

    def read(self, keys: Iterable[str], since: datetime.date) -> SourcesRead:
        """Read each source of ``keys`` since the day ``since``. The channels come first, in the order of their ids."""
        taken: dict[str, list[tuple[Video, str | None]]] = {}
        cut: list[str] = []
        for key in sorted(keys, key=source_order):
            read = self._read_source(key, since)
            if read is None:
                continue
            taken[key], was_cut = read
            if was_cut:
                cut.append(key)
        return SourcesRead(_found(taken), tuple(cut))

    def read_next_run(self, states: Mapping[str, SourceState], processing: Processing) -> SourcesRead:
        """Read the processing window of each source of ``states`` as the next sync run reads it.

        Keep only the videos that the run evaluates: the new ones and the deferred ones. Each source has stored state,
        because a source without it gets a baseline and no evaluation.
        """
        taken: dict[str, list[tuple[Video, str | None]]] = {}
        for key in sorted(states, key=source_order):
            read = self._read_window(key, states[key], processing)
            if read is not None:
                taken[key] = read
        return SourcesRead(_found(taken), ())

    def _read_source(self, key: str, since: datetime.date) -> tuple[list[tuple[Video, str | None]], bool] | None:
        """Read one source. Return its videos and whether READ_LIMIT cut the read, or None when a channel failed.

        A failed read of a channel gives no video at all, as in a sync run.
        """
        if key == FEED_SOURCE:
            return _take(self._read_feed(), since)
        try:
            return _take(self._read_uploads(key), since)
        except (PublicSourceError, ValueError) as error:
            self._error(f"Channel {self.title(key)}: {error}", channel_id=key)
            return None

    def _read_window(
        self, key: str, state: SourceState, processing: Processing
    ) -> list[tuple[Video, str | None]] | None:
        """Read the window of one source with ``collect`` of the sync. Return None when a channel failed."""
        channel_titles: dict[str, str | None] = {}

        def videos(listing: Iterable[Any]) -> Iterator[Video]:
            for entry in listing:
                video = Video.from_entry(entry)
                channel_titles.setdefault(video.video_id, getattr(entry, "channel_title", None))
                yield video

        def todo(listing: Iterable[Any]) -> list[tuple[Video, str | None]]:
            collected = collect(processing, videos(listing), state.known, state.mark, state.read_before)
            return [(video, channel_titles.get(video.video_id)) for video in collected.todo]

        if key == FEED_SOURCE:
            return todo(self._read_feed())
        try:
            return todo(self._read_uploads(key))
        except (PublicSourceError, ValueError) as error:
            self._error(f"Channel {self.title(key)}: {error}", channel_id=key)
            return None

    # The evaluation.

    def verdicts(self, found: Found, versions: Sequence[RuleVersion]) -> list[Verdict]:
        """Evaluate each version for the video. A version that does not read a source of the video gives NO_MATCH.

        A version that needs ``is_short`` reads the Shorts only when it must, as in a sync run.
        """
        video = found.video
        fields = video.fields()
        verdicts = []
        for version in versions:
            if not found.sources & version.source_keys:
                verdicts.append(Verdict.NO_MATCH)
                continue
            rule = version.spec
            verdict = self._evaluate(rule, fields, video)
            if verdict is Verdict.UNKNOWN and rule.needs_is_short and fields["is_short"] is None:
                is_short = self._is_short(video)
                if is_short is not None:
                    fields = video.fields(is_short)
                    verdict = self._evaluate(rule, fields, video)
            verdicts.append(verdict)
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
                self._error(
                    f"Channel {self.title(channel_id)}: Cannot read the Shorts of the channel: {error}",
                    channel_id=channel_id,
                )
        ids = self._shorts[channel_id]
        return None if ids is None else video.video_id in ids


def source_order(key: str) -> tuple[bool, str]:
    """The order of the sources of a read: the channels by their ids, then the feed."""
    return key == FEED_SOURCE, key


def _found(taken: Mapping[str, Iterable[tuple[Video, str | None]]]) -> tuple[Found, ...]:
    """Merge the videos of each source: each video once, with each source that listed it, newest first."""
    listed: dict[str, tuple[Video, str | None]] = {}
    sources_of: dict[str, set[str]] = {}
    for key, videos in taken.items():
        for video, channel_title in videos:
            listed.setdefault(video.video_id, (video, channel_title))
            sources_of.setdefault(video.video_id, set()).add(key)
    return _newest_first(
        Found(video, channel_title, frozenset(sources_of[video_id]))
        for video_id, (video, channel_title) in listed.items()
    )


def _newest_first(found: Iterable[Found]) -> tuple[Found, ...]:
    """Sort the videos newest first, and an entry without a date first of all.

    A sort in reverse keeps the order of equal keys, so the entries of one day stay in the order of the read.
    """
    return tuple(
        sorted(
            found,
            key=lambda item: (item.video.upload_date is None, item.video.upload_date or datetime.date.min),
            reverse=True,
        )
    )


def _take(listing: Iterable[Any], since: datetime.date) -> tuple[list[tuple[Video, str | None]], bool]:
    """Take the entries of a listing, newest first, until the first dated entry before ``since`` or READ_LIMIT entries.

    Return the videos with the channel title of each entry, and whether the limit cut the read before the date.
    """
    taken = []
    for count, entry in enumerate(listing, start=1):
        video = Video.from_entry(entry)
        if video.upload_date is not None and video.upload_date < since:
            return taken, False
        taken.append((video, getattr(entry, "channel_title", None)))
        if count >= READ_LIMIT:
            return taken, True
    return taken, False


# The preview.


def preview(
    sessions: sessionmaker[Session],
    request: PreviewRequest,
    since: datetime.date,
    *,
    read_uploads: ReadUploads,
    read_shorts: ReadShorts,
    read_feed: ReadFeed = _no_feed,
) -> Preview:
    """Read the sources of ``request`` since ``since`` and tell what each version does with their videos.

    The preview writes nothing. A saved version alone lists each video that it matches or cannot decide, marked as an
    apply treats it. A draft alone lists each video that it matches or cannot decide. A change lists only the videos
    that one version matches and the other does not. A failed read of the feed raises its error.
    """
    errors: list[str] = []

    def error(text: str, **_where: Any) -> None:
        LOG.warning("Preview: %s", text)
        errors.append(text)

    with sessions() as session:
        titles = _channel_titles(session)
        added = _added(session, request)
    reader = RuleReader(
        read_uploads=read_uploads, read_shorts=read_shorts, read_feed=read_feed, error=error, titles=titles
    )
    read = reader.read(request.source_keys, since)
    listed, counts = _listed(reader, request, read, added, titles)
    return Preview(listed, counts, tuple(errors), tuple(reader.title(key) for key in read.cut))


def next_run_preview(
    sessions: sessionmaker[Session],
    request: PreviewRequest,
    *,
    read_uploads: ReadUploads,
    read_shorts: ReadShorts,
    read_feed: ReadFeed = _no_feed,
    account: str | None = None,
) -> Preview:
    """Tell what the next sync run does with the rule of ``request``: the draft, or the saved rule without a draft.

    The preview writes nothing. It lists the videos of two reads, each video once, newest first:

    - The catch-up: the videos since the first day of the rule, which the run reads first, as an apply reads them.
    - The sync: the videos that the run evaluates after the catch-up, the new ones and the deferred ones. The
      processing mode and the stored state of each source decide how far the read goes. A source without stored state
      gets a baseline, and so does each source when the mode of the stored state is another mode. The preview reads
      no such source and names it in ``baseline_sources``.

    A video of both reads gets the mark of the sync, because the sync of the run also adds an upcoming video that
    matches. ``account`` is the cause when the YouTube account is not available, so each matching video waits for it.
    A failed read of the feed raises its error.
    """
    errors: list[str] = []

    def error(text: str, **_where: Any) -> None:
        LOG.warning("Preview of the next run: %s", text)
        errors.append(text)

    version = request.draft if request.draft is not None else request.saved
    if version is None:
        raise ValueError("a preview needs a saved rule, a draft or both")
    with sessions() as session:
        titles = _channel_titles(session)
        try:
            processing = load_processing(session)
        except SyncAborted as aborted:
            return Preview((), {}, errors=(str(aborted),))
        mode_changed = resets_state(session, processing)
        states: dict[str, SourceState] = {}
        baselines: list[str] = []
        for key in sorted(version.source_keys, key=source_order):
            known, mark, read_at = load_source_state(session, key)
            read_before = None if read_at is None else read_at.date()
            if mode_changed or not has_state(processing, known, mark, read_before):
                baselines.append(key)
            else:
                states[key] = SourceState(known, mark, read_before)
        added = _added_to(session, version.spec.playlist_id)
    reader = RuleReader(
        read_uploads=read_uploads, read_shorts=read_shorts, read_feed=read_feed, error=error, titles=titles
    )
    synced = reader.read_next_run(states, processing)
    caught = SourcesRead((), ())
    if version.catch_up_since is not None:
        caught = reader.read(version.source_keys, version.catch_up_since)
    synced_ids = {found.video.video_id for found in synced.found}
    marks: dict[str, Mark | None] = {}
    merged: list[Found] = []
    catch_up_adds = 0
    for found in caught.found:
        (verdict,) = reader.verdicts(found, [version])
        mark = _catch_up_mark(found.video, verdict, added, account)
        catch_up_adds += mark is Mark.WILL_ADD
        if found.video.video_id not in synced_ids:
            marks[found.video.video_id] = mark
            merged.append(found)
    for found in synced.found:
        (verdict,) = reader.verdicts(found, [version])
        marks[found.video.video_id] = _sync_mark(verdict, found.video, added, account)
        merged.append(found)
    listed, counts = _counted(((found, marks[found.video.video_id]) for found in _newest_first(merged)), titles)
    return Preview(
        listed,
        counts,
        tuple(errors),
        cut_sources=tuple(reader.title(key) for key in caught.cut),
        baseline_sources=tuple(reader.title(key) for key in baselines),
        mode_changed=mode_changed,
        catch_up_adds=catch_up_adds,
    )


def _sync_mark(verdict: Verdict, video: Video, added: set[str], account: str | None) -> Mark | None:
    """Return what the sync of the next run does with a video that it evaluates, or None when the rule does not match.

    The sync adds also an upcoming or live video that matches.
    """
    if verdict is Verdict.NO_MATCH:
        return None
    if video.video_id in added:
        return Mark.ALREADY_ADDED
    if verdict is Verdict.UNKNOWN:
        return Mark.WAITS_FOR_FIELD
    if account is not None:
        return Mark.WAITS_FOR_ACCOUNT
    return Mark.WILL_ADD


def _catch_up_mark(video: Video, verdict: Verdict, added: set[str], account: str | None) -> Mark | None:
    """Return what the catch-up of the next run does with a video, or None when the rule does not match it.

    The catch-up adds a video as an apply does, so it never adds an upcoming or live one.
    """
    if verdict is Verdict.NO_MATCH:
        return None
    if video.video_id in added:
        return Mark.ALREADY_ADDED
    if verdict is Verdict.UNKNOWN:
        return Mark.WAITS_FOR_FIELD
    if not video.is_finished:
        return Mark.WAITS_FOR_LIVE
    if account is not None:
        return Mark.WAITS_FOR_ACCOUNT
    return Mark.WILL_ADD


def _channel_titles(session: Session) -> dict[str, str]:
    return {row.channel_id: row.title for row in session.scalars(select(Channel))}


def _added_to(session: Session, playlist_id: str) -> set[str]:
    """The ids of the videos that the service added to the playlist ``playlist_id``."""
    return set(session.scalars(select(AddedVideo.video_id).where(AddedVideo.playlist_id == playlist_id)))


def _added(session: Session, request: PreviewRequest) -> set[str]:
    """The ids of the videos in the target playlist of a saved rule alone, for the marks of an apply."""
    if request.saved is None or request.draft is not None:
        return set()
    return _added_to(session, request.saved.spec.playlist_id)


def _listed(
    reader: RuleReader, request: PreviewRequest, read: SourcesRead, added: set[str], titles: Mapping[str, str]
) -> tuple[tuple[PreviewVideo, ...], dict[Mark, int]]:
    """Mark each found video. Return at most PREVIEW_LIMIT of the listed videos, and the count of each mark."""
    return _counted(((found, _mark(reader, request, found, added)) for found in read.found), titles)


def _counted(
    marked: Iterable[tuple[Found, Mark | None]], titles: Mapping[str, str]
) -> tuple[tuple[PreviewVideo, ...], dict[Mark, int]]:
    """List each video with a mark. Return at most PREVIEW_LIMIT of the listed videos, and the count of each mark."""
    listed: list[PreviewVideo] = []
    counts: dict[Mark, int] = {}
    for found, mark in marked:
        if mark is None:
            continue
        counts[mark] = counts.get(mark, 0) + 1
        if len(listed) < PREVIEW_LIMIT:
            listed.append(_preview_video(found, mark, titles))
    return tuple(listed), counts


def _mark(reader: RuleReader, request: PreviewRequest, found: Found, added: set[str]) -> Mark | None:
    """Return the mark of the video in the preview, or None when the preview does not list it."""
    if request.saved is not None and request.draft is not None:
        saved, draft = reader.verdicts(found, [request.saved, request.draft])
        if draft is Verdict.MATCH and saved is not Verdict.MATCH:
            return Mark.MATCHES_NOW
        if saved is Verdict.MATCH and draft is not Verdict.MATCH:
            return Mark.NO_LONGER_MATCHES
        return None
    if request.draft is not None:
        (verdict,) = reader.verdicts(found, [request.draft])
        return {Verdict.MATCH: Mark.MATCHES, Verdict.UNKNOWN: Mark.UNDECIDED}.get(verdict)
    (verdict,) = reader.verdicts(found, [request.saved])
    return _apply_mark(found.video, verdict, added)


def _apply_mark(video: Video, verdict: Verdict, added: set[str]) -> Mark | None:
    """Return what an apply does with the video, or None when the rule does not match it."""
    if verdict is Verdict.NO_MATCH:
        return None
    if video.video_id in added:
        return Mark.ALREADY_ADDED
    if verdict is Verdict.UNKNOWN or not video.is_finished:
        return Mark.UNDECIDED
    return Mark.WILL_ADD


def _preview_video(found: Found, mark: Mark, titles: Mapping[str, str]) -> PreviewVideo:
    video = found.video
    channel_id = video.channel_id
    stored = titles.get(channel_id) if channel_id is not None else None
    return PreviewVideo(
        video_id=video.video_id,
        title=video.title,
        channel_title=stored or found.channel_title or channel_id or "",
        upload_date=video.upload_date,
        mark=mark,
    )


# The apply.


def videos_to_add(reader: RuleReader, version: RuleVersion, read: SourcesRead, added: set[str]) -> list[Video]:
    """Return the videos of ``read`` that an apply or a catch-up of ``version`` adds, the oldest first.

    A video that the service added to the target playlist before is not added again, and ``added`` holds the ids of
    those videos. An upcoming or live video and a video that the rule cannot decide are not added.
    """
    matching = [
        found.video
        for found in read.found
        if _apply_mark(found.video, reader.verdicts(found, [version])[0], added) is Mark.WILL_ADD
    ]
    return matching[::-1]


class RuleApply:
    """One apply of the saved rule ``rule_id`` to the videos of its sources since ``since``, as the run ``run_id``.

    ``open_account`` gives the account client, and the apply calls it at the first need: to read the feed, or to add a
    video. ``session_died`` takes a session that YouTube refused during the apply. ``send`` sends a notification and
    never raises.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        run_id: int,
        rule_id: int,
        since: datetime.date,
        *,
        read_uploads: ReadUploads,
        read_shorts: ReadShorts,
        open_account: Callable[[], AccountLike],
        send: Callable[[notify.Notification], object],
        session_died: Callable[[NotLoggedInError], object] | None = None,
        clock: Callable[[], datetime.datetime] = utc_now,
    ) -> None:
        self._sessions = sessions
        self._run_id = run_id
        self._rule_id = rule_id
        self._since = since
        self._read_uploads = read_uploads
        self._read_shorts = read_shorts
        self._open_account = open_account
        self._send = send
        self._session_died = session_died
        self._clock = clock
        self._account: AccountLike | None = None
        self._playlist_titles: dict[str, str] = {}
        self.report = ApplyReport()

    def run(self) -> ApplyReport:
        """Do the apply. Raise ApplyAborted when the rule is gone or not valid."""
        with self._sessions() as session:
            try:
                version = saved_version(session, self._rule_id)
            except RuleRefused as error:
                raise ApplyAborted(str(error)) from None
            titles = {row.channel_id: row.title for row in session.scalars(select(Channel))}
            self._playlist_titles = {row.playlist_id: row.title for row in session.scalars(select(Playlist))}
        rule = version.spec
        LOG.info("Run %s: apply rule %s to the videos since %s", self._run_id, rule.name, self._since.isoformat())
        reader = RuleReader(
            read_uploads=self._read_uploads,
            read_shorts=self._read_shorts,
            read_feed=lambda: self._account_client().iter_subscription_feed(),
            error=self._error,
            titles=titles,
        )
        try:
            read = reader.read(version.source_keys, self._since)
        except NotLoggedInError as error:
            self._error(f"Cannot read {FEED_TITLE}: {self._lose_session(error)}")
            read = None
        except (AccountError, AccountUnavailable) as error:
            self._error(f"Cannot read {FEED_TITLE}: {error}")
            read = None
        if read is not None:
            self._add_matching(reader, version, read)
        LOG.info(
            "Run %s finished the apply: %d video(s) added, %d left for the next apply, %d error(s)",
            self._run_id,
            len(self.report.added),
            self.report.left,
            len(self.report.errors),
        )
        self._notify(notify.run_summary(self.report.added, self.report.errors))
        return self.report

    def _add_matching(self, reader: RuleReader, version: RuleVersion, read: SourcesRead) -> None:
        with self._sessions() as session:
            added = set(
                session.scalars(select(AddedVideo.video_id).where(AddedVideo.playlist_id == version.spec.playlist_id))
            )
        # The read gives each video once, and the sync lock keeps a sync run out during the apply, so an add needs no
        # second check.
        matching = videos_to_add(reader, version, read, added)
        # The limit leaves the newest videos to the next apply.
        todo = matching[:APPLY_LIMIT]
        self.report.left = len(matching) - len(todo)
        try:
            for video in todo:
                self._add(video, version.spec)
        except _Stopped:
            pass

    def _add(self, video: Video, rule: RuleSpec) -> None:
        """Add the video to the target playlist of the rule. Raise _Stopped when the add failed, so the apply adds no
        more videos."""
        playlist_id = rule.playlist_id
        playlist_title = self._playlist_titles.get(playlist_id, playlist_id)
        try:
            self._account_client().add_video(playlist_id, video.video_id)
        except NotLoggedInError as error:
            self._error(f"The apply stopped: {self._lose_session(error)}", rule_id=rule.id, video=video)
            raise _Stopped from error
        except AccountUnavailable as error:
            self._error(f"The apply stopped: {error}", rule_id=rule.id, video=video)
            raise _Stopped from error
        except (AccountError, ValueError) as error:
            self._error(
                f"Cannot add video {video.video_id} to {playlist_title}: {error}",
                rule_id=rule.id,
                playlist_id=playlist_id,
                video=video,
            )
            raise _Stopped from error
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
        self._notify(notify.video_added(video.title, playlist_title, video.url))

    def _account_client(self) -> AccountLike:
        if self._account is None:
            self._account = self._open_account()
        return self._account

    def _lose_session(self, error: NotLoggedInError) -> str:
        """Take a session that YouTube refused during the apply. Return the cause for the history."""
        LOG.warning("Run %s: YouTube refused the session: %s", self._run_id, error)
        if self._session_died is not None:
            self._session_died(error)
        return DEAD_SESSION_TEXT

    def _error(
        self,
        text: str,
        *,
        channel_id: str | None = None,
        rule_id: int | None = None,
        playlist_id: str | None = None,
        video: Video | None = None,
    ) -> None:
        LOG.warning("Run %s: %s", self._run_id, text)
        self.report.errors.append(text)
        with self._sessions.begin() as session:
            session.add(
                RunAction(
                    run_id=self._run_id,
                    at=self._clock(),
                    action=RunActionKind.ERROR,
                    video_id=None if video is None else video.video_id,
                    channel_id=channel_id if video is None else video.channel_id,
                    rule_id=rule_id,
                    playlist_id=playlist_id,
                    detail=text,
                )
            )

    def _notify(self, notification: notify.Notification) -> None:
        try:
            self._send(notification)
        except Exception as error:  # noqa: BLE001
            # A failed notification must never fail the apply.
            LOG.warning("Run %s: cannot send %s: %s", self._run_id, notification.event.value, type(error).__name__)


def apply_with_youtube(
    sessions: sessionmaker[Session], run_id: int, rule_id: int, since: datetime.date, settings: Settings
) -> ApplyReport:
    """Do the apply of the run ``run_id`` with the real YouTube sources and the notification targets of the database.

    The apply first checks the session of the stored cookies, because each add needs the account. Raise ApplyAborted
    without a read when the session does not live. At its end, the apply saves the updated cookies.
    """

    def send(notification: notify.Notification) -> object:
        return notify.send_to_stored_targets(sessions, notification)

    account = AccountSession(sessions, settings, send=send)
    result = account.check()
    LOG.info("Run %s: the session check gave %s", run_id, result.outcome.value)
    try:
        if result.outcome is not CheckOutcome.ALIVE:
            raise ApplyAborted(f"The apply needs the YouTube account. {result.detail}")
        return RuleApply(
            sessions,
            run_id,
            rule_id,
            since,
            read_uploads=public.iter_channel_uploads,
            read_shorts=lambda channel_id, limit: public.shorts_video_ids(channel_id, limit=limit),
            open_account=account.open,
            session_died=account.died,
            send=send,
        ).run()
    finally:
        account.write_back()
