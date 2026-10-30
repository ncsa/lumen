"""Tests for the background metrics-snapshot refresher.

The refresher exists so ``/metrics`` never queries the database. The failure it
must not reintroduce is the 1.22.0 idle-in-transaction leak: a session (and the
Flask app context that owns it) held across the sleep between passes. That is
why the pool assertion below happens *inside* a patched ``_sleep``, on the
refresher thread while it is parked — asserting after a pass completes passes
under the buggy hoisted-context shape too.
"""
import ast
import dataclasses
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from flask import has_app_context
from sqlalchemy import text

from lumen.extensions import db
from lumen.services import metrics_snapshot, timescale_health
from lumen.services.timescale_health import Aggregate, Job, TimescaleHealth

MODULE_PATH = Path(metrics_snapshot.__file__)


@pytest.fixture
def restore_snapshot():
    """Put the module-level snapshot back, so one test cannot leak into another."""
    original = metrics_snapshot._snapshot
    yield
    metrics_snapshot._snapshot = original


def test_refresh_populates_every_snapshot_field(app, restore_snapshot):
    from lumen.models.entity import Entity
    from lumen.models.model_config import ModelConfig
    from lumen.models.model_endpoint import ModelEndpoint
    from lumen.models.model_stat import ModelStat

    with app.app_context():
        user = Entity(entity_type="user", email="snap@example.com", name="Snap", active=True)
        disabled = Entity(entity_type="user", email="off@example.com", name="Off", active=False)
        mc = ModelConfig(
            model_name="snap-model", input_cost_per_million=1.0, output_cost_per_million=1.0,
        )
        db.session.add_all([user, disabled, mc])
        db.session.commit()
        db.session.add_all([
            ModelEndpoint(
                model_config_id=mc.id, url="http://snap.invalid/v1", api_key="k", healthy=True,
            ),
            ModelStat(
                entity_id=user.id, model_config_id=mc.id, source="chat",
                requests=3, input_tokens=11, output_tokens=7, cost=2,
            ),
        ])
        db.session.commit()

        snapshot = metrics_snapshot.refresh_snapshot()

    assert snapshot.primed
    usage = snapshot.model_usage[("snap-model", "chat")]
    assert (usage.requests, usage.input_tokens, usage.output_tokens) == (3, 11, 7)
    assert usage.cost == pytest.approx(2.0)
    assert snapshot.endpoint_health == (("snap-model", "http://snap.invalid/v1", True),)
    assert (snapshot.users_active, snapshot.users_total) == (1, 2)
    # get_snapshot() serves what the pass published, without touching the DB.
    assert metrics_snapshot.get_snapshot() is snapshot


def test_refresher_holds_no_connection_and_no_context_while_sleeping(
    app, restore_snapshot, stopped_refresher, monkeypatch,
):
    """The whole point: while parked between passes the refresher owns nothing.

    Copying ``health.py``'s loop shape but hoisting ``app.app_context()`` out of
    the loop leaks one connection per process, held idle-in-transaction across
    every sleep. Both halves are asserted: no pooled checkout, and no app
    context bound on the refresher's own thread.
    """
    with app.app_context():
        pool = db.engine.pool  # captured here so the assertion needs no context

    observed = {}
    parked = threading.Event()

    def fake_sleep(seconds, stop):
        observed["seconds"] = seconds
        observed["checked_out"] = pool.checkedout()
        observed["has_app_context"] = has_app_context()
        parked.set()
        stop.set()  # one pass is all the test needs; the loop exits on the next check

    monkeypatch.setattr(metrics_snapshot, "_sleep", fake_sleep)
    thread = metrics_snapshot.start_snapshot_refresher(app)
    assert parked.wait(timeout=10), "refresher never reached its sleep"
    thread.join(timeout=10)
    assert not thread.is_alive(), "the stop event did not end the refresher loop"

    assert observed["checked_out"] == 0
    assert observed["has_app_context"] is False
    assert observed["seconds"] >= metrics_snapshot._MIN_REFRESH_INTERVAL


