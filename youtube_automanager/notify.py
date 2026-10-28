"""Notifications through Apprise: events with levels, targets that pick their events, and one send for each target."""

import logging
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum

import apprise
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import NotificationTarget

LOG = logging.getLogger(__name__)

# Apprise logs the target URL with its tokens at TRACE and DEBUG for each send, and parts of it at WARNING
# and ERROR when it refuses a URL. Its loggers stay silent, and this module logs a target by its name only.
logging.getLogger(apprise.LOGGER_NAME).setLevel(logging.CRITICAL + 1)

APP_NAME = "YouTube AutoManager"
MAX_LISTED_ITEMS = 20
# The longest error text of one channel in a channel error message, so that 20 channels fit into one Telegram message.
MAX_ERROR_CHARS = 200
# The longest time that one target can hold a send. Apprise limits each request to 4 s to connect and 4 s between two
# reads, but a target can make several requests with pauses between them, and its URL can set longer limits.
DELIVER_TIMEOUT_S = 30.0


class Level(IntEnum):
    INFO = 0
    SUCCESS = 1
    WARNING = 2
    FAILURE = 3

    @property
    def notify_type(self) -> apprise.NotifyType:
        return apprise.NotifyType[self.name]


class Event(StrEnum):
    COOKIE_SESSION_DEAD = "cookie_session_dead"
    COOKIE_EXPIRY_WARNING = "cookie_expiry_warning"
    RUN_SUMMARY = "run_summary"
    RUN_FAILED = "run_failed"
    CHANNEL_ERROR = "channel_error"
    YTDLP_UPDATE_RESULT = "ytdlp_update_result"
    VIDEO_ADDED = "video_added"
    SCHEDULER_STATE = "scheduler_state"
    REQUEST_BREAKER = "request_breaker"


EVENT_LEVELS: dict[Event, Level] = {
    Event.COOKIE_SESSION_DEAD: Level.FAILURE,
    Event.COOKIE_EXPIRY_WARNING: Level.WARNING,
    Event.RUN_SUMMARY: Level.SUCCESS,
    Event.RUN_FAILED: Level.FAILURE,
    Event.CHANNEL_ERROR: Level.WARNING,
    Event.YTDLP_UPDATE_RESULT: Level.INFO,
    Event.VIDEO_ADDED: Level.INFO,
    Event.SCHEDULER_STATE: Level.INFO,
    Event.REQUEST_BREAKER: Level.WARNING,
}

DEFAULT_EVENTS: frozenset[Event] = frozenset(
    {
        Event.COOKIE_SESSION_DEAD,
        Event.COOKIE_EXPIRY_WARNING,
        Event.RUN_SUMMARY,
        Event.RUN_FAILED,
        Event.CHANNEL_ERROR,
        Event.YTDLP_UPDATE_RESULT,
        Event.REQUEST_BREAKER,
    }
)


@dataclass(frozen=True)
class Target:
    name: str
    url: str = field(repr=False)
    min_level: Level = Level.INFO
    events: frozenset[Event] = DEFAULT_EVENTS
    enabled: bool = True

    def accepts(self, event: Event) -> bool:
        return self.enabled and event in self.events and EVENT_LEVELS[event] >= self.min_level


@dataclass(frozen=True)
class Notification:
    event: Event
    title: str
    body: str

    @property
    def level(self) -> Level:
        return EVENT_LEVELS[self.event]


@dataclass(frozen=True)
class SendReport:
    succeeded: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True)
class AddedVideo:
    video_title: str
    playlist_title: str
    video_url: str


@dataclass(frozen=True)
class ChannelFailure:
    channel_title: str
    error_text: str


class Notifier:
    def __init__(self, targets: Iterable[Target]):
        self._targets = tuple(targets)

    def send(self, notification: Notification) -> SendReport:
        succeeded, failed, skipped = [], [], []
        for target in self._targets:
            if not target.accepts(notification.event):
                skipped.append(target.name)
            elif _deliver(target, notification.title, notification.body, notification.level, notification.event):
                succeeded.append(target.name)
            else:
                failed.append(target.name)
        return SendReport(tuple(succeeded), tuple(failed), tuple(skipped))

    @staticmethod
    def send_test(target: Target) -> bool:
        title = f"{APP_NAME}: test notification"
        body = f"This is a test notification for the target {target.name}. If you can read it, the target works."
        return _deliver(target, title, body, Level.INFO, "test notification")

    @staticmethod
    def is_valid_url(url: str) -> bool:
        return apprise.Apprise.instantiate(url) is not None


