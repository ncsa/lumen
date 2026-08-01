"""Tests for the guarded DB session release in the app-context teardown.

Both failure-path tests break the rollback itself rather than patching ``remove()``,
because that is where production fails: ``scoped_session.remove()`` closes the session
before clearing its registry, and ``SessionTransaction.close()`` detaches the
transaction before closing its connections — so a rollback that raises leaves the
connection checked out with the still-registered session pinning it.
"""
import gc
import logging
from http import HTTPStatus

from sqlalchemy import text

from lumen.extensions import db
from lumen.services import pool_tracker


def _broken_rollback(conn):
    raise RuntimeError("rollback exploded")


def test_failed_rollback_is_logged_and_the_session_dropped(app, caplog):
    with app.app_context():
        dialect = db.engine.dialect
    original = dialect.do_rollback
    try:
        with caplog.at_level(logging.ERROR):
            dialect.do_rollback = _broken_rollback
            with app.app_context():
                db.session.execute(text("select 1"))
    finally:
        dialect.do_rollback = original

    assert "db.session.remove() failed during app-context teardown" in caplog.text
    assert "rollback exploded" in caplog.text
    # Left in the registry, the session would pin its connection for the life of
    # the process — and Flask-SQLAlchemy's own teardown would retry the same
    # failing close(), letting the error escape AppContext.pop() as it did before.
    assert not db.session.registry.registry


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


def test_plain_requests_do_not_accumulate_checkouts(app, client):
    """Repeated plain requests must not grow the pool's checked-out count.

    That growth is the production symptom: one connection stranded per failed
    teardown, never returned. The count sits at one rather than zero because the
    test client keeps the most recent app context (and so its session) alive — it
    is the *absence of accumulation* that is under test here.
    """
    with app.app_context():
        pool = db.engine.pool
    # Collect first: a session pinned by an earlier test's captured log record
    # would otherwise still be holding a connection and skew the baseline.
    gc.collect()
    baseline = pool.checkedout()

    for _ in range(5):
        assert client.get("/healthz").status_code == HTTPStatus.OK
        assert pool.checkedout() <= baseline + 1
        assert len(db.session.registry.registry) <= 1

    assert len(pool_tracker.outstanding()) <= 1
