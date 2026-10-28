import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DataError, IntegrityError

from youtube_automanager.db import alembic_config, make_engine, make_session_factory, upgrade_to_head
from youtube_automanager.db.models import Base
from youtube_automanager.rules_store import AddedCounts, read_added_counts

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
    "request_budget",
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


@pytest.fixture
def postgres_url(postgres_database):
    """A database of the test on the server of TEST_POSTGRES_URL without the tables of the service.

    The database comes with the newest schema, so the fixture downgrades it to the base and drops the revision table.
    The database is dropped after the test, so it needs no cleanup.
    """
    command.downgrade(alembic_config(postgres_database), "base")
    engine = create_engine(postgres_database)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    finally:
        engine.dispose()
    return postgres_database


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
    assert len(migrated) == 11


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
        # The column of the older schema is as long as its longest value, so PostgreSQL refuses the longer value by its
        # length before the CHECK constraint, and SQLite refuses it by the CHECK constraint.
        with pytest.raises((IntegrityError, DataError)), engine.begin() as connection:
            connection.execute(
                text(
                    'INSERT INTO run (id, started_at, status, "trigger", added_count, seen_count) '
                    "VALUES (3, CURRENT_TIMESTAMP, 'running', 'rule_apply', 0, 0)"
                )
            )
    finally:
        engine.dispose()


def test_the_catch_up_day_of_a_rule_keeps_the_rules_and_their_channels_in_both_directions(database_url):
    config = alembic_config(database_url)
    command.upgrade(config, "8d3e61b2c4a7")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO playlist (playlist_id, title, is_own) VALUES ('WL', 'Watch later', true)")
            )
            connection.execute(
                text("INSERT INTO channel (channel_id, title, is_subscribed) VALUES ('UCa', 'Alpha', true)")
            )
            connection.execute(
                text(
                    "INSERT INTO rule (id, name, enabled, position, condition, target_playlist_id, no_shorts, "
                    "created_at, updated_at) VALUES (1, 'all', true, 1, '{}', 'WL', false, CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP)"
                )
            )
            connection.execute(text("INSERT INTO rule_channel (rule_id, channel_id) VALUES (1, 'UCa')"))

        command.upgrade(config, "head")

        with engine.begin() as connection:
            # A stored rule gets no catch-up.
            assert connection.execute(text("SELECT id, catch_up_since FROM rule")).all() == [(1, None)]
            connection.execute(text("UPDATE rule SET catch_up_since = '2026-10-05' WHERE id = 1"))

        command.downgrade(config, "8d3e61b2c4a7")

        assert "catch_up_since" not in {column["name"] for column in inspect(engine).get_columns("rule")}
        with engine.connect() as connection:
            assert connection.execute(text("SELECT id, name FROM rule")).all() == [(1, "all")]
            assert connection.execute(text("SELECT rule_id, channel_id FROM rule_channel")).all() == [(1, "UCa")]
    finally:
        engine.dispose()


BEFORE_REQUEST_BUDGET = "c41e7a9d3b58"
REQUEST_BUDGET = "e6b02f4a8d17"


def test_the_request_budget_table_starts_empty_with_one_row_at_most_and_downgrade_removes_it(database_url):
    config = alembic_config(database_url)
    command.upgrade(config, BEFORE_REQUEST_BUDGET)

    command.upgrade(config, REQUEST_BUDGET)

    engine = create_engine(database_url)
    try:
        columns = {column["name"]: column["nullable"] for column in inspect(engine).get_columns("request_budget")}
        assert columns == {"id": False, "tokens": False, "refilled_at": False, "last_answer_at": True}
        with engine.begin() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM request_budget")).scalar_one() == 0
            connection.execute(
                text("INSERT INTO request_budget (id, tokens, refilled_at) VALUES (1, 14.5, CURRENT_TIMESTAMP)")
            )
            assert connection.execute(text("SELECT tokens FROM request_budget")).scalar_one() == 14.5
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text("INSERT INTO request_budget (id, tokens, refilled_at) VALUES (2, 15, CURRENT_TIMESTAMP)")
            )

        command.downgrade(config, BEFORE_REQUEST_BUDGET)
    finally:
        engine.dispose()

    assert "request_budget" not in table_names(database_url)
    assert current_revision(database_url) == BEFORE_REQUEST_BUDGET


