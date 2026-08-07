"""Tests for connection-pool checkout tracking and the pool watchdog."""
import gc
import logging
import weakref

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from lumen.services import pool_tracker


class _FakeRecord:
    """Stand-in for a SQLAlchemy _ConnectionRecord.

    A plain class rather than ``object()`` because the tracker holds a weakref to
    the record, and ``object()`` instances do not support weak references.
    """


def _fake_record():
    return _FakeRecord()


def test_checkout_is_tracked_and_released(app):
    record = _fake_record()
    pool_tracker._on_checkout(None, record, None)
    try:
        entry = pool_tracker._outstanding[id(record)]
        assert entry.thread
        assert "test_checkout_is_tracked_and_released" in entry.stack
    finally:
        pool_tracker._on_checkin(None, record)
    assert id(record) not in pool_tracker._outstanding


def test_checkout_records_request_endpoint(app):
    with app.test_request_context("/chat"):
        record = _fake_record()
        pool_tracker._on_checkout(None, record, None)
        try:
            assert pool_tracker._outstanding[id(record)].endpoint == "chat.chat_page"
        finally:
            pool_tracker._on_checkin(None, record)


def test_stranded_count_only_counts_old_checkouts(app):
    baseline = pool_tracker.stranded_count()
    record = _fake_record()
    pool_tracker._on_checkout(None, record, None)
    try:
        assert pool_tracker.stranded_count() == baseline
        # Age the checkout past the stranded threshold.
        entry = pool_tracker._outstanding[id(record)]
        pool_tracker._outstanding[id(record)] = entry._replace(
            at=entry.at - pool_tracker.STRANDED_AFTER - 1
        )
        assert pool_tracker.stranded_count() == baseline + 1
        assert "held" in pool_tracker.format_outstanding(min_age=pool_tracker.STRANDED_AFTER)
    finally:
        pool_tracker._on_checkin(None, record)


def test_stale_entry_is_flagged_and_not_counted_as_stranded(app):
    """A record collected without a check-in leaves an entry holding no connection.

    This is the ambiguity that made the production /metrics/debug capture
    unreadable: an entry aging past the threshold on an endpoint that cannot hold
    a connection. A dead weakref settles it without needing pg_stat_activity.
    """
    baseline = pool_tracker.stranded_count()
    record = _fake_record()
    key = id(record)
    pool_tracker._on_checkout(None, record, None)
    try:
        entry = pool_tracker._outstanding[key]
        pool_tracker._outstanding[key] = entry._replace(
            at=entry.at - pool_tracker.STRANDED_AFTER - 1
        )
        assert pool_tracker.stranded_count() == baseline + 1

        # Drop the record the way a lost check-in would, leaving the entry behind.
        del record, entry
        gc.collect()

        stale = pool_tracker._outstanding[key]
        assert stale.is_stale()
        assert pool_tracker.stranded_count() == baseline
        listing = pool_tracker.format_outstanding(min_age=pool_tracker.STRANDED_AFTER)
        assert "STALE-TRACKER-ENTRY" in listing
    finally:
        pool_tracker._outstanding.pop(key, None)


def test_live_checkout_is_not_stale(app):
    record = _fake_record()
    pool_tracker._on_checkout(None, record, None)
    try:
        assert not pool_tracker._outstanding[id(record)].is_stale()
        assert "STALE-TRACKER-ENTRY" not in pool_tracker.format_outstanding()
    finally:
        pool_tracker._on_checkin(None, record)


def test_format_outstanding_empty():
    # No checkout is a day old, so the filtered listing is empty.
    assert pool_tracker.format_outstanding(min_age=86400).strip() == "(none)"


def test_format_holders_names_the_session_retaining_a_connection():
    """The chain from the pool record to its retainer, which the checkout stack
    cannot show: in production the acquiring thread was idle while the connection
    stayed out, so where it was acquired was not where it was held."""
    engine = create_engine("sqlite://", poolclass=QueuePool)
    pool_tracker.init_pool_tracking()
    session = Session(engine)
    session.execute(text("select 1"))
    try:
        out = pool_tracker.format_holders(min_age=0)
        assert "fairy: live" in out
        assert "sqlalchemy.engine.base.Connection" in out
        assert "sqlalchemy.orm.session.Session" in out
    finally:
        session.close()
        engine.dispose()


def test_format_holders_reports_a_stale_entry_as_holding_nothing():
    record = _fake_record()
    key = id(record)
    pool_tracker._on_checkout(None, record, None)
    try:
        del record
        gc.collect()
        assert "no connection held" in pool_tracker.format_holders(min_age=0)
    finally:
        pool_tracker._outstanding.pop(key, None)


def test_format_holders_empty_when_nothing_outstanding():
    assert pool_tracker.format_holders(min_age=86400).strip() == "(none)"


def test_retainers_omits_connective_tissue():
    """Functions, modules and cells reference everything and name nothing."""
    assert pool_tracker._is_informative("sqlalchemy.orm.session.Session")
    assert pool_tracker._is_informative("builtins.generator")
    assert not pool_tracker._is_informative("builtins.function")
    assert not pool_tracker._is_informative("builtins.cell")


def test_thread_dump_includes_current_thread():
    dump = pool_tracker.thread_dump()
    assert "--- thread" in dump
    assert "test_thread_dump_includes_current_thread" in dump