def test_a_failed_pass_does_not_kill_the_refresher(
    app, restore_snapshot, stopped_refresher, monkeypatch, caplog,
):
    """An unhandled exception would stop every later pass, and the snapshot would
    then age forever with nothing logged after the first traceback."""
    passes = []

    def boom():
        passes.append(1)
        raise RuntimeError("pass exploded")

    parked = threading.Event()

    def fake_sleep(seconds, stop):
        # Let a second pass start, so the test shows the loop survived the first
        # failure rather than merely that the first one was logged.
        if len(passes) >= 2:
            parked.set()
            stop.set()

    monkeypatch.setattr(metrics_snapshot, "refresh_snapshot", boom)
    monkeypatch.setattr(metrics_snapshot, "_sleep", fake_sleep)
    with caplog.at_level("ERROR"):
        thread = metrics_snapshot.start_snapshot_refresher(app)
        assert parked.wait(timeout=10)
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert "metrics snapshot refresh error" in caplog.text


def test_stop_snapshot_refresher_ends_the_thread_without_waiting_out_the_interval(
    app, restore_snapshot,
):
    """The shutdown path, with the real ``_sleep``.

    The loop parks on an event, not on ``time.sleep(interval)``, so a stop is
    honoured immediately instead of up to a full refresh interval later (or
    never, which is what leaves a thread running into interpreter teardown).
    """
    started = time.monotonic()
    thread = metrics_snapshot.start_snapshot_refresher(app)
    metrics_snapshot.stop_snapshot_refresher(timeout=10)
    assert not thread.is_alive()
    assert time.monotonic() - started < metrics_snapshot._MIN_REFRESH_INTERVAL


def test_refresh_interval_scales_with_the_worker_count(app):
    """Per-process refreshing multiplies the statement rate by the worker count,
    so the interval has to scale with it rather than being flat."""
    original = app.config.get("POOL_TOPOLOGY")
    try:
        app.config["POOL_TOPOLOGY"] = {"workers": 1, "replicas": 1}
        assert metrics_snapshot.refresh_interval(app) == metrics_snapshot._DEFAULT_REFRESH_INTERVAL
        app.config["POOL_TOPOLOGY"] = {"workers": 4, "replicas": 2}
        assert metrics_snapshot.refresh_interval(app) == 4 * metrics_snapshot._ASSUMED_SCRAPE_INTERVAL
    finally:
        app.config["POOL_TOPOLOGY"] = original
    assert metrics_snapshot._MIN_REFRESH_INTERVAL <= metrics_snapshot._DEFAULT_REFRESH_INTERVAL


def _app_context_withs(tree):
    """Every ``with app.app_context():`` node in *tree*, with its ancestors."""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            call = item.context_expr
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) \
                    and call.func.attr == "app_context":
                ancestors = []
                cur = node
                while cur in parents:
                    cur = parents[cur]
                    ancestors.append(cur)
                found.append((node, ancestors))
    return found


def test_app_context_is_only_ever_entered_inside_the_loop():
    """Static guard, in the shape of tests/unit/test_no_stream_with_context.py.

    A context pushed outside the loop lives across every sleep — the leak this
    module was written to avoid — and it reads as a harmless optimisation.
    """
    tree = ast.parse(MODULE_PATH.read_text())
    withs = _app_context_withs(tree)
    assert withs, "no `with app.app_context():` found — has the refresher moved?"
    for node, ancestors in withs:
        assert any(isinstance(a, (ast.While, ast.For)) for a in ancestors), (
            f"{MODULE_PATH.name}:{node.lineno}: app_context() is entered outside the refresh "
            "loop, so it spans the sleep between passes and holds a DB session with it"
        )


