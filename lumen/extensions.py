from authlib.integrations.flask_client import OAuth
from flask import current_app, g, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()
migrate = Migrate()
oauth = OAuth()
limiter = Limiter(key_func=get_remote_address)


def _chat_entity_id():
    # Keyed on the public UUID: rate-limit keys can surface in Redis and logs.
    entity = g.get("entity")
    return str(entity.uuid) if entity is not None else (request.remote_addr or "unknown")


DEFAULT_RATE_LIMIT = "30 per minute"

# Deduplicate rate-limit config warnings: the limit callables run on every request.
_rate_limit_warned: set = set()


def _warn_rate_limit_once(key, msg, *args):
    if key not in _rate_limit_warned:
        _rate_limit_warned.add(key)
        current_app.logger.warning(msg, *args)


def _rate_limiting_cfg() -> dict:
    rl_cfg = current_app.config.get("YAML_DATA", {}).get("rate_limiting") or {}
    return rl_cfg if isinstance(rl_cfg, dict) else {}


def resolve_chat_limit() -> str:
    """The chat rate limit: ``rate_limiting.chat``, else the deprecated ``limit``.

    ``limit`` is the pre-2.5.1 name of ``chat`` and is removed in 2.7.0. Setting
    both is rejected by config validation and at startup, so ``chat`` winning
    here only matters for a config that slipped past both.
    """
    rl_cfg = _rate_limiting_cfg()
    if "chat" in rl_cfg:
        return rl_cfg["chat"]
    if "limit" in rl_cfg:
        _warn_rate_limit_once(
            ("deprecated", "limit"),
            "rate_limiting.limit is deprecated: use rate_limiting.chat; "
            "limit will be removed in 2.7.0.",
        )
        return rl_cfg["limit"]
    return DEFAULT_RATE_LIMIT


def _chat_limit():
    return resolve_chat_limit()

# Root module of the PostgreSQL DBAPI (psycopg 3). Every other DBAPI (sqlite3)
# falls through untouched.
_POSTGRES_DBAPI_MODULE = "psycopg"


@event.listens_for(Engine, "connect")
def _force_utc_session(dbapi_connection, connection_record):
    """Pin every PostgreSQL session to UTC.

    CLAUDE.md's rule is that all times are UTC everywhere in the app and the DB,
    but ``date_trunc``, ``EXTRACT`` and every ``timestamptz`` value psycopg hands
    back are evaluated in the *session's* ``TimeZone``, which Postgres inherits
    from whatever ``initdb`` picked up from the host. Nothing set it, so the
    invariant held only by luck: the ``timescale/timescaledb`` image happens to
    default to UTC. On a server initialised as ``CST6CDT`` the /usage heatmap
    plots every request in the wrong hour *and* the wrong day-of-week, the
    day/week/month buckets land in the wrong period, and ``backfill-aggregate``
    computes the wrong starting month — none of which raises anything.

    Registered on the ``Engine`` *class* rather than on one engine on purpose:
    the guarantee has to hold for every connection in the process, not just the
    Flask-SQLAlchemy pool. That covers the CLI's AUTOCOMMIT connections in
    ``lumen/commands.py``, the throwaway ``NullPool`` engine in
    ``lumen/services/db_pool.py``, Alembic, and any engine a test or script
    creates — none of which would pick up an engine-local ``connect_args``. It
    fires per *DBAPI* connection, so pool overflow, ``pool_recycle`` and a
    pre-ping replacement each get their own ``SET``.

    The ``COMMIT`` is required, not tidiness: ``SET`` is transactional, so the
    pool's rollback-on-return would revert it, and leaving the transaction open
    would both hold the connection idle-in-transaction and make psycopg refuse
    the dialect's own autocommit / isolation-level changes.
    """
    if type(dbapi_connection).__module__.split(".")[0] != _POSTGRES_DBAPI_MODULE:
        return
    with dbapi_connection.cursor() as cursor:
        cursor.execute("SET TIME ZONE 'UTC'")
    dbapi_connection.commit()
