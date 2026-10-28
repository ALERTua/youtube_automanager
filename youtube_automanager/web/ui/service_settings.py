"""The settings of the service on the settings page: the processing mode of the sync and the cookie rotation.

Both live in the ``setting`` table as JSON values. A save replaces the row: the change tracking compares a JSON value
with Python ``==``, so a change from ``1`` to ``true`` would write nothing. The sync engine reads the processing mode
at the start of each run, and the rotation job reads its setting at each turn, so a change needs no restart. This
module imports no NiceGUI, so its functions have tests of their own. Each function blocks.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from youtube_automanager.db.models import Setting
from youtube_automanager.engine.modes import MIN_CHECKED, Processing, ProcessingMode
from youtube_automanager.i18n import N_, _
from youtube_automanager.rotation import ROTATION_KEY, rotation_enabled

# The key of ``engine.sync``. That module imports yt-dlp, which the process of the service never imports.
PROCESSING_MODE_KEY = "processing_mode"
# The count of mode NEWEST that the form offers when the stored mode has none.
DEFAULT_NEWEST_COUNT = 30
# One request to YouTube gives up to this many entries of a source.
ENTRIES_PER_REQUEST = 100

MODE_LABELS = {
    ProcessingMode.STOP_AT_SEEN: N_("Stop at a seen video (default)"),
    ProcessingMode.NEWEST: N_("A fixed number of the newest videos"),
    ProcessingMode.MARK_DATE: N_("Stop at the date mark"),
}
# Each text can hold {count}, the number of the newest videos that mode STOP_AT_SEEN always reads.
MODE_HELP = {
    ProcessingMode.STOP_AT_SEEN: N_(
        "Each run reads a source from its newest video down to the first video that an earlier run saw, and always "
        "at least the {count} newest videos."
    ),
    ProcessingMode.NEWEST: N_(
        "Each run reads the given number of the newest videos of a source, and checks each one that an earlier run "
        "did not see."
    ),
    ProcessingMode.MARK_DATE: N_(
        "Each run reads a source down to the upload day of the newest video that an earlier run saw."
    ),
}


@dataclass(frozen=True)
class StoredMode:
    """The stored processing mode. ``processing`` is None when the stored value is not valid, and ``error`` says why."""

    processing: Processing | None
    error: str | None = None


def mode_help(mode: ProcessingMode) -> str:
    """Return the explanation of ``mode`` in the language of this moment."""
    return _(MODE_HELP[mode]).format(count=MIN_CHECKED)


def replace_setting(session: Session, key: str, value: Any) -> None:
    """Write ``value`` under ``key`` as a new row, so a value of another JSON type always goes into the database."""
    session.execute(delete(Setting).where(Setting.key == key))
    session.add(Setting(key=key, value=value))


def read_processing(sessions: sessionmaker[Session]) -> StoredMode:
    """Read the processing mode. Without a stored value, the mode is the default, as for the sync engine."""
    with sessions() as session:
        row = session.get(Setting, PROCESSING_MODE_KEY)
        value = None if row is None else row.value
    if row is None:
        return StoredMode(Processing())
    try:
        return StoredMode(Processing.from_value(value))
    except ValueError as error:
        return StoredMode(None, str(error))


def save_processing(sessions: sessionmaker[Session], processing: Processing) -> None:
    with sessions.begin() as session:
        replace_setting(session, PROCESSING_MODE_KEY, processing.to_value())


def read_rotation(sessions: sessionmaker[Session]) -> bool:
    """Read the setting of the cookie rotation as the rotation job reads it."""
    with sessions() as session:
        return rotation_enabled(session)


def save_rotation(sessions: sessionmaker[Session], enabled: bool) -> None:
    """Store the setting of the cookie rotation as a JSON true or false."""
    with sessions.begin() as session:
        replace_setting(session, ROTATION_KEY, bool(enabled))