def test_the_sleep_never_happens_inside_an_app_context():
    tree = ast.parse(MODULE_PATH.read_text())
    for node, _ in _app_context_withs(tree):
        for inner in ast.walk(node):
            name = getattr(inner, "id", None) or getattr(inner, "attr", None)
            assert name != "_sleep", (
                f"{MODULE_PATH.name}:{inner.lineno}: the refresher sleeps while holding an "
                "app context — exactly the 1.22.0 idle-in-transaction leak"
            )


def test_exactly_one_app_context_per_pass():
    """One context, entered inside the loop — not one per statement, and not one
    hoisted around several passes."""
    assert len(_app_context_withs(ast.parse(MODULE_PATH.read_text()))) == 1


def test_the_refresher_is_never_elected():
    """Electing a single runner would be a fleet-breaking bug: the non-holders
    would hold an empty snapshot forever, and a counter that disappears and
    reappears reads as a reset, so rate() invents a spike on every recovery.
    Elect only work whose result lands in the DB; this result is in memory.

    Checked against the parsed module, not the text: the docstring names
    ``flock`` and ``health.py`` precisely to say what this module must not do.
    """
    tree = ast.parse(MODULE_PATH.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for module in ("fcntl", "lumen.services.health"):
        assert module not in imported, (
            f"{MODULE_PATH.name} imports {module!r} — the refresher must run in every "
            "process, never behind a single-runner election"
        )
    names = {
        getattr(node, "id", None) or getattr(node, "attr", None)
        for node in ast.walk(tree)
    }
    assert not names & {"flock", "lockf", "acquire_heartbeat", "run_health_pass"}, (
        f"{MODULE_PATH.name} takes a lock or reuses the health election"
    )


def _start_refresher_calls(tree):
    """Every ``start_snapshot_refresher(...)`` call statement, with its ancestors."""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "start_snapshot_refresher"):
            continue
        ancestors = []
        cur = node
        while cur in parents:
            cur = parents[cur]
            ancestors.append(cur)
        found.append((node, ancestors))
    return found


def test_create_app_starts_the_refresher_in_every_serving_process():
    """Two contract items in one static check.

        It must start outside the ``BACKGROUND_WORKER`` guard — that switch keeps
        extra workers from duplicating *shared* work, and the snapshot is
        per-process in-memory state, so a guarded worker would serve an empty
        /metrics forever. And it must not be gated on ``api.prometheus.enabled``:
        the snapshot is the application's own cache of its own state, read by pages
        that have nothing to do with Prometheus. The sole exception is a Flask CLI
        command that never serves requests and must start no background threads.
    """
    import lumen

    tree = ast.parse(Path(lumen.__file__).read_text())
    calls = _start_refresher_calls(tree)
    assert len(calls) == 1, "expected exactly one start_snapshot_refresher() call in create_app"
    node, ancestors = calls[0]
    guards = [a for a in ancestors if isinstance(a, (ast.If, ast.Try, ast.While, ast.For))]
    assert len(guards) == 1 and isinstance(guards[0], ast.If) \
        and ast.unparse(guards[0].test) == "not non_serving_cli", (
        f"lumen/__init__.py:{node.lineno}: start_snapshot_refresher() must be guarded only "
        "for a non-serving Flask CLI, never by BACKGROUND_WORKER or api.prometheus.enabled"
    )


# --- TimescaleDB health (#173) ---------------------------------------------------

_FINISHED = datetime(2026, 10, 30, 5, 0)


def _health(failed=(), failures=None, drop=(), materialized_only=()):
    """A TimescaleHealth with every expected object, adjusted by the arguments."""
    failures = failures or {}
    aggregates = tuple(
        Aggregate(view, view in materialized_only, f"_materialized_hypertable_{i}")
        for i, view in enumerate(timescale_health.AGGREGATES)
    )
    jobs = tuple(
        (name, Job(1000 + i, proc, target, "Failed" if name in failed else "Success",
                   failures.get(name, 0), _FINISHED))
        for i, (name, (proc, target)) in enumerate(timescale_health.EXPECTED_JOBS.items())
        if name not in drop
    )
    return TimescaleHealth(
        installed_version="2.27.2", available_version="2.27.2",
        request_logs_hypertable=True, aggregates=aggregates, jobs=jobs,
    )