BREAKER_COLUMNS = {"breaker_until", "breaker_level", "breaker_reason", "breaker_opened_at"}


def insert_target(connection, target_id: int, events: list[str]) -> None:
    connection.execute(
        text(
            "INSERT INTO notification_target (id, name, url, min_level, events, enabled) "
            "VALUES (:id, :name, 'json://localhost', 0, :events, true)"
        ),
        {"id": target_id, "name": f"target {target_id}", "events": json.dumps(events)},
    )


def stored_events(connection) -> dict[int, list[str]]:
    rows = connection.execute(text("SELECT id, events FROM notification_target ORDER BY id")).all()
    return {row[0]: row[1] if isinstance(row[1], list) else json.loads(row[1]) for row in rows}


def test_the_breaker_columns_keep_the_budget_and_each_stored_target_gets_the_breaker_event_once(database_url):
    config = alembic_config(database_url)
    command.upgrade(config, REQUEST_BUDGET)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO request_budget (id, tokens, refilled_at) VALUES (1, 14.5, CURRENT_TIMESTAMP)")
            )
            insert_target(connection, 1, ["run_failed"])
            insert_target(connection, 2, [])
            # A target that has the event already, for example after a downgrade that the user undid by hand.
            insert_target(connection, 3, ["request_breaker", "run_summary"])

        command.upgrade(config, "head")

        columns = {column["name"]: column["nullable"] for column in inspect(engine).get_columns("request_budget")}
        assert {name: columns.get(name) for name in BREAKER_COLUMNS} == dict.fromkeys(BREAKER_COLUMNS, True)
        with engine.begin() as connection:
            assert connection.execute(text("SELECT tokens, breaker_reason FROM request_budget")).all() == [(14.5, None)]
            assert stored_events(connection) == {
                1: ["run_failed", "request_breaker"],
                2: ["request_breaker"],
                3: ["request_breaker", "run_summary"],
            }
            connection.execute(
                text(
                    "UPDATE request_budget SET breaker_reason = 'bot_check', breaker_level = 2, "
                    "breaker_until = CURRENT_TIMESTAMP, breaker_opened_at = CURRENT_TIMESTAMP"
                )
            )
        # The value is shorter than the column, so each database refuses it by the CHECK constraint.
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text("UPDATE request_budget SET breaker_reason = 'captcha'"))

        command.downgrade(config, REQUEST_BUDGET)

        assert not BREAKER_COLUMNS & {column["name"] for column in inspect(engine).get_columns("request_budget")}
        with engine.connect() as connection:
            assert connection.execute(text("SELECT tokens FROM request_budget")).scalars().all() == [14.5]
            assert stored_events(connection) == {1: ["run_failed"], 2: [], 3: ["run_summary"]}
    finally:
        engine.dispose()
    assert current_revision(database_url) == REQUEST_BUDGET


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


# The rule ids on SQLite.

BEFORE_RULE_AUTOINCREMENT = "3a7c5e9b2d14"
INSERT_RULE = (
    "INSERT INTO rule (id, name, enabled, position, condition, target_playlist_id, no_shorts, created_at, updated_at) "
    "VALUES (:id, :name, true, 1, '{}', 'WL', false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
)


def rule_table_sql(url) -> str:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT sql FROM sqlite_master WHERE name = 'rule'")).scalar_one()
    finally:
        engine.dispose()