def stored_targets(session: Session) -> list[Target]:
    """Return the targets of the ``notification_target`` table.

    An unknown event name is left out, and a level outside the known levels becomes the nearest known level.
    """
    known_events = {event.value for event in Event}
    targets = []
    for row in session.scalars(select(NotificationTarget).order_by(NotificationTarget.id)):
        events = frozenset(Event(name) for name in row.events or () if name in known_events)
        min_level = Level(min(max(row.min_level, Level.INFO), Level.FAILURE))
        targets.append(Target(row.name, row.url, min_level=min_level, events=events, enabled=row.enabled))
    return targets


def send_to_stored_targets(sessions: sessionmaker[Session], notification: Notification) -> SendReport | None:
    """Send ``notification`` to each target of the database. Never raise, so a notification never fails a run.

    Return None when the targets cannot be read.
    """
    try:
        with sessions() as session:
            targets = stored_targets(session)
        return Notifier(targets).send(notification)
    except Exception as error:  # noqa: BLE001
        # The text of the error can hold a target URL, so only its type goes into the log.
        LOG.warning("Cannot send %s: %s", notification.event.value, type(error).__name__)
        return None


def _deliver(target: Target, title: str, body: str, level: Level, what: str) -> bool:
    """Send to one target, and give up after ``DELIVER_TIMEOUT_S``.

    The send runs in a daemon thread, because Apprise cannot stop a request. A thread that is late keeps its send, but
    the caller goes on, and the thread ends with the process at the latest.
    """
    results: list[bool] = []
    thread = threading.Thread(
        target=lambda: results.append(_deliver_now(target, title, body, level, what)),
        name="notify-target",
        daemon=True,
    )
    thread.start()
    thread.join(DELIVER_TIMEOUT_S)
    if thread.is_alive():
        LOG.warning("Cannot send %s to target %r: no result after %g s", what, target.name, DELIVER_TIMEOUT_S)
        return False
    return results[0]


def _deliver_now(target: Target, title: str, body: str, level: Level, what: str) -> bool:
    try:
        sender = apprise.Apprise()
        if not sender.add(target.url):
            LOG.warning("Cannot send %s to target %r: Apprise does not accept its URL", what, target.name)
            return False
        with sender.notify(
            body=body,
            title=title,
            notify_type=level.notify_type,
            body_format=apprise.NotifyFormat.TEXT,
        ) as result:
            status = result.status
    except Exception as error:  # noqa: BLE001
        # One broken target must not stop the others. The text of the exception can hold the URL,
        # so only its type goes into the log.
        LOG.warning("Cannot send %s to target %r: %s", what, target.name, type(error).__name__)
        return False
    if status != apprise.AppriseResultStatus.SUCCESS:
        LOG.warning("Cannot send %s to target %r: Apprise reports %s", what, target.name, status.name)
        return False
    LOG.info("Sent %s to target %r", what, target.name)
    return True


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _limited(lines: Sequence[str]) -> list[str]:
    shown = list(lines[:MAX_LISTED_ITEMS])
    if len(lines) > MAX_LISTED_ITEMS:
        shown.append(f"and {len(lines) - MAX_LISTED_ITEMS} more")
    return shown


def run_summary(added: Sequence[AddedVideo], errors: Sequence[str]) -> Notification:
    title = f"{APP_NAME}: {_count(len(added), 'video')} added"
    if errors:
        title += f", {_count(len(errors), 'error')}"
    parts = []
    if added:
        items = [f"- {item.video_title} -> {item.playlist_title}\n  {item.video_url}" for item in added]
        parts.append("\n".join([f"Added {_count(len(added), 'video')}:", *_limited(items)]))
    else:
        parts.append("The run added no videos.")
    if errors:
        items = [f"- {error}" for error in errors]
        parts.append("\n".join([f"{_count(len(errors), 'error')}:", *_limited(items)]))
    return Notification(Event.RUN_SUMMARY, title, "\n\n".join(parts))


def run_failed(error_text: str) -> Notification:
    return Notification(
        Event.RUN_FAILED,
        f"{APP_NAME}: the run failed",
        f"The run stopped with an error and added no more videos.\n\n{error_text}",
    )


def cookie_session_dead() -> Notification:
    return Notification(
        Event.COOKIE_SESSION_DEAD,
        f"{APP_NAME}: YouTube does not accept the cookies",
        "YouTube no longer accepts the cookies of the service. The service cannot read your subscriptions "
        "or change your playlists. Export new YouTube cookies from your browser and upload them to the service.",
    )


