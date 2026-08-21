import concurrent.futures
import contextlib
import logging
import os
import threading
import time

import openai
from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm.exc import StaleDataError

from lumen.extensions import db
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.timeutils import utcnow

try:
    import fcntl
except ImportError:  # non-POSIX platform: no flock, so no election (see _elected_to_probe)
    fcntl = None

logger = logging.getLogger(__name__)

# The openai/httpx client's own timeout=5.0 bounds well-behaved failures (refused,
# reset), but a network path that silently drops packets (no RST) can leave the
# raw socket.connect() blocked far longer than that. Running each probe through
# this executor with a hard result() timeout guarantees one blackholed endpoint
# can't stall every later endpoint in the same pass. A stuck probe's thread is
# abandoned (not killed — Python can't force-abort a blocked native call) and
# occupies a worker slot until the OS eventually gives up; the pool is sized
# with headroom so that's tolerable unless many endpoints are blackholed at once.
_PROBE_TIMEOUT = 10
_probe_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="health-probe")

#: Seconds between passes.
_CHECK_INTERVAL = 60

#: Environment variable overriding :data:`DEFAULT_LOCK_PATH`. Follows the
#: ``LUMEN_WSGI_SEND_TIMEOUT`` precedent in ``wsgi_disconnect.py``: server
#: plumbing belongs in the environment, not in ``config.yaml``.
LOCK_PATH_ENV = "LUMEN_HEALTH_LOCK"

#: File whose ``flock`` elects the one process that probes. A plain container
#: path is already pod-scoped — every uvicorn worker of a pod shares one
#: container — so this deliberately needs no volume and no shared filesystem.
DEFAULT_LOCK_PATH = "/tmp/lumen-health.lock"

#: A holder that has not refreshed its heartbeat (the lock file's mtime) for
#: this long is presumed wedged rather than working, and is reported loudly —
#: but not displaced; see :func:`_elected_to_probe`. Nothing in a pass is
#: preemptible — probes are serial and legitimately run _PROBE_TIMEOUT × N,
#: abandoned probe threads cannot be killed, and a blocked commit() cannot be
#: interrupted — so there is deliberately no watchdog that releases the flock
#: out from under a live holder; that would admit a second concurrent pass
#: against the same backends.
_HEARTBEAT_STALE_AFTER = 3 * _CHECK_INTERVAL


def _no_heartbeat() -> None:
    """Heartbeat for a pass that holds no lock to refresh."""


def _acquire(path: str):
    """Open *path* and try to take a non-blocking exclusive flock.

    Returns ``(fd, acquired)``. ``fd`` is None when the file or the lock
    primitive is unusable (platform, filesystem), and the caller then runs the
    pass unelected — the behaviour that predates the election. Never raises:
    electing a runner must not turn a health check into an exception.
    """
    try:
        # O_CREAT|O_RDWR, never "w": "w" truncates *before* the flock attempt,
        # so every non-holder's failed attempt would wipe the holder's file.
        #
        # O_NOFOLLOW because the default path is under /tmp. Inside a container
        # that is private and this is theatre, but Lumen also runs outside one,
        # and there a symlink planted at the path would point this open() —
        # and the utime() heartbeat — at a file of someone else's choosing.
        # Refusing to follow it costs nothing and removes the question.
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o644)
    except OSError as e:
        logger.warning("health check: cannot open lock file %s (%r); running unelected", path, e)
        return None, False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd, True
    except BlockingIOError:
        return fd, False  # held by another process
    except Exception as e:
        os.close(fd)
        logger.warning("health check: cannot flock %s (%r); running unelected", path, e)
        return None, False


def _heartbeat(fd: int) -> None:
    """Mark the lock file as still being worked on. Best effort."""
    try:
        os.utime(fd)
    except (OSError, NotImplementedError):  # a platform without futimens; staleness just degrades
        pass


def _heartbeat_is_stale(fd: int) -> bool:
    try:
        return (time.time() - os.fstat(fd).st_mtime) > _HEARTBEAT_STALE_AFTER
    except OSError:
        return False


