"""The notification targets of the settings page: the reads and the writes of the ``notification_target`` table.

A target URL holds the token of its service. The page shows a stored URL only in the edit form of its target, because
the user needs to see the URL to change it. The web UI has no login, so the deployer must protect the access to the
UI. A record of this module holds only the scheme of the URL, for example ``tgram://…``, for the cards of the targets.
Only ``load_target`` reads the URL itself, for a test notification and for the edit form. A change of a target keeps
the stored URL when it gets no new URL. This module imports no NiceGUI, so its functions have tests of their own. Each
function that reads or writes the database blocks.
"""

import enum
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import NotificationTarget
from youtube_automanager.i18n import N_
from youtube_automanager.notify import DEFAULT_EVENTS, Event, Level, Target

# The list of the services and of their URLs.
APPRISE_URLS = "https://appriseit.com/services/"
HIDDEN = "…"
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]{0,31})://")

EVENT_LABELS = {
    Event.COOKIE_SESSION_DEAD: N_("Cookies refused"),
    Event.COOKIE_EXPIRY_WARNING: N_("Cookies expire soon"),
    Event.RUN_SUMMARY: N_("Run summary"),
    Event.RUN_FAILED: N_("Run failed"),
    Event.CHANNEL_ERROR: N_("Channel error"),
    Event.YTDLP_UPDATE_RESULT: N_("yt-dlp update"),
    Event.VIDEO_ADDED: N_("Video added"),
    Event.SCHEDULER_STATE: N_("Schedule paused or resumed"),
}
LEVEL_LABELS = {
    Level.INFO: N_("Info"),
    Level.SUCCESS: N_("Success"),
    Level.WARNING: N_("Warning"),
    Level.FAILURE: N_("Failure"),
}


class SaveOutcome(enum.Enum):
    SAVED = "saved"
    # Another target has the same name, in any case.
    NAME_TAKEN = "name_taken"
    # The target to change does not exist any more.
    GONE = "gone"


@dataclass(frozen=True)
class TargetChoices:
    """The choices of a target in its card on the page: its events, its lowest level and its switch."""

    events: frozenset[Event]
    min_level: Level
    enabled: bool


@dataclass(frozen=True)
class TargetRecord:
    """A target as the page shows it, without its URL."""

    id: int
    name: str
    # Only the scheme of the URL, for example "tgram://…".
    url_scheme: str
    min_level: Level
    events: frozenset[Event]
    enabled: bool

    @property
    def choices(self) -> TargetChoices:
        return TargetChoices(self.events, self.min_level, self.enabled)


def keep_edits(stored: TargetChoices, edited: TargetChoices, read: TargetChoices) -> TargetChoices:
    """Return the choices ``read`` from the database with each choice of ``edited`` that differs from ``stored``.

    ``stored`` holds the choices that the page read or saved before, and ``edited`` holds the choices of the controls.
    So a new read keeps each change of the user, and each other choice takes the value that the read gave.
    """
    added = edited.events - stored.events
    removed = stored.events - edited.events
    return TargetChoices(
        events=(read.events - removed) | added,
        min_level=edited.min_level if edited.min_level != stored.min_level else read.min_level,
        enabled=edited.enabled if edited.enabled != stored.enabled else read.enabled,
    )


def url_scheme(url: str) -> str:
    """Return the scheme of ``url`` with the mark of the hidden rest, for example ``tgram://…``.

    A URL without a scheme gives only the mark, so no part of the URL shows.
    """
    found = _SCHEME.match(url)
    return f"{found.group(1)}://{HIDDEN}" if found else HIDDEN


def _level(value: int) -> Level:
    """Return the level of a stored number. A number outside the known levels becomes the nearest known level."""
    return Level(min(max(value, Level.INFO), Level.FAILURE))


def _known_events(names: list[str] | None) -> frozenset[Event]:
    known = {event.value for event in Event}
    return frozenset(Event(name) for name in names or () if name in known)


def _ordered(names: set[str]) -> list[str]:
    """Return the event names in the order of ``Event``, and the unknown names after them, sorted."""
    order = {event.value: index for index, event in enumerate(Event)}
    return sorted(names, key=lambda name: (order.get(name, len(order)), name))


def read_targets(sessions: sessionmaker[Session]) -> list[TargetRecord]:
    """Read the targets in the order of their creation, each without its URL."""
    with sessions() as session:
        return [
            TargetRecord(
                id=row.id,
                name=row.name,
                url_scheme=url_scheme(row.url),
                min_level=_level(row.min_level),
                events=_known_events(row.events),
                enabled=row.enabled,
            )
            for row in session.scalars(select(NotificationTarget).order_by(NotificationTarget.id))
        ]


def load_target(sessions: sessionmaker[Session], target_id: int) -> Target | None:
    """Return the target with its URL, for a test notification or the edit form. Return None when it does not exist."""
    with sessions() as session:
        row = session.get(NotificationTarget, target_id)
        if row is None:
            return None
        return Target(
            row.name, row.url, min_level=_level(row.min_level), events=_known_events(row.events), enabled=row.enabled
        )


def save_target(sessions: sessionmaker[Session], target_id: int | None, name: str, url: str | None) -> SaveOutcome:
    """Create a target when ``target_id`` is None, else change its name and its URL.

    A new target needs a URL. It gets the default events, the lowest level and the switch on. A change without a URL
    keeps the stored URL. The caller checks the URL with Apprise first.
    """
    if target_id is None and not url:
        raise ValueError("A new target needs a URL")
    try:
        with sessions.begin() as session:
            # Python compares the case, because the lower() of SQLite changes only the ASCII letters.
            others = session.execute(select(NotificationTarget.id, NotificationTarget.name))
            if any(other_id != target_id and other.casefold() == name.casefold() for other_id, other in others):
                return SaveOutcome.NAME_TAKEN
            if target_id is None:
                session.add(
                    NotificationTarget(
                        name=name,
                        url=url,
                        min_level=Level.INFO.value,
                        events=_ordered({event.value for event in DEFAULT_EVENTS}),
                        enabled=True,
                    )
                )
                return SaveOutcome.SAVED
            row = session.get(NotificationTarget, target_id)
            if row is None:
                return SaveOutcome.GONE
            row.name = name
            if url:
                row.url = url
            return SaveOutcome.SAVED
    except IntegrityError:
        # Another page saved the same name between the check and the write.
        return SaveOutcome.NAME_TAKEN


def delete_target(sessions: sessionmaker[Session], target_id: int) -> None:
    with sessions.begin() as session:
        row = session.get(NotificationTarget, target_id)
        if row is not None:
            session.delete(row)


def save_choices(sessions: sessionmaker[Session], target_id: int, stored: TargetChoices, edited: TargetChoices) -> bool:
    """Write each choice of ``edited`` that differs from ``stored``, the choices that the page read or saved before.

    Each other stored value stays, also a stored event name that this version does not know. Return False when the
    target is gone.
    """
    with sessions.begin() as session:
        row = session.get(NotificationTarget, target_id)
        if row is None:
            return False
        if edited.events != stored.events:
            names = set(row.events or ())
            names |= {event.value for event in edited.events - stored.events}
            names -= {event.value for event in stored.events - edited.events}
            # A new list, so the change tracking sees the change.
            row.events = _ordered(names)
        if edited.min_level != stored.min_level:
            row.min_level = Level(edited.min_level).value
        if edited.enabled != stored.enabled:
            row.enabled = bool(edited.enabled)
        return True