def _timescale_messages(caplog):
    return [(r.levelno, r.getMessage()) for r in caplog.records
            if r.name == metrics_snapshot.logger.name and "TimescaleDB" in r.getMessage()]


def _run_passes(app, monkeypatch, count):
    """Run the real refresher loop for *count* passes; returns the passes that checked health."""
    checked, passes = [], []
    monkeypatch.setattr(metrics_snapshot, "refresh_timescale_health",
                        lambda: checked.append(len(passes)))

    def fake_sleep(seconds, stop):
        passes.append(1)
        if len(passes) >= count:
            stop.set()

    monkeypatch.setattr(metrics_snapshot, "_sleep", fake_sleep)
    thread = metrics_snapshot.start_snapshot_refresher(app)
    thread.join(timeout=10)
    assert not thread.is_alive()
    return checked


def test_health_is_checked_on_the_first_pass_then_every_300s_of_passes(
    app, restore_snapshot, stopped_refresher, monkeypatch,
):
    original = app.config.get("POOL_TOPOLOGY")
    try:
        # 60 s passes: ceil(300 / 60) = 5.
        app.config["POOL_TOPOLOGY"] = {"workers": 1, "replicas": 1}
        assert _run_passes(app, monkeypatch, 11) == [0, 5, 10]
        # 4 workers: 120 s passes, ceil(300 / 120) = 3.
        app.config["POOL_TOPOLOGY"] = {"workers": 4, "replicas": 1}
        assert _run_passes(app, monkeypatch, 7) == [0, 3, 6]
    finally:
        app.config["POOL_TOPOLOGY"] = original


def test_a_raising_health_check_leaves_the_snapshot_fresh(
    app, restore_snapshot, stopped_refresher, monkeypatch, caplog,
):
    """The check runs after the snapshot, in its own try: it can neither delay nor stale it,
    and a check that keeps failing the same way is logged once."""
    metrics_snapshot._snapshot = metrics_snapshot._UNPRIMED
    snapshots = []

    def boom():
        snapshots.append(metrics_snapshot.get_snapshot())
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr(metrics_snapshot, "refresh_timescale_health", boom)
    monkeypatch.setattr(metrics_snapshot, "_TIMESCALE_HEALTH_INTERVAL", 1.0)  # due every pass

    def fake_sleep(seconds, stop):
        if len(snapshots) >= 3:
            stop.set()

    monkeypatch.setattr(metrics_snapshot, "_sleep", fake_sleep)
    with caplog.at_level("WARNING"):
        thread = metrics_snapshot.start_snapshot_refresher(app)
        thread.join(timeout=10)
    assert not thread.is_alive()

    # Every check saw a snapshot published by its own pass.
    assert all(s.primed for s in snapshots)
    assert len({s.captured_at for s in snapshots}) == 3
    assert metrics_snapshot.get_snapshot() is snapshots[-1]
    assert metrics_snapshot.get_timescale_health() is None
    failures = [r for r in caplog.records if "Could not check TimescaleDB health" in r.getMessage()]
    assert len(failures) == 1
    assert "metrics snapshot refresh error" not in caplog.text


