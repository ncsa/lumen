import logging
import sys
from logging.config import fileConfig

from alembic import context
from flask import current_app

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
fileConfig(config.config_file_name)
logger = logging.getLogger('alembic.env')

SQLITE_MIGRATION_ERROR = """\
ERROR: Database migrations cannot be run against SQLite.

Migrations are PostgreSQL-only. The following migrations use PostgreSQL-specific
DDL that SQLite does not support (ALTER TABLE ... ADD/DROP CONSTRAINT):

  - y9z0a1b2c3d4  request_logs surrogate PK (DROP CONSTRAINT without batch mode)
  - b2c3d4e5f6g7  entity_type CHECK constraint (ADD CONSTRAINT)
  - c4d5e6f7a8b9  rename client to project (ADD/DROP CONSTRAINT)

For local SQLite development, create the schema from the ORM models and stamp
the migration head instead:

    BACKGROUND_WORKER=false uv run python -c \\
      "from lumen import create_app; from lumen.extensions import db; \\
       app=create_app(); app.app_context().push(); db.create_all()"
    uv run flask --app 'lumen:create_app' db stamp head
"""


def get_engine():
    try:
        # this works with Flask-SQLAlchemy<3 and Alchemical
        return current_app.extensions['migrate'].db.get_engine()
    except (TypeError, AttributeError):
        # this works with Flask-SQLAlchemy>=3
        return current_app.extensions['migrate'].db.engine


def get_engine_url():
    try:
        return get_engine().url.render_as_string(hide_password=False).replace(
            '%', '%%')
    except AttributeError:
        return str(get_engine().url).replace('%', '%%')


# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
config.set_main_option('sqlalchemy.url', get_engine_url())
target_db = current_app.extensions['migrate'].db

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def get_metadata():
    if hasattr(target_db, 'metadatas'):
        return target_db.metadatas[None]
    return target_db.metadata


def run_migrations_offline():
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url, target_metadata=get_metadata(), literal_binds=True
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    # this callback is used to prevent an auto-migration from being generated
    # when there are no changes to the schema
    # reference: http://alembic.zzzcomputing.com/en/latest/cookbook.html
    def process_revision_directives(context, revision, directives):
        if getattr(config.cmd_opts, 'autogenerate', False):
            script = directives[0]
            if script.upgrade_ops.is_empty():
                directives[:] = []
                logger.info('No changes in schema detected.')

    conf_args = current_app.extensions['migrate'].configure_args
    if conf_args.get("process_revision_directives") is None:
        conf_args["process_revision_directives"] = process_revision_directives

    connectable = get_engine()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=get_metadata(),
            **conf_args
        )

        # SQLite guard: migrations are PostgreSQL-only. Block upgrade/downgrade
        # (which execute migration scripts) while allowing stamp (which doesn't).
        if connectable.dialect.name == "sqlite":
            fn = getattr(context.get_context(), "_migrations_fn", None)
            if fn and getattr(fn, "__name__", "") in ("upgrade", "downgrade"):
                print(SQLITE_MIGRATION_ERROR, file=sys.stderr)
                sys.exit(1)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
