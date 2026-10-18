"""Alembic environment for the monitor database.

The URL is set by `monitor.migrate` as `sqlalchemy.url`; when Alembic is run
directly it falls back to the monitor config (MONITOR_CONFIG or config.yaml).
"""

import os
from logging.config import fileConfig

from alembic import context

from monitor.config import DEFAULT_CONFIG_PATH, load_config
from monitor.db import make_engine
from monitor.models import Base

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or load_config(os.environ.get("MONITOR_CONFIG", DEFAULT_CONFIG_PATH)).database_url


def run_migrations_offline() -> None:
    url = _url()
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, render_as_batch=url.startswith("sqlite"))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(_url())
    try:
        with engine.connect() as connection:
            # Batch mode lets ALTER-style migrations work on SQLite.
            context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=connection.dialect.name == "sqlite")
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