@contextlib.contextmanager
def _elected_to_probe():
    """Yield a heartbeat callable if this process should run the pass, else None.

    The lock is non-blocking on purpose. A blocking flock would be strictly
    worse than the duplicated probing it fixes: ``check_all_endpoints`` commits
    inside the pass and that commit can block up to ``pool_timeout`` on an
    exhausted pool, so every other process would queue behind a hung holder —
    stalling all health probing during exactly the burst when health data
    matters. Non-holders skip and read the DB result the holder wrote.

    There is deliberately no takeover path. An earlier version let a process
    whose heartbeat looked stale seize the pass, and that is unsound in both
    directions:

    * When the holder **dies**, the kernel releases its flock for free, so the
      next ``_acquire`` simply succeeds and no takeover is needed.
    * When the holder is **alive but wedged**, it still owns the flock, so a
      "takeover" is a second pass running concurrently with it — the exact
      outcome the non-blocking design exists to prevent, aimed at the same
      backends, and raising the odds of the ``StaleDataError`` handled below.

    That leaves staleness reachable only for a live, wedged holder — and the
    only step that does not heartbeat is the commit, so "stale" means "the pool
    is exhausted". Racing another pass into an exhausted pool makes it worse.
    A wedged holder is therefore logged loudly and left alone.
    """
    with contextlib.ExitStack() as stack:
        if fcntl is None:
            yield _no_heartbeat
            return
        path = os.environ.get(LOCK_PATH_ENV) or DEFAULT_LOCK_PATH
        fd, acquired = _acquire(path)
        if fd is None:
            # Cannot open the lock file at all (read-only fs, permissions).
            # Fall back to today's behaviour and probe: a housekeeping failure
            # must never turn into no health checks.
            yield _no_heartbeat
            return
        stack.callback(os.close, fd)  # closing the fd releases the flock
        if acquired:
            # Stamp immediately: an old mtime inherited from a previous pass
            # would read as stale to everyone else for this whole pass.
            _heartbeat(fd)
            yield lambda: _heartbeat(fd)
            return
        if _heartbeat_is_stale(fd):
            logger.warning(
                "health check: %s has not been refreshed for over %ss — the holding "
                "process is wedged (most likely blocked committing against an "
                "exhausted connection pool). Skipping this pass rather than running "
                "a second one alongside it; health data is going stale.",
                path, _HEARTBEAT_STALE_AFTER,
            )
        else:
            logger.info("health check: another process holds %s; skipping this pass", path)
        yield None


def _probe_endpoint(url: str, api_key: str, expected: str) -> bool:
    with openai.OpenAI(api_key=api_key, base_url=url, timeout=5.0) as client:
        models = client.models.list()
    return expected in {m.id for m in models.data}


def check_all_endpoints(heartbeat=_no_heartbeat) -> int:
    """Run one health-check pass for all endpoints. Caller owns the app context.
    Returns the number of endpoints checked.

    *heartbeat* is called after each endpoint so a long but healthy pass is not
    mistaken for a wedged one; see :func:`_elected_to_probe`."""
    log_enabled = current_app.config.get("LOG_MODEL_HEALTH", False)
    # Join model_name upfront to avoid lazy-loading ep.model_config inside the loop.
    # Accessing the backref on a lazy="dynamic" + delete-orphan relationship can cause
    # SQLAlchemy to schedule the endpoint for deletion, producing StaleDataError on commit.
    rows = db.session.execute(
        select(ModelEndpoint, ModelConfig.model_name.label("config_model_name"))
        .join(ModelConfig, ModelEndpoint.model_config_id == ModelConfig.id)
    ).all()
    # Capture every scalar needed for probing/logging before releasing the read
    # transaction. Each probe is a network call (up to 5s + retries per endpoint),
    # so holding the transaction open across the loop would leave the connection
    # idle-in-transaction and trip Postgres's idle_in_transaction_session_timeout.
    probes = [
        (ep, ep.url, ep.api_key, ep.model_name or config_model_name)
        for ep, config_model_name in rows
    ]
    db.session.rollback()  # end the read transaction before the slow network probes

    now = utcnow()
    for ep, url, api_key, expected in probes:
        try:
            ep.healthy = _probe_executor.submit(_probe_endpoint, url, api_key, expected).result(timeout=_PROBE_TIMEOUT)
            if log_enabled:
                found = "found" if ep.healthy else "NOT FOUND"
                current_app.logger.info(
                    "health check %s → endpoint UP, model '%s' %s", url, expected, found
                )
        except concurrent.futures.TimeoutError:
            ep.healthy = False
            if log_enabled:
                current_app.logger.info(
                    "health check %s → endpoint DOWN (probe exceeded %ss, abandoned)", url, _PROBE_TIMEOUT
                )
        except Exception as e:
            ep.healthy = False
            if log_enabled:
                cause = e.__cause__ or e
                current_app.logger.info(
                    "health check %s → endpoint DOWN (%r)", url, cause
                )
        ep.last_checked_at = now
        heartbeat()
    try:
        db.session.commit()
    except StaleDataError:
        # An endpoint was deleted between the SELECT and the commit; discard stale updates.
        db.session.rollback()
        logger.warning("health check: endpoint deleted mid-pass, changes discarded")
    return len(probes)


def run_health_pass() -> int:
    """Run one pass if this process wins the election, else skip it.

    Returns the number of endpoints checked, or 0 when another process is
    already running the pass. Caller owns the app context.
    """
    with _elected_to_probe() as heartbeat:
        if heartbeat is None:
            return 0
        return check_all_endpoints(heartbeat)


def start_health_checker(app):
    """Start a background daemon thread that checks all endpoints every 60s."""

    def run():
        while True:
            try:
                with app.app_context():
                    run_health_pass()
            except Exception:
                logger.exception("health checker error")
            time.sleep(_CHECK_INTERVAL)

    t = threading.Thread(target=run, daemon=True)
    t.start()
