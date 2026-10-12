"""The update of yt-dlp in the data folder: each day by a schedule, and on request from the settings page.

The settings live in the ``setting`` table as one JSON value under ``SETTINGS_KEY``, for example
``{"enabled": true, "channel": "stable", "time": "04:00"}``. Without a stored value, the update runs each day at 04:00
in the time zone of the schedule and installs from the stable channel. A new channel acts at the next update.

An update replaces the folder that each child process imports yt-dlp from, so it holds the sync lock: no run, no
session check and no other child works while the folder changes. A scheduled update waits for the lock up to
``SCHEDULED_WAIT_S``, as a scheduled run does, and an update on request does not wait. The result of the last update
lives under ``LAST_UPDATE_KEY``. An update sends the ``ytdlp_update`` notification only when the version changed or the
update failed. This module imports neither NiceGUI nor yt-dlp, so the process of the service runs it.
"""

import datetime
import functools
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager import notify
from youtube_automanager.db.models import Setting, utc_now
from youtube_automanager.runner import SCHEDULED_WAIT_S, RunAlreadyActive, SyncRunner, error_text_from_tail
from youtube_automanager.youtube.ytdlp_updater import Channel, UpdateResult, image_version

LOG = logging.getLogger(__name__)

SETTINGS_KEY = "ytdlp_update"
LAST_UPDATE_KEY = "ytdlp_last_update"
CHANNELS: tuple[Channel, ...] = get_args(Channel)
DEFAULT_TIME = datetime.time(4, 0)
# Only ASCII digits: ``\d`` and ``int`` also take the digits of other scripts.
_TIME = re.compile(r"([0-9]{1,2}):([0-9]{2})")

# Installs the latest release of a channel into a folder, as ``ytdlp_updater.update`` does.
type Install = Callable[[Path, Channel], UpdateResult]


def parse_time(text: str) -> datetime.time:
    """Return the time of the day of ``text``, written as H:MM or HH:MM. Raise ValueError for another text."""
    match = _TIME.fullmatch(text.strip())
    if match is not None:
        hour, minute = int(match[1]), int(match[2])
        if hour < 24 and minute < 60:
            return datetime.time(hour, minute)
    raise ValueError(f"A time of the day is H:MM or HH:MM from 0:00 to 23:59, got {text!r}")


def time_text(at: datetime.time) -> str:
    """Return the time of the day ``at`` as HH:MM."""
    return f"{at:%H:%M}"


@dataclass(frozen=True)
class UpdateSettings:
    """When the daily update runs, and which channel each update installs from."""

    enabled: bool = True
    channel: Channel = "stable"
    at: datetime.time = DEFAULT_TIME

    def __post_init__(self) -> None:
        if self.channel not in CHANNELS:
            raise ValueError(f"Unknown yt-dlp channel {self.channel!r}: use one of {', '.join(CHANNELS)}")

    @classmethod
    def from_value(cls, value: object) -> UpdateSettings:
        """Return the settings of a stored JSON value. A missing key gets its default.

        Raise TypeError for a value of a wrong JSON type, and ValueError for another wrong value.
        """
        if not isinstance(value, Mapping):
            raise TypeError(f"The yt-dlp update settings are not a JSON object: {value!r}")
        default = cls()
        enabled = value.get("enabled", default.enabled)
        if not isinstance(enabled, bool):
            raise TypeError(f"The value of enabled is not true or false: {enabled!r}")
        at = value.get("time", time_text(default.at))
        if not isinstance(at, str):
            raise TypeError(f"The value of time is not a text: {at!r}")
        return cls(enabled, value.get("channel", default.channel), parse_time(at))

    def to_value(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "channel": self.channel, "time": time_text(self.at)}


@dataclass(frozen=True)
class LastUpdate:
    """The last update that ran: its time, its channel, the versions before and after it, and its error.

    ``old_version`` is None when the data folder held no yt-dlp. ``new_version`` is the latest version of the channel,
    or None when PyPI gave no answer.
    """

    at: datetime.datetime
    ok: bool
    channel: Channel
    old_version: str | None
    new_version: str | None
    error: str | None = None

    @property
    def changed(self) -> bool:
        """True when the update put another version into the data folder."""
        return self.ok and self.old_version != self.new_version

    @classmethod
    def from_result(cls, result: UpdateResult, at: datetime.datetime) -> LastUpdate:
        # A uv error can be long, so only its end stays, as for the error of a run.
        error = error_text_from_tail(result.error.splitlines()) if result.error else None
        return cls(at, result.ok, result.channel, result.old_version, result.new_version, error)

    @classmethod
    def from_value(cls, value: object) -> LastUpdate:
        """Return the record of a stored JSON value.

        Raise TypeError for a value of a wrong JSON type, and ValueError for another wrong value.
        """
        if not isinstance(value, Mapping):
            raise TypeError(f"The last yt-dlp update is not a JSON object: {value!r}")
        try:
            at = datetime.datetime.fromisoformat(value["at"])
            fields = (value["ok"], value["channel"], value["old_version"], value["new_version"], value["error"])
        except (KeyError, TypeError) as error:
            raise ValueError(f"The last yt-dlp update lacks a value or has a wrong one: {error!r}") from error
        ok, channel, old_version, new_version, error_text = fields
        if at.tzinfo is None or not isinstance(ok, bool) or channel not in CHANNELS:
            raise ValueError(f"The last yt-dlp update has a wrong value: {dict(value)!r}")
        if not all(text is None or isinstance(text, str) for text in (old_version, new_version, error_text)):
            raise ValueError(f"The last yt-dlp update has a version or an error that is not a text: {dict(value)!r}")
        return cls(at, ok, channel, old_version, new_version, error_text)

    def to_value(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "ok": self.ok,
            "channel": self.channel,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "error": self.error,
        }


