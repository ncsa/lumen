"""Automatic SQLAlchemy connection-pool sizing.

Each worker process and each Kubernetes replica opens its own pool, so the global
Postgres ``max_connections`` budget must be split across ``workers x replicas`` to
avoid exhausting the server. This module computes per-process pool settings:

- ``pool_size``    = 60% of max_connections / (workers x replicas)
- ``max_overflow`` = 20% of max_connections / (workers x replicas)
- the remaining 20% is reserved headroom for psql, migrations, monitoring, etc.

SQLite has no connection limit, so pool sizing is skipped entirely for it.
"""

import logging
import os
import re

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

# Imported for its side effect: lumen.extensions registers the process-wide
# "every PostgreSQL session is UTC" connect listener at import time, and this
# module's throwaway engine (query_max_connections) connects during create_app
# *before* anything else has imported lumen.extensions. Without this import it
# would be the one connection in the process outside the guarantee.
import lumen.extensions  # noqa: F401
from lumen.services.pool_tracker import TimingQueuePool

logger = logging.getLogger(__name__)

# Fraction of the server's max_connections handed to persistent pool connections,
# to burst overflow connections, and reserved for everything else (psql, migrations).
POOL_FRACTION = 0.60
OVERFLOW_FRACTION = 0.20
# Combined ceiling an explicit override may not exceed across all workers/replicas.
MAX_TOTAL_FRACTION = POOL_FRACTION + OVERFLOW_FRACTION

# Pass-through engine options that remain admin-configurable.
_PASSTHROUGH_KEYS = ("pool_timeout", "pool_recycle")

# a2wsgi's own default thread pool size; also the floor when deriving from the pool,
# and the count used when the engine options carry no pool_size to derive from.
DEFAULT_WSGI_WORKERS = 10
# Threads per pooled connection for the derived count. Most threads serving a
# streamed completion sit idle on the upstream response without holding a DB
# connection, so busy threads routinely outnumber checked-out connections; sizing
# at the pool sum queues requests while the pool is mostly idle.
AUTO_WSGI_WORKERS_PER_CONNECTION = 2
# Ceiling for the derived count. A large pool_size is cheap (connections open on
# demand), but the same number of OS threads is not, so a derived value stops here
# and anything higher has to be asked for explicitly.
MAX_AUTO_WSGI_WORKERS = 256

# Set once the invalid-LUMEN_WSGI_WORKERS warning has been logged, so the
# per-request /metrics/debug call does not repeat it.
_warned_invalid_wsgi_workers = False


def _is_sqlite(uri: str) -> bool:
    return uri.startswith("sqlite")


def detect_workers() -> int:
    """Best-effort count of WSGI worker processes in this deployment.

    Order of precedence:
    1. ``WEB_CONCURRENCY`` env var (honoured natively by uvicorn/gunicorn).
    2. ``--workers N`` / ``--workers=N`` / ``-w N`` on the parent (server master)
       process command line. Parsing the master's cmdline avoids the startup race
       that counting sibling processes has — workers import the app before all
       siblings have spawned.
    3. Fall back to 1.
    """
    env = os.environ.get("WEB_CONCURRENCY", "")
    if env.isdigit() and int(env) > 0:
        return int(env)

    try:
        import psutil

        parent = psutil.Process().parent()
        if parent is not None:
            cmd = parent.cmdline()
            for i, arg in enumerate(cmd):
                if arg in ("--workers", "-w") and i + 1 < len(cmd) and cmd[i + 1].isdigit():
                    return max(1, int(cmd[i + 1]))
                m = re.fullmatch(r"--workers=(\d+)", arg)
                if m:
                    return max(1, int(m.group(1)))
    except Exception:  # psutil missing, no permission, or platform quirk
        logger.debug("worker detection via psutil failed; assuming 1 worker", exc_info=True)

    return 1


def detect_replicas() -> int:
    """Replica count from the ``LUMEN_REPLICAS`` env var (wired from the chart's
    replicaCount). Defaults to 1. Static — does not track HPA scaling."""
    env = os.environ.get("LUMEN_REPLICAS", "")
    if env.isdigit() and int(env) > 0:
        return int(env)
    return 1


def resolve_wsgi_workers(engine_options: dict) -> int:
    """Concurrent requests one process serves, i.e. a2wsgi's thread pool size.

    ``LUMEN_WSGI_WORKERS`` is a positive integer, or ``auto`` (the default when
    unset or empty) to derive the count from this process's own pool:
    ``AUTO_WSGI_WORKERS_PER_CONNECTION x (pool_size + max_overflow)``, clamped to
    ``DEFAULT_WSGI_WORKERS..MAX_AUTO_WSGI_WORKERS``. Any other value logs a warning
    once per process and is treated as ``auto``.

    ``auto`` gives ``DEFAULT_WSGI_WORKERS`` only when the engine options carry no
    ``pool_size``: SQLite, or a Postgres boot where ``max_connections`` could not be
    queried and no ``pool_size`` was configured. With a configured ``pool_size``,
    ``_passthrough_options`` keeps it and ``auto`` sizes from it.
    """
    global _warned_invalid_wsgi_workers

    raw = os.environ.get("LUMEN_WSGI_WORKERS", "")
    setting = raw.strip().lower()
    if setting.isdigit() and int(setting) > 0:
        return int(setting)
    if setting not in ("", "auto") and not _warned_invalid_wsgi_workers:
        _warned_invalid_wsgi_workers = True
        logger.warning(
            "LUMEN_WSGI_WORKERS=%r is not a positive integer or 'auto'; using auto", raw,
        )
    if "pool_size" not in engine_options:
        return DEFAULT_WSGI_WORKERS

    capacity = int(engine_options["pool_size"]) + int(engine_options.get("max_overflow", 0))
    return min(
        MAX_AUTO_WSGI_WORKERS,
        max(DEFAULT_WSGI_WORKERS, AUTO_WSGI_WORKERS_PER_CONNECTION * capacity),
    )


