"""A copy of a SQLite database before a migration changes its schema.

The service migrates its database at each start. Before a pending migration, it copies the SQLite file into the
folder ``backups`` next to it, and it keeps the newest copies. A fresh database, a database at the newest schema and a
PostgreSQL database get no copy.
"""

import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, make_url, pool

from youtube_automanager.db import SQLITE_BUSY_TIMEOUT_S, alembic_config

LOG = logging.getLogger(__name__)

BACKUP_DIRNAME = "backups"
KEEP_BACKUPS = 3
TIME_FORMAT = "%Y%m%dT%H%M%SZ"


class BackupError(RuntimeError):
    """The copy before a migration failed, so the service must not migrate."""


def sqlite_path(url: str) -> Path | None:
    """The path of the SQLite file of ``url``, or None for another database or a database in memory."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.database == ":memory:":
        return None
    return Path(parsed.database)


def current_revision(url: str) -> str | None:
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def _safe(revision: str) -> str:
    # The pattern of the old copies takes the revision as one word, so the name of another database never matches.
    return re.sub(r"\W", "_", revision)


def _copies(folder: Path, stem: str) -> list[tuple[str, Path]]:
    pattern = re.compile(rf"{re.escape(stem)}-\w+-(\d{{8}}T\d{{6}}Z)\.sqlite")
    found = [(match[1], path) for path in folder.iterdir() if (match := pattern.fullmatch(path.name))]
    return sorted(found)


def _remove_old_copies(folder: Path, stem: str) -> None:
    copies = _copies(folder, stem)
    for _, path in copies[: max(len(copies) - KEEP_BACKUPS, 0)]:
        try:
            path.unlink()
        except OSError as error:
            LOG.warning("Cannot delete the old database copy %s: %s", path, error)


def backup_before_upgrade(url: str, now: datetime | None = None) -> Path | None:
    """Copy the SQLite database of ``url`` when a migration is pending, and return the path of the copy.

    Return None when the database needs no copy. Raise ``BackupError`` when the copy fails.
    """
    path = sqlite_path(url)
    if path is None or not path.is_file():
        return None
    revision = current_revision(url)
    if revision is None:
        return None
    heads = ScriptDirectory.from_config(alembic_config(url)).get_heads()
    if revision in heads:
        return None
    folder = path.parent / BACKUP_DIRNAME
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime(TIME_FORMAT)
    target = folder / f"{path.stem}-{_safe(revision)}-{stamp}.sqlite"
    LOG.info("Copying the database to %s before the migration from the revision %s", target, revision)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_S)
        try:
            connection.execute("VACUUM INTO ?", (str(target),))
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as error:
        raise BackupError(
            f"Cannot copy the database to the folder {folder} before the migration: {error}. The service did not "
            f"migrate the database. Make the folder writable, or free some disk space, and start the service again."
        ) from error
    _remove_old_copies(folder, path.stem)
    return target
