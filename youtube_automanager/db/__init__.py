"""The service database: the engine, the sessions and the schema migrations."""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, make_url
from sqlalchemy.orm import Session, sessionmaker

LOG = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def make_engine(url: str) -> Engine:
    """Create the engine of the service. SQLite enforces foreign keys only when each connection turns them on."""
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create sessions that keep the loaded values at a commit, so an object stays readable after its session ends."""
    return sessionmaker(engine, expire_on_commit=False)


def alembic_config(url: str) -> Config:
    """An Alembic configuration for `url` that needs no `alembic.ini` and leaves the logging setup as it is."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR).replace("%", "%%"))
    config.attributes["url"] = url
    return config


def upgrade_to_head(url: str) -> None:
    LOG.info("Upgrading the database %s to the newest schema", make_url(url).render_as_string(hide_password=True))
    command.upgrade(alembic_config(url), "head")