def query_max_connections(uri: str) -> int:
    """Return Postgres ``max_connections`` using a throwaway, unpooled connection."""
    engine = create_engine(uri, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return int(conn.execute(text("SHOW max_connections")).scalar())
    finally:
        engine.dispose()


def _passthrough_options(db_cfg: dict) -> dict:
    """Engine options without auto-sizing: any explicit pool settings the admin
    configured plus the always-on pre-ping.

    Reached only on the Postgres fallback where ``max_connections`` could not be
    queried — SQLite returns ``{}`` from ``build_engine_options`` before getting
    here — so the timing pool class belongs on this path too. That fallback
    means the database was unreachable at startup, which is exactly when
    checkout waits are worth seeing; omitting the instrumentation here would
    blind the metric to the degraded case it exists to describe.
    """
    opts = {"pool_pre_ping": True, "poolclass": TimingQueuePool}
    for key in ("pool_size", "max_overflow", *_PASSTHROUGH_KEYS):
        if key in db_cfg:
            opts[key] = db_cfg[key]
    return opts


def build_engine_options(uri: str, db_cfg: dict, *, workers: int, replicas: int) -> dict:
    """Build SQLALCHEMY_ENGINE_OPTIONS with auto-sized pool settings.

    Returns ``{}`` for SQLite. For Postgres, returns pool_size/max_overflow split
    across ``workers x replicas`` (with ``pool_pre_ping`` always on), honouring an
    explicit pool_size/max_overflow override only when it fits the 80% budget. If
    ``max_connections`` cannot be queried (e.g. the DB is briefly unreachable at
    startup), falls back to the configured pool settings rather than failing boot.
    """
    if _is_sqlite(uri):
        return {}

    divisor = max(1, workers * replicas)

    max_conn = db_cfg.get("max_connections")
    if max_conn is None:
        try:
            max_conn = query_max_connections(uri)
        except Exception:
            logger.warning(
                "Could not query Postgres max_connections; using configured pool "
                "settings without auto-sizing",
                exc_info=True,
            )
            return _passthrough_options(db_cfg)
    max_conn = int(max_conn)

    auto_pool = max(1, int(max_conn * POOL_FRACTION) // divisor)
    auto_overflow = max(1, int(max_conn * OVERFLOW_FRACTION) // divisor)

    explicit_pool = db_cfg.get("pool_size")
    explicit_overflow = db_cfg.get("max_overflow")
    if explicit_pool is not None or explicit_overflow is not None:
        pool_size = int(explicit_pool) if explicit_pool is not None else auto_pool
        max_overflow = int(explicit_overflow) if explicit_overflow is not None else auto_overflow
        combined = (pool_size + max_overflow) * divisor
        if combined <= max_conn * MAX_TOTAL_FRACTION:
            logger.info(
                "Using explicit DB pool: pool_size=%d max_overflow=%d "
                "(combined %d across %d workers x replicas, max_connections=%d)",
                pool_size, max_overflow, combined, divisor, max_conn,
            )
        else:
            logger.warning(
                "Explicit DB pool (pool_size=%d, max_overflow=%d) uses %d connections "
                "across %d workers x replicas, exceeding %.0f%% of max_connections=%d; "
                "falling back to auto-sized pool_size=%d max_overflow=%d",
                pool_size, max_overflow, combined, divisor,
                MAX_TOTAL_FRACTION * 100, max_conn, auto_pool, auto_overflow,
            )
            pool_size, max_overflow = auto_pool, auto_overflow
    else:
        pool_size, max_overflow = auto_pool, auto_overflow
        logger.info(
            "Auto-sized DB pool: pool_size=%d max_overflow=%d "
            "(%d workers x replicas, max_connections=%d)",
            pool_size, max_overflow, divisor, max_conn,
        )

    if (pool_size + max_overflow) * divisor > max_conn:
        logger.warning(
            "DB pool (pool_size=%d + max_overflow=%d) x %d workers/replicas exceeds "
            "max_connections=%d; reduce workers/replicas or raise max_connections",
            pool_size, max_overflow, divisor, max_conn,
        )

    opts = {
        "pool_size": pool_size,
        "max_overflow": max_overflow,
        "pool_pre_ping": True,
        # QueuePool is what SQLAlchemy would pick for Postgres anyway; the
        # subclass only times how long each checkout blocks (lumen_db_pool_wait_
        # seconds), which no pool event can report. Also set on the
        # max_connections-unavailable fallback (_passthrough_options): a DB that
        # was unreachable at startup is exactly when checkout waits are worth
        # seeing, so the instrumentation must not be missing where it matters
        # most.
        "poolclass": TimingQueuePool,
    }
    for key in _PASSTHROUGH_KEYS:
        if key in db_cfg:
            opts[key] = db_cfg[key]
    return opts