def _replace_setting(session: Session, key: str, value: Any) -> None:
    # A new row, because the change tracking compares a JSON value with ``==``, so a change from ``1`` to ``true`` in
    # place would write nothing.
    session.execute(delete(Setting).where(Setting.key == key))
    session.add(Setting(key=key, value=value))


def read_settings(sessions: sessionmaker[Session]) -> UpdateSettings:
    """Read the settings of the update. A missing or wrong stored value gives the defaults. This call blocks."""
    with sessions() as session:
        row = session.get(Setting, SETTINGS_KEY)
        value = None if row is None else row.value
    if row is None:
        return UpdateSettings()
    try:
        return UpdateSettings.from_value(value)
    except (ValueError, TypeError) as error:
        LOG.warning("The setting %s is not valid, so the yt-dlp update uses its defaults: %s", SETTINGS_KEY, error)
        return UpdateSettings()


def save_settings(sessions: sessionmaker[Session], settings: UpdateSettings) -> None:
    """Store the settings of the update. The caller tells the scheduler about a new time. This call blocks."""
    with sessions.begin() as session:
        _replace_setting(session, SETTINGS_KEY, settings.to_value())


def read_last_update(sessions: sessionmaker[Session]) -> LastUpdate | None:
    """Read the last update that ran, or None when no update ran or its record is not valid. This call blocks."""
    with sessions() as session:
        row = session.get(Setting, LAST_UPDATE_KEY)
        value = None if row is None else row.value
    if row is None:
        return None
    try:
        return LastUpdate.from_value(value)
    except (ValueError, TypeError) as error:
        LOG.warning("The setting %s is not valid: %s", LAST_UPDATE_KEY, error)
        return None


def _save_last_update(sessions: sessionmaker[Session], record: LastUpdate) -> None:
    with sessions.begin() as session:
        _replace_setting(session, LAST_UPDATE_KEY, record.to_value())


class YtdlpUpdates:
    """Updates the yt-dlp of the data folder ``ytdlp_dir`` while it holds the sync lock of ``runner``.

    ``install`` installs the latest release of a channel, and ``send`` sends a notification, by default to the targets
    of the database.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        runner: SyncRunner,
        ytdlp_dir: Path,
        *,
        install: Install,
        send: Callable[[notify.Notification], object] | None = None,
        clock: Callable[[], datetime.datetime] = utc_now,
    ) -> None:
        self._sessions = sessions
        self._runner = runner
        self._ytdlp_dir = ytdlp_dir
        self._install = install
        self._send = send or functools.partial(notify.send_to_stored_targets, sessions)
        self._clock = clock

    def run_scheduled(self) -> None:
        """The job of the daily update. It waits up to SCHEDULED_WAIT_S for the sync lock, as a scheduled run does.

        A run, a session check, a refresh of the account lists or an apply of a rule keeps the lock for a while, so the
        update starts late, but it is not lost to them. A stop of the service ends the wait.
        """
        try:
            with self._runner.lock_held(timeout=SCHEDULED_WAIT_S):
                # The settings can change during the wait, so the update reads them only now.
                settings = read_settings(self._sessions)
                if not settings.enabled:
                    LOG.info("The scheduled yt-dlp update is off")
                    return
                record = self._update(settings.channel)
        except RunAlreadyActive:
            LOG.warning(
                "The scheduled yt-dlp update did not start, because another task kept the sync lock or the service stops"
            )
            return
        self._tell(record)

    def update_now(self) -> LastUpdate | None:
        """Update now from the stored channel, and return the result. This call blocks until the update ends.

        Return None and change nothing when a run, a session check or another child holds the sync lock.
        """
        try:
            with self._runner.lock_held(timeout=0):
                record = self._update(read_settings(self._sessions).channel)
        except RunAlreadyActive:
            return None
        self._tell(record)
        return record

    def _update(self, channel: Channel) -> LastUpdate:
        """Install the latest release of ``channel`` and store the result. The caller holds the sync lock."""
        LOG.info("The yt-dlp update from the %s channel starts", channel)
        record = LastUpdate.from_result(self._install(self._ytdlp_dir, channel), self._clock())
        _save_last_update(self._sessions, record)
        return record

    def _tell(self, record: LastUpdate) -> None:
        """Send the result of an update when the version changed or the update failed. Never raise."""
        if record.ok and not record.changed:
            return
        notification = notify.ytdlp_update(
            record.old_version,
            record.new_version,
            record.ok,
            record.error,
            image_version=image_version() if record.old_version is None else None,
        )
        try:
            self._send(notification)
        except Exception as error:  # noqa: BLE001
            # A failed notification must never change the result of the update.
            LOG.warning("Cannot send the result of the yt-dlp update: %s", type(error).__name__)
