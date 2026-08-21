"""Tests for connection-pool checkout tracking and the pool watchdog."""
import gc
import inspect
import logging
import sys
import threading
import time
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


def test_scope_report_gives_teardown_status_for_connectionless_sessions():
    """A registered session holding no connection still gets a teardown verdict:
    NEVER is the same never-torn-down disease without the stranded connection."""
    pool_tracker._teardowns.clear()
    orphan, torn = 0xabc1, 0xabc2
    pool_tracker._teardowns.append((torn, 1.0, True))
    report = pool_tracker.format_scope_report([orphan, torn])
    assert f"key 0x{orphan:x}  checkouts=0  teardown=NEVER" in report
    assert f"key 0x{torn:x}  checkouts=0  teardown=last ran" in report


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


# --- checkout wait timing (TimingQueuePool) ---------------------------------


def _timing_engine(**kwargs):
    """An engine whose pool is the instrumented one.

    In-memory SQLite with ``check_same_thread`` off, so a connection opened by
    one thread can be checked back in by another: the contention test needs two
    threads and it is the pool, not the database, that is under test.
    """
    return create_engine(
        "sqlite://",
        poolclass=pool_tracker.TimingQueuePool,
        connect_args={"check_same_thread": False},
        **kwargs,
    )


def _wait_histogram():
    """(count, sum) that ``lumen_db_pool_wait_seconds`` holds in this process."""
    from lumen.blueprints.metrics import middleware

    count = total = 0.0
    for metric in middleware._db_pool_wait.collect():
        for sample in metric.samples:
            if sample.name.endswith("_count"):
                count = sample.value
            elif sample.name.endswith("_sum"):
                total = sample.value
    return count, total


def test_uncontended_checkout_reaches_the_real_histogram():
    """An immediate hit on the pool is a real observation, not a skipped one.

    Nothing is patched here: the value goes through ``observe_pool_wait`` into
    the Prometheus histogram, which is what proves the call site is wired. The
    left edge of the distribution is meaningful — it is what separates a pool
    that is merely full from one requests are queued behind.
    """
    before_count, before_sum = _wait_histogram()
    engine = _timing_engine()
    try:
        with engine.connect() as conn:
            assert conn.execute(text("select 1")).scalar() == 1
    finally:
        engine.dispose()
    after_count, after_sum = _wait_histogram()
    assert after_count == before_count + 1
    assert after_sum - before_sum < 0.05


def test_exhausted_pool_records_the_time_the_second_consumer_blocked(monkeypatch):
    """The measurement itself is real: a real pool, really exhausted.

    ``pool_size=1`` with no overflow, one connection held, and a second thread
    that cannot proceed until the first is checked back in — asserted by the
    thread still being alive at the moment of release. Only the *recording* is
    intercepted, and it still calls through to the real histogram.
    """
    from lumen.blueprints.metrics import middleware

    waits = []
    real_observe = middleware.observe_pool_wait

    def record(seconds):
        waits.append(seconds)
        real_observe(seconds)

    monkeypatch.setattr(middleware, "observe_pool_wait", record)

    blocked = 0.2
    engine = _timing_engine(pool_size=1, max_overflow=0)
    held = engine.connect()
    reached_connect = threading.Event()

    def waiter():
        reached_connect.set()
        engine.connect().close()

    thread = threading.Thread(target=waiter, name="pool-waiter")
    try:
        thread.start()
        assert reached_connect.wait(timeout=5)
        time.sleep(blocked)
        # Nothing in waiter() blocks except the checkout, so a thread still alive
        # here is a thread queued on the pool.
        assert thread.is_alive()
        held.close()
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        held.close()
        engine.dispose()

    assert len(waits) == 2
    assert min(waits) < 0.05  # the first checkout, which never queued
    # 10% of slack for the scheduling gap between the event being set and the
    # waiting thread actually entering the checkout.
    assert max(waits) >= blocked * 0.9


def test_do_get_is_still_the_method_the_timing_pool_wraps():
    """``_do_get`` is semi-private, and a rename would be silent.

    SQLAlchemy has no pre-checkout event, so the wait can only be measured by
    wrapping the acquisition. If a future version renames ``_do_get`` or changes
    its signature, ``TimingQueuePool._do_get`` overrides nothing, every checkout
    stops being timed, and ``lumen_db_pool_wait_seconds`` goes quietly empty.
    Fail here, loudly, instead.
    """
    assert "_do_get" in vars(QueuePool), (
        "sqlalchemy.pool.QueuePool no longer defines _do_get. "
        "pool_tracker.TimingQueuePool._do_get now overrides nothing and "
        "lumen_db_pool_wait_seconds is silently empty — re-point the timing at "
        "whatever replaced it."
    )
    assert list(inspect.signature(QueuePool._do_get).parameters) == ["self"], (
        "sqlalchemy.pool.QueuePool._do_get changed signature; "
        "pool_tracker.TimingQueuePool._do_get must match it or checkouts break."
    )
    assert pool_tracker.TimingQueuePool._do_get is not QueuePool._do_get


def test_a_broken_metric_does_not_break_the_checkout(monkeypatch):
    """Instrumentation must never take down a request."""
    from lumen.blueprints.metrics import middleware

    def boom(seconds):
        raise RuntimeError("prometheus exploded")

    monkeypatch.setattr(middleware, "observe_pool_wait", boom)
    engine = _timing_engine()
    try:
        with engine.connect() as conn:
            assert conn.execute(text("select 1")).scalar() == 1
    finally:
        engine.dispose()


def test_timing_never_imports_the_metrics_middleware_itself(monkeypatch):
    """With the middleware unimported the timing is a no-op — and stays one.

    Importing that module constructs the Prometheus metric objects, which
    prometheus_client binds to their mmap files immediately; doing it before
    PROMETHEUS_MULTIPROC_DIR is set is the hazard documented in
    lumen/__init__.py. So the checkout path may use the module but must never be
    what pulls it in.
    """
    monkeypatch.delitem(sys.modules, "lumen.blueprints.metrics.middleware", raising=False)
    engine = _timing_engine()
    try:
        with engine.connect() as conn:
            assert conn.execute(text("select 1")).scalar() == 1
    finally:
        engine.dispose()
    assert "lumen.blueprints.metrics.middleware" not in sys.modules


def test_timed_checkouts_are_still_tracked_for_leaks():
    """Timing must not disturb the leak attribution this module exists for."""
    pool_tracker.init_pool_tracking()
    engine = _timing_engine()
    before = len(pool_tracker.outstanding())
    conn = engine.connect()
    try:
        assert len(pool_tracker.outstanding()) == before + 1
    finally:
        conn.close()
        engine.dispose()
    assert len(pool_tracker.outstanding()) == before


def test_engine_options_wire_the_timing_pool():
    """A subclass nothing builds an engine with measures nothing.

    Guarding the wiring here rather than in test_db_pool.py because it is this
    class that is silently disabled if the option is dropped.
    """
    from lumen.services import db_pool

    opts = db_pool.build_engine_options(
        "postgresql://u:p@localhost/db", {"max_connections": 100}, workers=1, replicas=1
    )
    assert opts["poolclass"] is pool_tracker.TimingQueuePool
    # SQLite keeps SQLAlchemy's own pool choice — an in-memory engine must not be
    # handed a QueuePool — so it is deliberately not instrumented.
    assert db_pool.build_engine_options("sqlite:///x.db", {}, workers=1, replicas=1) == {}