def seed_rules_before_the_autoincrement(url) -> None:
    """Two rules, the channel and the added video of rule 2, and the history of rule 5, which went before."""
    command.upgrade(alembic_config(url), BEFORE_RULE_AUTOINCREMENT)
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO playlist (playlist_id, title, is_own) VALUES ('WL', 'Later', true)"))
            connection.execute(text("INSERT INTO channel (channel_id, title, is_subscribed) VALUES ('UCa', 'A', true)"))
            for rule_id in (1, 2):
                connection.execute(text(INSERT_RULE), {"id": rule_id, "name": f"rule {rule_id}"})
            connection.execute(text("INSERT INTO rule_channel (rule_id, channel_id) VALUES (2, 'UCa')"))
            connection.execute(
                text(
                    "INSERT INTO added_video (playlist_id, video_id, rule_id, added_at) "
                    "VALUES ('WL', 'v2', 2, CURRENT_TIMESTAMP)"
                )
            )
            connection.execute(
                text(
                    'INSERT INTO run (id, started_at, status, "trigger", added_count, seen_count) '
                    "VALUES (1, CURRENT_TIMESTAMP, 'succeeded', 'manual', 2, 2)"
                )
            )
            for rule_id in (2, 5):
                connection.execute(
                    text(
                        "INSERT INTO run_action (run_id, at, action, video_id, rule_id) "
                        "VALUES (1, CURRENT_TIMESTAMP, 'added', 'v', :rule_id)"
                    ),
                    {"rule_id": rule_id},
                )
    finally:
        engine.dispose()


def test_after_the_upgrade_a_new_rule_never_takes_the_id_of_a_deleted_rule(sqlite_url):
    seed_rules_before_the_autoincrement(sqlite_url)

    upgrade_to_head(sqlite_url)

    assert "AUTOINCREMENT" in rule_table_sql(sqlite_url)
    # The engine of the service turns the foreign keys of SQLite on.
    engine = make_engine(sqlite_url)
    try:
        with engine.begin() as connection:
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            connection.execute(text("DELETE FROM rule WHERE id = 2"))
            # The foreign keys to the new table still act: the channel of the rule goes, its added video stays.
            assert connection.execute(text("SELECT rule_id FROM rule_channel")).all() == []
            assert connection.execute(text("SELECT video_id, rule_id FROM added_video")).all() == [("v2", None)]
            connection.execute(text(INSERT_RULE.replace(":id", "NULL")), {"name": "new"})
            # The history still holds rule 5, which went before the upgrade, so the new rule starts above it.
            new_id = connection.execute(text("SELECT id FROM rule WHERE name = 'new'")).scalar_one()
            assert new_id == 6
            connection.execute(text("DELETE FROM rule WHERE id = 6"))
            connection.execute(text(INSERT_RULE.replace(":id", "NULL")), {"name": "newer"})
            assert connection.execute(text("SELECT id FROM rule WHERE name = 'newer'")).scalar_one() == 7
        # The new rule has no videos of the deleted rules.
        assert read_added_counts(make_session_factory(engine), datetime.now(UTC)) == {
            2: AddedCounts(1, 1),
            5: AddedCounts(1, 1),
        }
    finally:
        engine.dispose()


def test_the_first_rule_of_an_empty_database_gets_the_id_1(sqlite_url):
    upgrade_to_head(sqlite_url)
    engine = make_engine(sqlite_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO playlist (playlist_id, title, is_own) VALUES ('WL', 'Later', true)"))
            connection.execute(text(INSERT_RULE.replace(":id", "NULL")), {"name": "first"})
            assert connection.execute(text("SELECT id FROM rule")).scalar_one() == 1
    finally:
        engine.dispose()


def test_the_rule_autoincrement_keeps_the_rules_and_their_links_in_both_directions(sqlite_url):
    seed_rules_before_the_autoincrement(sqlite_url)
    config = alembic_config(sqlite_url)

    upgrade_to_head(sqlite_url)
    command.downgrade(config, BEFORE_RULE_AUTOINCREMENT)

    assert "AUTOINCREMENT" not in rule_table_sql(sqlite_url)
    engine = create_engine(sqlite_url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT id, name FROM rule ORDER BY id")).all() == [
                (1, "rule 1"),
                (2, "rule 2"),
            ]
            assert connection.execute(text("SELECT rule_id, channel_id FROM rule_channel")).all() == [(2, "UCa")]
            assert connection.execute(text("SELECT rule_id FROM added_video")).all() == [(2,)]
            assert connection.execute(text("SELECT name FROM sqlite_sequence WHERE name = 'rule'")).all() == []
    finally:
        engine.dispose()

    upgrade_to_head(sqlite_url)

    assert "AUTOINCREMENT" in rule_table_sql(sqlite_url)
    assert current_revision(sqlite_url) == head_revision()
    assert model_differences(sqlite_url) == []
