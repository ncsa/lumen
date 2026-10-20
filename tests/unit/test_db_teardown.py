"""Tests for the guarded DB session release in the app-context teardown.

Both failure-path tests break the rollback itself rather than patching ``remove()``,
because that is where production fails: ``scoped_session.remove()`` closes the session
before clearing its registry, and ``SessionTransaction.close()`` detaches the
transaction before closing its connections — so a rollback that raises leaves the
connection checked out with the still-registered session pinning it.
"""
import contextvars
import gc
import logging
from http import HTTPStatus

from sqlalchemy import text

from lumen.extensions import db
from lumen.services import pool_tracker


def _broken_rollback(conn):
    raise RuntimeError("rollback exploded")


def test_failed_rollback_is_logged_and_the_session_dropped(app, caplog):
    """The session this app context created must be gone from the registry.

    Asserted against *this context's* key rather than "the registry dict is
    empty", which is not the same claim and was flaky. ``db.session.registry``
    is a ``ScopedRegistry`` built with a ``scopefunc``, so its storage is a plain
    process-wide dict (``sqlalchemy/util/_collections.py``), not a
    ``threading.local``. The metrics-snapshot refresher runs in this same
    process, pushes its own app context every pass and registers a session under
    a different key while it does; catching it mid-pass made ``not registry``
    false for a session that has nothing to do with this test.

    Nothing is weakened by narrowing it: if ``release_db_session`` stopped
    clearing the registry after a failed rollback, *this* context's key would
    still be there and this assertion fails every time.
    """
    with app.app_context():
        dialect = db.engine.dialect
    original = dialect.do_rollback
    try:
        with caplog.at_level(logging.ERROR):
            dialect.do_rollback = _broken_rollback
            # ``ctx`` is deliberately still referenced after the block: the
            # registry is keyed by ``id(app_ctx)``, and a collected context
            # frees its address for the next one to reuse.
            with app.app_context() as ctx:
                db.session.execute(text("select 1"))
    finally:
        dialect.do_rollback = original

    assert "db.session.remove() failed during app-context teardown" in caplog.text
    assert "rollback exploded" in caplog.text
    # Left in the registry, the session would pin its connection for the life of
    # the process — and Flask-SQLAlchemy's own teardown would retry the same
    # failing close(), letting the error escape AppContext.pop() as it did before.
    assert id(ctx) not in db.session.registry.registry


def test_failed_rollback_releases_the_pool_slot(app):
    """The pool slot must come back once the registry drops its reference.

    This test silences the app logger rather than capturing it: the log record
    carries the exception traceback, whose frames reference the session, and
    pytest's log capture holds every emitted record for the duration of the test —
    which would pin the very objects whose collection is under test. Nothing
    retains records that way in production; logging formats and discards them.
    """
    with app.app_context():
        pool = db.engine.pool
        dialect = db.engine.dialect
    # Collect first: a session pinned by an earlier test's captured log record
    # would otherwise still be holding a connection and skew the baseline.
    gc.collect()
    baseline = pool.checkedout()
    original = dialect.do_rollback
    app.logger.propagate = False
    quiet = logging.NullHandler()
    app.logger.addHandler(quiet)
    try:
        dialect.do_rollback = _broken_rollback
        with app.app_context():
            db.session.execute(text("select 1"))
            assert pool.checkedout() == baseline + 1
    finally:
        dialect.do_rollback = original
        app.logger.removeHandler(quiet)
        app.logger.propagate = True

    # With the registry entry gone, SQLAlchemy's weakref finalizer resets or
    # invalidates the connection and checks it back in once the session is collected.
    gc.collect()
    assert pool.checkedout() == baseline


def test_plain_requests_do_not_accumulate_checkouts(app, client, stopped_refresher):
    """Repeated plain requests must not grow the pool's checked-out count.

    That growth is the production symptom: one connection stranded per failed
    teardown, never returned. So each request must leave the pool and the
    session registry exactly as it found them.

    Each request runs in a fresh ``contextvars.Context``. pytest-flask pushes a
    request context for every test that uses ``app``, and Flask reuses an
    already-pushed app context for the same app. Called directly,
    ``client.get`` would run every request inside that one long-lived context:
    its session would stay registered until the test ended, and per-request
    teardown, the code this test is about, would never run. An empty context
    has no app context to reuse, so each request pushes and pops its own.

    The refresher is stopped first (``stopped_refresher``) because both counts
    are process-wide: ``pool.checkedout()`` and the session registry, a plain
    dict keyed by ``id(app_ctx)``. A ``metrics-snapshot`` pass holds a
    connection and a registered session while it runs, and a pass that started
    after the baseline read showed up here as a leak (``2 <= 0 + 1`` in CI, with
    several refreshers alive). Stopping waits for an in-flight pass to finish,
    so nothing else can move either count during the loop.
    """
    with app.app_context():
        pool = db.engine.pool
    # Collect first: a session pinned by an earlier test's captured log record
    # would otherwise still be holding a connection and skew the baseline.
    gc.collect()
    baseline = pool.checkedout()
    sessions_baseline = set(db.session.registry.registry)
    outstanding_baseline = len(pool_tracker.outstanding())

    for _ in range(5):
        response = contextvars.Context().run(client.get, "/healthz")
        assert response.status_code == HTTPStatus.OK
        assert pool.checkedout() == baseline
        assert set(db.session.registry.registry) == sessions_baseline

    assert len(pool_tracker.outstanding()) <= outstanding_baseline