def cookie_expiry(days_left: int, expires_at: datetime) -> Notification:
    if days_left < 0:
        raise ValueError(f"days_left must not be negative, got {days_left}")
    when = "today" if days_left == 0 else f"in {_count(days_left, 'day')}"
    return Notification(
        Event.COOKIE_EXPIRY_WARNING,
        f"{APP_NAME}: the YouTube cookies expire {when}",
        f"The cookie file gives {expires_at.isoformat(sep=' ', timespec='minutes')} as the expiry time. "
        "Export new YouTube cookies from your browser and upload them to the service before that time.",
    )


def _shortened(text: str) -> str:
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 3] + "..."


def channel_error(failures: Sequence[ChannelFailure]) -> Notification:
    """Tell each channel that a run could not read, in one message. Raise ValueError without a failure."""
    if not failures:
        raise ValueError("A channel error needs at least one failed channel")
    if len(failures) == 1:
        title = f"{APP_NAME}: cannot read the channel {failures[0].channel_title}"
    else:
        title = f"{APP_NAME}: cannot read {_count(len(failures), 'channel')}"
    items = [f"- {failure.channel_title}: {_shortened(failure.error_text)}" for failure in failures]
    body = "\n".join(
        [
            f"yt-dlp failed to read {_count(len(failures), 'channel')}. The run continued with the other channels.",
            "",
            *_limited(items),
        ]
    )
    return Notification(Event.CHANNEL_ERROR, title, body)


def ytdlp_update(
    old_version: str | None,
    new_version: str | None,
    ok: bool,
    error_text: str | None = None,
    image_version: str | None = None,
) -> Notification:
    """Tell the result of a yt-dlp update.

    ``old_version`` is None when the data folder held no yt-dlp, so the service used the copy of the image, which has
    ``image_version``. ``new_version`` is None when the service could not get the latest version.
    """
    image = f"the yt-dlp {image_version} of the image" if image_version else "the yt-dlp of the image"
    if not ok:
        title = f"{APP_NAME}: the yt-dlp update failed"
        target = f" to {new_version}" if new_version else ""
        if old_version is None:
            body = f"yt-dlp did not update{target}. The data folder holds no yt-dlp, so the service keeps {image}."
        else:
            body = f"yt-dlp did not update from {old_version}{target}. The service keeps {old_version}."
        if error_text:
            body += f"\n\n{error_text}"
    elif old_version == new_version:
        title = f"{APP_NAME}: yt-dlp is up to date"
        body = f"yt-dlp {old_version} is the newest version."
    else:
        title = f"{APP_NAME}: yt-dlp updated to {new_version}"
        if old_version is None:
            body = (
                f"yt-dlp {new_version} is now in the data folder, which held no yt-dlp before. "
                f"Until now, the service used {image}."
            )
        else:
            body = f"yt-dlp updated from {old_version} to {new_version}."
        body += " The next run uses the new version."
    return Notification(Event.YTDLP_UPDATE_RESULT, title, body)


def video_added(video_title: str, playlist_title: str, video_url: str) -> Notification:
    return Notification(
        Event.VIDEO_ADDED,
        f"{APP_NAME}: video added to {playlist_title}",
        f"Added {video_title} to the playlist {playlist_title}.\n{video_url}",
    )


def _duration(pause: timedelta) -> str:
    """Return a pause as whole hours, or as whole minutes when it is not a whole number of hours."""
    minutes = int(pause.total_seconds()) // 60
    if minutes % 60 == 0:
        return _count(minutes // 60, "hour")
    return _count(minutes, "minute")


def request_breaker_opened(reason: str, pause: timedelta, until: datetime) -> Notification:
    """Tell that the service paused the requests with cookies. ``reason`` names the answer of YouTube, and ``until``
    is the end of the pause in the time zone of the service."""
    return Notification(
        Event.REQUEST_BREAKER,
        f"{APP_NAME}: the requests with cookies are paused",
        f"YouTube answered a request with the cookies of the account with {reason}. The service sends no request "
        f"with the cookies for {_duration(pause)}, until {until.strftime('%Y-%m-%d %H:%M %Z')}. Until then, each "
        "action with the account fails at once without a request to YouTube. The reads of public channels go on.",
    )


def request_breaker_closed() -> Notification:
    """Tell that the first request with cookies after the end of a pause got an answer."""
    return Notification(
        Event.REQUEST_BREAKER,
        f"{APP_NAME}: the requests with cookies work again",
        "The pause of the requests with the cookies of the account ended, and YouTube answered the first request "
        "after it. The requests with cookies work again.",
    )


def scheduler_state(paused: bool) -> Notification:
    if paused:
        title = f"{APP_NAME}: the schedule is paused"
        body = "The service starts no runs until you resume the schedule."
    else:
        title = f"{APP_NAME}: the schedule is resumed"
        body = "The service starts the runs by the schedule again."
    return Notification(Event.SCHEDULER_STATE, title, body)
