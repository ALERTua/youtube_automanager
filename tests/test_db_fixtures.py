"""The tests of the database fixtures: each test gets a database with the schema and no rows, on each database."""

import pytest
from sqlalchemy import make_url, select

from youtube_automanager.db.models import Setting


def stored_keys(sessions):
    with sessions() as session:
        return session.scalars(select(Setting.key)).all()


def test_the_database_has_the_schema_and_no_rows_and_a_write_stays_in_it(db_sessions):
    assert stored_keys(db_sessions) == []

    with db_sessions.begin() as session:
        session.add(Setting(key="written by the first test", value=1))

    assert stored_keys(db_sessions) == ["written by the first test"]


def test_the_next_test_does_not_see_the_rows_of_the_test_before_it(db_sessions):
    assert stored_keys(db_sessions) == []


def test_the_url_names_the_database_of_the_case(db_url, request):
    url = make_url(db_url)
    if request.node.callspec.params["db_url"] == "postgres":
        assert url.get_backend_name() == "postgresql"
        assert url.database.startswith("youtube_automanager_test_")
    else:
        assert url.get_backend_name() == "sqlite"
        assert url.database.endswith("service.sqlite")


@pytest.mark.sqlite_only
def test_a_test_with_the_sqlite_only_marker_runs_on_sqlite_alone(db_url):
    assert make_url(db_url).get_backend_name() == "sqlite"
