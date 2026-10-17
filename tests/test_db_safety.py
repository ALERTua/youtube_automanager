import sqlite3
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import event, text

from youtube_automanager.db import alembic_config, make_engine
from youtube_automanager.db.backup import BackupError, backup_before_upgrade, current_revision
from youtube_automanager.web.app import _prepare_database

NOW = datetime(2026, 10, 17, 9, 30, 5, tzinfo=UTC)


class Connected(Exception):
    """Stops a connect before it reaches the network."""


def head_revision():
    return ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "data" / "service.sqlite"


@pytest.fixture
def url(db_path):
    db_path.parent.mkdir()
    return f"sqlite:///{db_path}"


@pytest.fixture
def old_url(url):
    """A database one migration behind the newest schema."""
    config = alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "-1")
    return url


def test_sqlite_waits_30_seconds_for_a_lock(url):
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 30000
    finally:
        engine.dispose()


@pytest.mark.parametrize("scheme", ["postgresql", "postgresql+psycopg"])
def test_postgresql_waits_10_seconds_for_its_server(scheme):
    engine = make_engine(f"{scheme}://user:fake@127.0.0.1:1/service")
    seen = []

    @event.listens_for(engine, "do_connect")
    def record(dialect, connection_record, cargs, cparams):
        seen.append(cparams)
        raise Connected

    try:
        with pytest.raises(Connected):
            engine.connect()
    finally:
        engine.dispose()

    assert seen[0]["connect_timeout"] == 10


def test_a_pending_migration_copies_the_database_first(old_url, db_path):
    old_revision = current_revision(old_url)

    copy = backup_before_upgrade(old_url, NOW)

    assert copy == db_path.parent / "backups" / f"service-{old_revision}-20261017T093005Z.sqlite"
    assert current_revision(f"sqlite:///{copy}") == old_revision


def test_the_start_copies_the_database_before_the_migration(old_url, db_path):
    old_revision = current_revision(old_url)

    _prepare_database(old_url)

    (copy,) = (db_path.parent / "backups").iterdir()
    assert copy.name.startswith(f"service-{old_revision}-")
    assert current_revision(f"sqlite:///{copy}") == old_revision
    assert current_revision(old_url) == head_revision()


def test_a_fresh_database_gets_no_copy(url, db_path):
    assert backup_before_upgrade(url, NOW) is None

    _prepare_database(url)

    assert current_revision(url) == head_revision()
    assert not (db_path.parent / "backups").exists()


def test_a_file_without_a_revision_gets_no_copy(url, db_path):
    sqlite3.connect(db_path).close()

    assert backup_before_upgrade(url, NOW) is None
    assert not (db_path.parent / "backups").exists()


def test_a_database_at_the_head_gets_no_copy(url, db_path):
    command.upgrade(alembic_config(url), "head")

    _prepare_database(url)

    assert not (db_path.parent / "backups").exists()


def test_postgresql_gets_no_copy():
    # The URL names no server that answers, so a connect would fail.
    assert backup_before_upgrade("postgresql://user:fake@127.0.0.1:1/service", NOW) is None


def test_only_the_newest_three_copies_stay(old_url, db_path):
    folder = db_path.parent / "backups"
    folder.mkdir()
    old = [folder / f"service-abc123-2026010{day}T000000Z.sqlite" for day in range(1, 5)]
    others = [folder / "service-other-abc123-20250101T000000Z.sqlite", folder / "notes.txt"]
    for path in old + others:
        path.write_bytes(b"")

    copy = backup_before_upgrade(old_url, NOW)

    assert sorted(folder.iterdir()) == sorted([old[2], old[3], copy, *others])


def test_a_failed_copy_stops_the_start_before_the_migration(old_url, db_path):
    old_revision = current_revision(old_url)
    # A file where the folder of the copies must be makes the copy fail.
    (db_path.parent / "backups").write_bytes(b"")

    with pytest.raises(BackupError, match="backups"):
        _prepare_database(old_url)

    assert current_revision(old_url) == old_revision