def test_health_check_holds_no_connection_and_no_context_while_sleeping(
    app, restore_snapshot, stopped_refresher, monkeypatch,
):
    """The health check shares the pass's one app context, so it is released before the sleep too."""
    with app.app_context():
        pool = db.engine.pool
        monkeypatch.setattr(db.engine.dialect, "name", "postgresql")

    def check(session):
        session.execute(text("SELECT 1"))  # takes a pooled connection, as the real check does
        return _health()

    monkeypatch.setattr(timescale_health, "check", check)
    observed = {}

    def fake_sleep(seconds, stop):
        observed["checked_out"] = pool.checkedout()
        observed["has_app_context"] = has_app_context()
        stop.set()

    monkeypatch.setattr(metrics_snapshot, "_sleep", fake_sleep)
    thread = metrics_snapshot.start_snapshot_refresher(app)
    thread.join(timeout=10)
    assert not thread.is_alive()

    assert metrics_snapshot.get_timescale_health().health == _health()
    assert observed == {"checked_out": 0, "has_app_context": False}


def test_sqlite_publishes_no_health(app):
    with app.app_context():
        assert metrics_snapshot.refresh_timescale_health() is None
    assert metrics_snapshot.get_timescale_health() is None


def test_health_check_is_still_a_separate_cadence_from_the_snapshot_pass():
    """The TimescaleHealth must not become a snapshot field: a pass is capped at five statements."""
    fields = {f.name for f in dataclasses.fields(metrics_snapshot.MetricsSnapshot)}
    assert not any("timescale" in name for name in fields)


def test_a_persisting_problem_is_logged_once(caplog):
    broken = _health(drop=("refresh:request_counts_hourly",))
    with caplog.at_level("INFO"):
        metrics_snapshot._log_health_changes(_health(), broken)
        metrics_snapshot._log_health_changes(broken, broken)
        metrics_snapshot._log_health_changes(broken, broken)
    assert _timescale_messages(caplog) == [(
        logging.WARNING,
        "TimescaleDB: Background job refresh:request_counts_hourly "
        "(policy_refresh_continuous_aggregate on request_counts_hourly) is missing.",
    )]


def test_without_a_baseline_every_problem_is_new(caplog):
    """The startup check failed, so nothing was logged yet: the first check logs it all."""
    broken = TimescaleHealth(installed_version=None, available_version="2.27.2")
    with caplog.at_level("INFO"):
        metrics_snapshot._log_health_changes(None, broken)
        metrics_snapshot._log_health_changes(None, _health())
    assert _timescale_messages(caplog) == [
        (logging.ERROR, f"TimescaleDB: {timescale_health.EXTENSION_MISSING}"),
    ]


def test_a_cleared_problem_is_logged(caplog):
    broken = _health(materialized_only=("request_metrics_1m",))
    with caplog.at_level("INFO"):
        metrics_snapshot._log_health_changes(broken, _health())
    [(level, message)] = _timescale_messages(caplog)
    assert level == logging.INFO
    assert message.startswith("TimescaleDB problem cleared: Continuous aggregate request_metrics_1m")


def test_rising_failures_are_logged_even_when_the_last_run_succeeded(caplog):
    """A job that failed and then recovered between two checks only shows as a rising count."""
    name = "compression:request_logs"
    with caplog.at_level("INFO"):
        metrics_snapshot._log_health_changes(_health(failures={name: 2}), _health(failures={name: 5}))
        metrics_snapshot._log_health_changes(_health(failures={name: 5}), _health(failures={name: 5}))
    [(level, message)] = _timescale_messages(caplog)
    assert level == logging.WARNING
    assert "compression:request_logs (job 1003) failed 3 time(s) since the last check; 5 failures" in message


def test_a_failing_job_is_logged_again_only_when_its_count_rises(caplog):
    name = "refresh:request_metrics_1m"
    first = _health(failed=(name,), failures={name: 1})
    again = _health(failed=(name,), failures={name: 2})
    with caplog.at_level("INFO"):
        metrics_snapshot._log_health_changes(_health(), first)
        metrics_snapshot._log_health_changes(first, first)
        metrics_snapshot._log_health_changes(first, again)
    messages = [m for _, m in _timescale_messages(caplog)]
    assert len(messages) == 2
    assert messages[0].endswith("failed its last run; 1 failures in total.")
    assert messages[1].endswith("failed its last run; 2 failures in total.")
