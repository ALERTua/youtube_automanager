import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, make_url, text
from sqlalchemy.exc import IntegrityError

from youtube_automanager.db import alembic_config, upgrade_to_head
from youtube_automanager.db.models import Base

ROOT = Path(__file__).parent.parent

TABLES = {
    "account",
    "cookie_version",
    "channel",
    "playlist",
    "rule",
    "rule_channel",
    "seen_video",
    "added_video",
    "schedule",
    "run",
    "run_action",
    "notification_target",
    "setting",
}

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")


def head_revision():
    return ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()


def table_names(url):
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def current_revision(url):
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def model_differences(url):
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "compare_server_default": True},
            )
            return compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()


@pytest.fixture
def sqlite_url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.sqlite'}"


@pytest.fixture(scope="session")
def postgres_database(worker_id):
    """A database of its own on the server of TEST_POSTGRES_URL for each worker of a parallel run, and its URL.

    Each test empties its database before and after it, so two workers of ``pytest -n`` must not share one.
    """
    server = make_url(POSTGRES_URL)
    name = f"youtube_automanager_test_{worker_id}"
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        yield server.set(database=name).render_as_string(hide_password=False)
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


@pytest.fixture
def postgres_url(postgres_database):
    """The database of the worker on the server of TEST_POSTGRES_URL, emptied of the tables of the service before and
    after the test."""
    config = alembic_config(postgres_database)
    command.downgrade(config, "base")
    yield postgres_database
    command.downgrade(config, "base")
    engine = create_engine(postgres_database)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    finally:
        engine.dispose()


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres",
            marks=pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set"),
        ),
    ]
)
def database_url(request):
    return request.getfixturevalue(f"{request.param}_url")


def test_upgrade_creates_each_table(database_url):
    upgrade_to_head(database_url)

    assert table_names(database_url) == TABLES | {"alembic_version"}
    assert current_revision(database_url) == head_revision()


def test_downgrade_base_removes_each_table(database_url):
    upgrade_to_head(database_url)

    command.downgrade(alembic_config(database_url), "base")

    assert table_names(database_url) == {"alembic_version"}
    assert current_revision(database_url) is None


def test_second_upgrade_changes_nothing(database_url):
    upgrade_to_head(database_url)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO setting (key, value) VALUES ('kept', '1')"))

        upgrade_to_head(database_url)

        with engine.connect() as connection:
            assert connection.execute(text("SELECT key FROM setting")).scalars().all() == ["kept"]
    finally:
        engine.dispose()
    assert current_revision(database_url) == head_revision()


def test_models_agree_with_migrations(database_url):
    upgrade_to_head(database_url)

    assert model_differences(database_url) == []


def check_constraints(url):
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        return {
            (table, check["name"], check["sqltext"])
            for table in inspector.get_table_names()
            for check in inspector.get_check_constraints(table)
        }
    finally:
        engine.dispose()


def test_check_constraints_of_models_and_migrations_agree(sqlite_url, tmp_path):
    """Alembic does not compare CHECK constraints, so this test compares the migrated database with `create_all`."""
    upgrade_to_head(sqlite_url)
    created_url = f"sqlite:///{tmp_path / 'created.sqlite'}"
    engine = create_engine(created_url)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    migrated = check_constraints(sqlite_url)

    assert migrated == check_constraints(created_url)
    assert len(migrated) == 9


def test_upgrade_keeps_stored_cookies_as_the_first_cookie_version_and_downgrade_keeps_them_in_the_account(
    database_url,
):
    config = alembic_config(database_url)
    command.upgrade(config, "bce2013af8cf")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO account (cookies, session_state) VALUES (:cookies, 'ok')"),
                {"cookies": b"encrypted cookies"},
            )

        command.upgrade(config, "head")

        with engine.connect() as connection:
            versions = connection.execute(text("SELECT origin, state, cookies FROM cookie_version")).all()
        assert [tuple(row) for row in versions] == [("upload", "unchecked", b"encrypted cookies")]

        command.downgrade(config, "bce2013af8cf")

        with engine.connect() as connection:
            assert connection.execute(text("SELECT cookies FROM account")).scalars().all() == [b"encrypted cookies"]
    finally:
        engine.dispose()
    assert "cookie_version" not in table_names(database_url)


def test_the_rule_apply_trigger_keeps_the_runs_and_their_actions_and_downgrade_makes_an_apply_a_manual_run(
    database_url,
):
    config = alembic_config(database_url)
    command.upgrade(config, "5f2c9a7d1e43")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    'INSERT INTO run (id, started_at, status, "trigger", added_count, seen_count) '
                    "VALUES (1, CURRENT_TIMESTAMP, 'succeeded', 'schedule', 2, 3)"
                )
            )
            connection.execute(
                text("INSERT INTO run_action (run_id, at, action) VALUES (1, CURRENT_TIMESTAMP, 'added')")
            )

        command.upgrade(config, "head")

        with engine.begin() as connection:
            connection.execute(
                text(
                    'INSERT INTO run (id, started_at, status, "trigger", added_count, seen_count) '
                    "VALUES (2, CURRENT_TIMESTAMP, 'running', 'rule_apply', 0, 0)"
                )
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    'INSERT INTO run (id, started_at, status, "trigger", added_count, seen_count) '
                    "VALUES (3, CURRENT_TIMESTAMP, 'running', 'another', 0, 0)"
                )
            )
        with engine.connect() as connection:
            assert connection.execute(text("SELECT run_id, action FROM run_action")).all() == [(1, "added")]

        command.downgrade(config, "5f2c9a7d1e43")

        with engine.connect() as connection:
            runs = connection.execute(text('SELECT id, "trigger", added_count FROM run ORDER BY id')).all()
            assert runs == [(1, "schedule", 2), (2, "manual", 0)]
            assert connection.execute(text("SELECT run_id FROM run_action")).scalars().all() == [1]
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    'INSERT INTO run (id, started_at, status, "trigger", added_count, seen_count) '
                    "VALUES (3, CURRENT_TIMESTAMP, 'running', 'rule_apply', 0, 0)"
                )
            )
    finally:
        engine.dispose()


def test_comparison_finds_a_column_that_the_migrations_lack(sqlite_url):
    upgrade_to_head(sqlite_url)
    engine = create_engine(sqlite_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE setting DROP COLUMN value"))
    finally:
        engine.dispose()

    differences = model_differences(sqlite_url)

    assert [(diff[0], diff[3].name) for diff in differences] == [("add_column", "value")]


def run_alembic(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )


def test_command_line_upgrades_and_downgrades_the_url_of_the_x_argument(sqlite_url):
    upgraded = run_alembic("-x", f"url={sqlite_url}", "upgrade", "head")

    assert upgraded.returncode == 0, upgraded.stderr
    assert table_names(sqlite_url) == TABLES | {"alembic_version"}

    downgraded = run_alembic("-x", f"url={sqlite_url}", "downgrade", "base")

    assert downgraded.returncode == 0, downgraded.stderr
    assert table_names(sqlite_url) == {"alembic_version"}


def test_command_line_without_a_url_uses_the_database_of_the_settings(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "DB_FILENAME": "from_settings.sqlite"}

    result = run_alembic("upgrade", "head", env=env)

    assert result.returncode == 0, result.stderr
    assert table_names(f"sqlite:///{tmp_path / 'from_settings.sqlite'}") == TABLES | {"alembic_version"}
