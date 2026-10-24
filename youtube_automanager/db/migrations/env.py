"""The Alembic environment of the service database.

The database URL comes from `upgrade_to_head`, from `alembic -x url=...`, or from the settings of the service.
"""

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import create_engine, pool

from youtube_automanager.db.models import Base
from youtube_automanager.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# The options go to `context.configure` as keyword arguments, and each has its own type there.
CONFIGURE_OPTIONS: dict[str, Any] = {
    "target_metadata": target_metadata,
    # SQLite cannot alter most parts of a table, so each change recreates the table.
    "render_as_batch": True,
    "compare_type": True,
    "compare_server_default": True,
}


def database_url() -> str:
    url = config.attributes.get("url") or context.get_x_argument(as_dictionary=True).get("url")
    if url:
        return url
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **CONFIGURE_OPTIONS,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # This engine leaves SQLite foreign keys off. A batch migration drops the old copy of a table, and with
    # foreign keys on, that drop would delete or refuse the rows that refer to it.
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, **CONFIGURE_OPTIONS)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