def test_watchdog_logs_after_consecutive_near_capacity_scrapes(caplog):
    pool_tracker._pressure_count = 0
    record = _fake_record()
    pool_tracker._on_checkout(None, record, None)
    try:
        with caplog.at_level(logging.ERROR, logger="lumen.services.pool_tracker"):
            for _ in range(pool_tracker._PRESSURE_SCRAPES - 1):
                pool_tracker.watchdog(75, 80)
            assert caplog.records == []

            pool_tracker.watchdog(75, 80)
            assert len(caplog.records) == 1
            assert "DB pool at 75/80" in caplog.records[0].getMessage()

            # Logs once per episode, not on every subsequent scrape.
            pool_tracker.watchdog(75, 80)
            assert len(caplog.records) == 1
    finally:
        pool_tracker._on_checkin(None, record)
        pool_tracker._pressure_count = 0


def test_watchdog_resets_when_pool_recovers(caplog):
    pool_tracker._pressure_count = 0
    try:
        with caplog.at_level(logging.ERROR, logger="lumen.services.pool_tracker"):
            pool_tracker.watchdog(75, 80)
            pool_tracker.watchdog(10, 80)  # recovered — counter resets
            assert pool_tracker._pressure_count == 0
            for _ in range(pool_tracker._PRESSURE_SCRAPES - 1):
                pool_tracker.watchdog(75, 80)
            assert caplog.records == []
    finally:
        pool_tracker._pressure_count = 0


def test_watchdog_noop_without_limit():
    pool_tracker._pressure_count = 0
    pool_tracker.watchdog(75, 0)
    assert pool_tracker._pressure_count == 0


def test_checkout_records_a_live_app_context(app):
    """A stranded checkout whose context is still alive means teardown never ran."""
    with app.app_context():
        record = _fake_record()
        pool_tracker._on_checkout(None, record, None)
        try:
            assert pool_tracker._outstanding[id(record)].app_ctx_state() == "alive"
            assert "app_ctx=alive" in pool_tracker.format_outstanding()
        finally:
            pool_tracker._on_checkin(None, record)


def test_checkout_reports_a_collected_app_context(app):
    """Once the context is popped it is collected, so teardown did run.

    Pushed and popped explicitly rather than with a ``with`` block, which can leave
    the context referenced by the frame and keep the weakref alive.
    """
    ctx = app.app_context()
    ctx.push()
    record = _fake_record()
    pool_tracker._on_checkout(None, record, None)
    ctx.pop()
    del ctx
    gc.collect()
    try:
        assert pool_tracker._outstanding[id(record)].app_ctx_state() == "collected"
    finally:
        pool_tracker._on_checkin(None, record)


def test_checkout_records_the_scope_key(app):
    """The scope key is the id of the current app context — the same key the
    scoped-session registry uses — so a capture can match a leaked checkout to
    its registry entry."""
    with app.app_context():
        from flask.globals import app_ctx
        expected = id(app_ctx._get_current_object())
        record = _fake_record()
        pool_tracker._on_checkout(None, record, None)
        try:
            assert pool_tracker._outstanding[id(record)].scope_key == expected
            assert f"scope=0x{expected:x}" in pool_tracker.format_outstanding()
        finally:
            pool_tracker._on_checkin(None, record)


def test_scope_report_flags_a_checkout_whose_context_never_tore_down(app):
    """The discriminating capture: a checkout made under a context that never
    went through teardown reads NEVER, one whose context tore down without
    releasing it reads ran-with-session-present."""
    with app.app_context():
        record = _fake_record()
        pool_tracker._on_checkout(None, record, None)
        key = pool_tracker._outstanding[id(record)].scope_key
    try:
        # Some teardown activity, none of it for this checkout's scope.
        pool_tracker._teardowns.clear()
        pool_tracker.record_teardown(had_session=True)  # different (current-test) ctx
        report = pool_tracker.format_scope_report([key])
        assert f"key 0x{key:x}" in report
        assert "teardown=NEVER" not in report  # ring starts after the checkout
        # Age the ring window back past the checkout so absence is conclusive.
        k, t, had = pool_tracker._teardowns[0]
        pool_tracker._teardowns[0] = (k, pool_tracker._outstanding[id(record)].at - 1, had)
        report = pool_tracker.format_scope_report([key])
        assert "teardown=NEVER (within ring window)" in report
        assert "registered=yes" in report

        # Now teardown runs for that scope: the verdict flips.
        pool_tracker._teardowns.append((key, pool_tracker._outstanding[id(record)].at + 1, True))
        report = pool_tracker.format_scope_report([])
        assert "teardown=ran" in report
        assert "session present" in report
    finally:
        pool_tracker._on_checkin(None, record)


def test_scope_report_with_no_teardowns_recorded():
    pool_tracker._teardowns.clear()
    report = pool_tracker.format_scope_report([])
    assert "0 session(s) registered" in report


def test_checkout_without_an_app_context_reports_none():
    """Checkouts made outside a request — background workers — record no context.

    Built directly rather than through ``_on_checkout`` because pytest-flask pushes
    a context around every test, so the no-context path is unreachable from here.
    """
    record = _fake_record()
    entry = pool_tracker.Checkout(
        endpoint="no-request-context",
        thread="worker",
        at=0.0,
        stack="",
        record_ref=weakref.ref(record),
    )
    assert entry.app_ctx_state() == "none"
