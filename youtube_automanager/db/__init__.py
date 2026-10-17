"""The service database: the engine, the sessions and the schema migrations."""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, make_url
from sqlalchemy.orm import Session, sessionmaker

LOG = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
# A sync child and the web service write to one SQLite file. A write waits this long for the lock of the other one.
SQLITE_BUSY_TIMEOUT_S = 30
POSTGRES_CONNECT_TIMEOUT_S = 10


def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def connect_args(url: str) -> dict[str, int]:
    """The options of a new connection: how long SQLite waits for a lock of another process, and how long PostgreSQL
    waits for its server."""
    backend = make_url(url).get_backend_name()
    if backend == "sqlite":
        return {"timeout": SQLITE_BUSY_TIMEOUT_S}
    if backend == "postgresql":
        return {"connect_timeout": POSTGRES_CONNECT_TIMEOUT_S}
    return {}


def make_engine(url: str) -> Engine:
    """Create the engine of the service. SQLite enforces foreign keys only when each connection turns them on."""
    engine = create_engine(url, connect_args=connect_args(url))
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
