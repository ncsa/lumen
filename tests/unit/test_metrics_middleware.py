"""Tests for make_metrics_middleware and _normalize_path."""


# ---------------------------------------------------------------------------
# _normalize_path
# ---------------------------------------------------------------------------

def test_normalize_path_no_ids():
    from lumen.blueprints.metrics.middleware import _normalize_path
    assert _normalize_path("/admin/groups") == "/admin/groups"


def test_normalize_path_single_id():
    from lumen.blueprints.metrics.middleware import _normalize_path
    assert _normalize_path("/admin/groups/42") == "/admin/groups/{id}"


def test_normalize_path_multiple_ids():
    from lumen.blueprints.metrics.middleware import _normalize_path
    assert _normalize_path("/admin/users/7/access/99/delete") == "/admin/users/{id}/access/{id}/delete"


def test_normalize_path_root():
    from lumen.blueprints.metrics.middleware import _normalize_path
    assert _normalize_path("/") == "/"


def test_normalize_path_empty():
    from lumen.blueprints.metrics.middleware import _normalize_path
    assert _normalize_path("") == ""


def test_normalize_path_leading_id_only():
    from lumen.blueprints.metrics.middleware import _normalize_path
    assert _normalize_path("/123") == "/{id}"


# ---------------------------------------------------------------------------
# make_metrics_middleware
# ---------------------------------------------------------------------------

def _fake_environ(path="/", method="GET", rule=None):
    """A minimal WSGI environ.

    ``rule`` is what ``create_app``'s teardown_request hook would have stashed
    for a routed request; leaving it None is an unrouted request (a scanner, a
    404), which is what a bare fake app produces.
    """
    environ = {"PATH_INFO": path, "REQUEST_METHOD": method}
    if rule is not None:
        environ["lumen.url_rule"] = rule
    return environ


def _capture_labels(monkeypatch):
    """Record the (method, path_template) of every counter/histogram label call."""
    from lumen.blueprints.metrics import middleware as mw

    recorded = []
    for metric in (mw._http_requests, mw._http_latency):
        orig = metric.labels

        def spy(_orig=orig, **kwargs):
            recorded.append((kwargs["method"], kwargs["path_template"]))
            return _orig(**kwargs)

        monkeypatch.setattr(metric, "labels", spy)
    return recorded


def test_middleware_passes_through_response():
    from lumen.blueprints.metrics.middleware import make_metrics_middleware

    def fake_app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"hello"]

    wrapped = make_metrics_middleware(fake_app)
    status_seen = []

    result = wrapped(_fake_environ("/chat"), lambda s, h, *_: status_seen.append(s))
    assert list(result) == [b"hello"]
    assert status_seen == ["200 OK"]


def test_middleware_captures_4xx_status():
    from lumen.blueprints.metrics.middleware import make_metrics_middleware

    def fake_app(environ, start_response):
        start_response("404 Not Found", [])
        return [b""]

    wrapped = make_metrics_middleware(fake_app)
    status_seen = []
    wrapped(_fake_environ("/missing"), lambda s, h, *_: status_seen.append(s))
    assert status_seen == ["404 Not Found"]


# ---------------------------------------------------------------------------
# Label cardinality — both dimensions. Every label value must come from a
# bounded set, or a scanner mints unbounded series in every worker process's
# memory and in the TSDB.
# ---------------------------------------------------------------------------

def _drive(wrapped, environ):
    body = wrapped(environ, lambda *a: None)
    list(body)
    body.close()


def _null_app(environ, start_response):
    start_response("404 Not Found", [])
    return []


def test_unmatched_paths_all_share_one_label_value(monkeypatch):
    """50 distinct junk paths must produce exactly one path_template value.

    This is the pre-existing bug Phase 1 makes urgent: the old label was the
    request path with numbers folded, so /.env, /wp-admin, /cgi-bin/... each
    minted a new series — unbounded, and multiplied by the worker count.
    """
    from lumen.blueprints.metrics import middleware as mw

    recorded = _capture_labels(monkeypatch)
    wrapped = mw.make_metrics_middleware(_null_app)
    for i in range(50):
        _drive(wrapped, _fake_environ(f"/wp-admin/setup-{i}.php"))

    assert len(recorded) == 100  # one counter + one histogram call per request
    assert {path for _, path in recorded} == {"<unmatched>"}

    from prometheus_client import REGISTRY, generate_latest
    assert "wp-admin" not in generate_latest(REGISTRY).decode()


def test_matched_route_is_labelled_by_its_rule(app, monkeypatch):
    """A routed request is labelled with the url_rule, not the concrete path."""
    import re

    from werkzeug.test import EnvironBuilder

    from lumen.blueprints.metrics import middleware as mw

    rule = next(
        r for r in app.url_map.iter_rules()
        if "<int:" in r.rule and "GET" in r.methods
    )
    concrete = re.sub(r"<int:[^>]+>", "987654", rule.rule)

    recorded = _capture_labels(monkeypatch)
    wrapped = mw.make_metrics_middleware(app.wsgi_app)
    environ = EnvironBuilder(path=concrete).get_environ()
    _drive(wrapped, environ)

    # The teardown_request hook registered in create_app is the only way this
    # value can reach the middleware: Flask pops the request context before
    # wsgi_app returns, so the closures cannot read request.url_rule at all.
    assert environ["lumen.url_rule"] == rule.rule
    assert {path for _, path in recorded} == {rule.rule}
    assert "987654" not in rule.rule


def test_unrouted_request_through_the_real_app_is_unmatched(app, monkeypatch):
    """A 404 has no url_rule; the hook stashes None and the label falls back."""
    from werkzeug.test import EnvironBuilder

    from lumen.blueprints.metrics import middleware as mw

    recorded = _capture_labels(monkeypatch)
    wrapped = mw.make_metrics_middleware(app.wsgi_app)
    environ = EnvironBuilder(path="/.env").get_environ()
    _drive(wrapped, environ)

    assert environ["lumen.url_rule"] is None
    assert {path for _, path in recorded} == {"<unmatched>"}


def test_junk_methods_all_share_one_label_value(monkeypatch):
    """HTTP methods are RFC token grammar, so the method label is unbounded too.

    The path fix alone would pass a gate that only scans junk paths while the
    same explosion continued through the other dimension.
    """
    from lumen.blueprints.metrics import middleware as mw

    recorded = _capture_labels(monkeypatch)
    wrapped = mw.make_metrics_middleware(_null_app)
    for i in range(20):
        _drive(wrapped, _fake_environ("/", f"FOOBARBAZ{i}"))

    assert {method for method, _ in recorded} == {"<other>"}


def test_standard_methods_are_kept_verbatim(monkeypatch):
    """The allow-list must not flatten the methods anyone actually queries by."""
    from lumen.blueprints.metrics import middleware as mw

    recorded = _capture_labels(monkeypatch)
    wrapped = mw.make_metrics_middleware(_null_app)
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        _drive(wrapped, _fake_environ("/", method))

    assert {m for m, _ in recorded} == {
        "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"
    }


def test_middleware_warns_when_a_context_survives_the_request(app, caplog):
    """A request that leaves an app context pushed past the response body's
    close() has escaped teardown; the middleware names it at that moment."""
    import logging

    from lumen.blueprints.metrics.middleware import make_metrics_middleware

    leaked = []

    def leaking_app(environ, start_response):
        ctx = app.app_context()
        ctx.push()  # never popped — the leak under investigation
        leaked.append(ctx)
        start_response("200 OK", [])
        return [b"ok"]

    wrapped = make_metrics_middleware(leaking_app)
    with caplog.at_level(logging.WARNING, logger="lumen.blueprints.metrics.middleware"):
        body = wrapped(_fake_environ("/v1/models"), lambda *a: None)
        list(body)
        # Only close() is the end of the request (the fixture's own ambient
        # context may additionally be reported at request start).
        assert not any("still current after" in r.getMessage() for r in caplog.records)
        body.close()
    leftover = [r.getMessage() for r in caplog.records if "still current after" in r.getMessage()]
    assert len(leftover) == 1
    assert "still current after GET /v1/models" in leftover[0]
    assert "stranded" in leftover[0]
    # Also kept for /metrics/debug, keyed to the leaked context's id.
    from lumen.services.ctx_probe import format_context_anomalies
    report = format_context_anomalies()
    assert f"leftover-context  ctx=0x{id(leaked[0]):x}" in report
    assert "left behind by GET /v1/models" in report
    leaked[0].pop()  # clean up for the other tests


def test_middleware_is_silent_for_a_balanced_request(app, caplog):
    """An ambient context around the request (test fixtures, nested dispatch)
    is the close()-check baseline, not a leftover — though it is reported once
    as ambient-at-start, since in production it means a poisoned thread."""
    import logging

    from lumen.blueprints.metrics.middleware import make_metrics_middleware

    def clean_app(environ, start_response):
        ctx = app.app_context()
        ctx.push()
        ctx.pop()
        start_response("200 OK", [])
        return [b"ok"]

    wrapped = make_metrics_middleware(clean_app)
    with app.app_context():  # ambient context present the whole time
        with caplog.at_level(logging.WARNING, logger="lumen.blueprints.metrics.middleware"):
            body = wrapped(_fake_environ("/v1/models"), lambda *a: None)
            list(body)
            body.close()
            # A second request on the same poisoned baseline reports nothing new.
            body2 = wrapped(_fake_environ("/v1/models"), lambda *a: None)
            list(body2)
            body2.close()
    messages = [r.getMessage() for r in caplog.records]
    assert not any("still current after" in m for m in messages)  # no leftover
    ambient = [m for m in messages if "already current at the start" in m]
    assert len(ambient) == 1  # reported once per context, not per request


def test_middleware_checks_the_exception_path(app, caplog):
    """A request that raises never gets a body close(); the leftover check
    must run on the exception path instead."""
    import logging

    import pytest

    from lumen.blueprints.metrics.middleware import make_metrics_middleware
    from lumen.services.ctx_probe import format_context_anomalies

    leaked = []

    def exploding_leaking_app(environ, start_response):
        ctx = app.app_context()
        ctx.push()  # never popped
        leaked.append(ctx)
        raise RuntimeError("boom")

    wrapped = make_metrics_middleware(exploding_leaking_app)
    with caplog.at_level(logging.WARNING, logger="lumen.blueprints.metrics.middleware"):
        with pytest.raises(RuntimeError, match="boom"):
            wrapped(_fake_environ("/v1/models"), lambda *a: None)
    assert any("raised" in r.getMessage() for r in caplog.records)
    assert f"leftover-context-after-exception  ctx=0x{id(leaked[0]):x}" in format_context_anomalies()
    leaked[0].pop()  # clean up


def test_middleware_heals_a_poisoned_thread(caplog):
    """Outside tests, an ambient context at request start is neutralized: the
    session registered under its key is closed and the contextvar cleared, so
    this and every later request on the thread pushes a fresh context."""
    import logging
    from unittest.mock import MagicMock

    from flask import Flask
    from flask.globals import _cv_app

    from lumen.blueprints.metrics.middleware import make_metrics_middleware
    from lumen.extensions import db

    prod_app = Flask("prod-like")  # testing defaults to False → heal path
    prior = _cv_app.get(None)  # the test fixture's own context, restored below
    ctx = prod_app.app_context()
    ctx.push()
    stuck_session = MagicMock()
    db.session.registry.registry[id(ctx)] = stuck_session

    def clean_app(environ, start_response):
        start_response("200 OK", [])
        return [b"ok"]

    wrapped = make_metrics_middleware(clean_app)
    try:
        with caplog.at_level(logging.WARNING, logger="lumen.blueprints.metrics.middleware"):
            body = wrapped(_fake_environ("/v1/models"), lambda *a: None)
            list(body)
            body.close()
        assert _cv_app.get(None) is None  # contextvar cleared
        assert id(ctx) not in db.session.registry.registry
        stuck_session.close.assert_called_once()
        assert any("(healing)" in r.getMessage() for r in caplog.records)
    finally:
        # The heal cleared the contextvar; drop the orphaned push token rather
        # than popping (pop would assert on the mismatched current context),
        # and restore the fixture's own context so its teardown pops cleanly.
        ctx._cv_tokens.clear()
        db.session.registry.registry.pop(id(ctx), None)
        if prior is not None:
            _cv_app.set(prior)


def test_middleware_records_500_on_app_exception():
    """If the wrapped app raises, status defaults to '500' and the exception propagates."""
    import pytest

    from lumen.blueprints.metrics.middleware import make_metrics_middleware

    def exploding_app(environ, start_response):
        raise RuntimeError("boom")

    wrapped = make_metrics_middleware(exploding_app)
    with pytest.raises(RuntimeError, match="boom"):
        wrapped(_fake_environ("/crash"), lambda *a: None)


# ---------------------------------------------------------------------------
# observe_stream_abort / lumen_stream_aborts_total
# ---------------------------------------------------------------------------

def _abort_count(source, reason):
    from prometheus_client import REGISTRY
    return REGISTRY.get_sample_value(
        "lumen_stream_aborts_total", {"source": source, "reason": reason}) or 0.0


def test_observe_stream_abort_increments_per_label_pair():
    """Each (source, reason) is its own series — 'clients are leaving' and 'the
    backend is broken' must not be summed into one number."""
    from lumen.blueprints.metrics.middleware import observe_stream_abort

    before = {
        ("chat", "disconnect"): _abort_count("chat", "disconnect"),
        ("api", "disconnect"): _abort_count("api", "disconnect"),
        ("api", "upstream_error"): _abort_count("api", "upstream_error"),
    }
    observe_stream_abort("api", "upstream_error")
    observe_stream_abort("api", "upstream_error")
    observe_stream_abort("chat", "disconnect")

    assert _abort_count("api", "upstream_error") == before[("api", "upstream_error")] + 2
    assert _abort_count("chat", "disconnect") == before[("chat", "disconnect")] + 1
    assert _abort_count("api", "disconnect") == before[("api", "disconnect")]


def test_stream_abort_counter_is_on_the_default_registry():
    """It must live on the default registry like the HTTP counters, so
    prometheus_client's multiprocess mode picks it up and /metrics exposes it."""
    from prometheus_client import REGISTRY, generate_latest

    from lumen.blueprints.metrics.middleware import observe_stream_abort

    observe_stream_abort("chat", "disconnect")
    scrape = generate_latest(REGISTRY).decode()
    assert "# TYPE lumen_stream_aborts_total counter" in scrape
    assert 'lumen_stream_aborts_total{reason="disconnect",source="chat"}' in scrape


# ---------------------------------------------------------------------------
# Streaming latency — the histogram must time the response the user waited for,
# not the microseconds it took to build a generator.
# ---------------------------------------------------------------------------

def _latency_sum(path):
    """Observed seconds for a path label, or 0.0 before any observation."""
    from prometheus_client import REGISTRY
    return REGISTRY.get_sample_value(
        "lumen_http_request_duration_seconds_sum",
        {"method": "POST", "path_template": path},
    ) or 0.0


def test_streaming_latency_is_measured_over_the_whole_body(monkeypatch):
    """A streaming view returns its generator instantly; the work happens later.

    Timing `wsgi_app()` therefore measured generator construction, so every SSE
    request — the ones this service exists to serve — recorded a near-zero
    duration however long the client actually waited. The observation belongs at
    close(), the last moment the request runs on this thread.
    """
    import time

    from lumen.blueprints.metrics.middleware import make_metrics_middleware

    body_time = 0.25
    path = "/stream-latency-probe"

    def streaming_app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/event-stream")])

        def generate():
            time.sleep(body_time)      # the part the user waits for
            yield b"data: done\n\n"

        return generate()

    before = _latency_sum(path)
    wrapped = make_metrics_middleware(streaming_app)
    body = wrapped(_fake_environ(path, "POST", rule=path), lambda s, h, *_: None)
    assert list(body) == [b"data: done\n\n"]
    body.close()

    observed = _latency_sum(path) - before
    assert observed >= body_time, (
        f"observed {observed:.3f}s for a response whose body took {body_time}s — "
        "the histogram is timing generator construction, not the request"
    )


def test_latency_is_still_recorded_when_the_app_raises(monkeypatch):
    """No body is returned, so nothing will ever call close() — the failure path
    has to observe its own latency or 500s vanish from the histogram."""
    from lumen.blueprints.metrics.middleware import make_metrics_middleware

    path = "/raising-latency-probe"

    def exploding_app(environ, start_response):
        raise RuntimeError("boom")

    import pytest

    before = _latency_sum(path)
    wrapped = make_metrics_middleware(exploding_app)
    with pytest.raises(RuntimeError):
        wrapped(_fake_environ(path, "POST", rule=path), lambda s, h, *_: None)
    assert _latency_sum(path) > before


def test_latency_is_observed_once_per_request():
    """close() can be called more than once; the histogram must not double-count."""
    from prometheus_client import REGISTRY

    from lumen.blueprints.metrics.middleware import make_metrics_middleware

    path = "/double-close-probe"

    def fake_app(environ, start_response):
        start_response("200 OK", [])
        return [b"x"]

    def count():
        return REGISTRY.get_sample_value(
            "lumen_http_request_duration_seconds_count",
            {"method": "POST", "path_template": path},
        ) or 0.0

    before = count()
    body = make_metrics_middleware(fake_app)(
        _fake_environ(path, "POST", rule=path), lambda s, h, *_: None
    )
    list(body)
    body.close()
    body.close()
    assert count() - before == 1


# ---------------------------------------------------------------------------
# Multiprocess gauge invariant — static guard, same shape as
# tests/unit/test_no_stream_with_context.py
# ---------------------------------------------------------------------------

def test_every_gauge_declares_a_multiprocess_mode():
    """Under PROMETHEUS_MULTIPROC_DIR every process writes its own mmap file and
    the scrape merges them. A Gauge with no explicit multiprocess_mode gets the
    library default ("all"), which exposes one series per pid instead of the one
    fleet number the dashboard is built on. Counter/Histogram need nothing —
    summing is the only correct merge for them.
    """
    import ast
    from pathlib import Path

    lumen_dir = Path(__file__).resolve().parents[2] / "lumen"
    offenders = []
    for path in sorted(lumen_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "Gauge":
                continue
            if not any(kw.arg == "multiprocess_mode" for kw in node.keywords):
                offenders.append(f"{path.relative_to(lumen_dir.parent)}:{node.lineno}")
    assert not offenders, (
        "every Gauge must pass an explicit multiprocess_mode (see the invariant "
        "comment in lumen/blueprints/metrics/middleware.py). Found:\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# lumen.queue_wait — computed by the before_request hook in create_app from the
# mark the ASGI bridge leaves in environ.
# ---------------------------------------------------------------------------

def test_queue_wait_is_computed_from_the_bridge_mark(app):
    import time

    from werkzeug.test import EnvironBuilder

    environ = EnvironBuilder(path="/healthz").get_environ()
    environ["lumen.t0_monotonic"] = time.monotonic() - 0.5
    app.wsgi_app(environ, lambda *a: None)
    assert environ["lumen.queue_wait"] >= 0.5


def test_queue_wait_is_absent_when_the_bridge_did_not_mark_the_request(app):
    """The test client and the dev server never set the mark; the hook must not
    invent a number for a request that never queued."""
    from werkzeug.test import EnvironBuilder

    environ = EnvironBuilder(path="/healthz").get_environ()
    app.wsgi_app(environ, lambda *a: None)
    assert "lumen.queue_wait" not in environ


# ---------------------------------------------------------------------------
# lumen_wsgi_queue_wait_seconds — the histogram fed from that environ key.
# ---------------------------------------------------------------------------

def _queue_wait_sample(suffix):
    """A sample of the unlabelled queue-wait histogram, 0.0 before any observation."""
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(f"lumen_wsgi_queue_wait_seconds_{suffix}") or 0.0


def test_queue_wait_is_observed_when_the_environ_key_is_present():
    import pytest

    from lumen.blueprints.metrics.middleware import make_metrics_middleware

    def queued_app(environ, start_response):
        environ["lumen.queue_wait"] = 0.75  # what the before_request hook writes
        start_response("200 OK", [])
        return [b"ok"]

    before_count = _queue_wait_sample("count")
    before_sum = _queue_wait_sample("sum")
    _drive(make_metrics_middleware(queued_app), _fake_environ("/v1/models", "POST"))

    assert _queue_wait_sample("count") - before_count == 1
    assert _queue_wait_sample("sum") - before_sum == pytest.approx(0.75)


def test_queue_wait_is_not_observed_when_the_environ_key_is_absent():
    """Under the Flask test client and the Werkzeug dev server nothing queued.

    Observing 0.0 there would fill the histogram with a queue that does not
    exist and drag every percentile towards zero.
    """
    from lumen.blueprints.metrics.middleware import make_metrics_middleware

    before = _queue_wait_sample("count")
    _drive(make_metrics_middleware(_null_app), _fake_environ("/v1/models", "POST"))
    assert _queue_wait_sample("count") == before


# ---------------------------------------------------------------------------
# lumen_wsgi_queue_depth / _threads_busy / _threads_total — the claim made in
# the comment beside them is that they are per-process live values that SUM
# across live workers and lose a dead worker's contribution. Proving that needs
# real processes: prometheus_client binds its value class at import time from
# PROMETHEUS_MULTIPROC_DIR, so a fork of this test process has the
# single-process class already bound (same reason as
# tests/unit/test_metrics_multiprocess.py).
# ---------------------------------------------------------------------------

# Sets the bridge's counters, then drives one request through the middleware —
# so what is asserted below is the whole chain (accessor -> gauge -> mmap file
# -> merged scrape), not just a .set() call.
_GAUGE_CHILD = """
import os, sys

from lumen.services import wsgi_disconnect as wd

wd._queued, wd._running, wd._threads_total = (int(a) for a in sys.argv[1:4])

# Imported only after the counters are set and with PROMETHEUS_MULTIPROC_DIR
# already in the environment: importing this module constructs the gauges, and
# prometheus_client opens each mmap eagerly at construction.
from lumen.blueprints.metrics import middleware as mw


def app(environ, start_response):
    start_response("200 OK", [])
    return [b""]


body = mw.make_metrics_middleware(app)(
    {"PATH_INFO": "/", "REQUEST_METHOD": "GET"}, lambda *a: None
)
list(body)
body.close()
print(os.getpid())
"""


def _run_gauge_child(multiproc_dir, queued, running, threads):
    """Run one request in a separate process; return its (now dead) pid."""
    import os
    import subprocess
    import sys

    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(multiproc_dir)}
    result = subprocess.run(
        [sys.executable, "-c", _GAUGE_CHILD, str(queued), str(running), str(threads)],
        env=env, capture_output=True, text=True, check=True,
    )
    return int(result.stdout.strip())


def _merged(multiproc_dir):
    from prometheus_client import CollectorRegistry
    from prometheus_client.multiprocess import MultiProcessCollector

    registry = CollectorRegistry()
    MultiProcessCollector(registry, path=str(multiproc_dir))
    return registry


def _series(registry, name):
    return [s for m in registry.collect() if m.name == name for s in m.samples]


def test_wsgi_pool_gauges_are_one_fleet_number_not_one_series_per_process(tmp_path):
    """livesum, not the library default: two workers must merge into one sample.

    With the default mode ("all") each process keeps its own series tagged with
    its pid, so a dashboard panel showing "queue depth" would show four lines at
    four processes and no total — the failure the file's invariant comment
    exists to prevent.
    """
    _run_gauge_child(tmp_path, 3, 7, 10)
    _run_gauge_child(tmp_path, 2, 5, 10)

    registry = _merged(tmp_path)
    for name in ("lumen_wsgi_queue_depth", "lumen_wsgi_threads_busy",
                 "lumen_wsgi_threads_total"):
        samples = _series(registry, name)
        assert len(samples) == 1, f"{name} exposed {len(samples)} series, not a fleet total"
        assert samples[0].labels == {}, f"{name} is labelled per process: {samples[0].labels}"

    assert registry.get_sample_value("lumen_wsgi_queue_depth") == 5.0
    assert registry.get_sample_value("lumen_wsgi_threads_busy") == 12.0
    # Fleet capacity, which is what the depth has to be read against: two
    # processes of ten threads is twenty threads, not ten.
    assert registry.get_sample_value("lumen_wsgi_threads_total") == 20.0


def test_a_dead_worker_stops_contributing_to_the_pool_gauges(tmp_path):
    """A gauge is a claim about now, and a dead worker's queue is not queued."""
    from prometheus_client.multiprocess import mark_process_dead

    pid_a = _run_gauge_child(tmp_path, 3, 7, 10)
    pid_b = _run_gauge_child(tmp_path, 2, 5, 10)
    assert pid_a != pid_b
    assert _merged(tmp_path).get_sample_value("lumen_wsgi_queue_depth") == 5.0

    mark_process_dead(pid_a, str(tmp_path))

    registry = _merged(tmp_path)
    assert registry.get_sample_value("lumen_wsgi_queue_depth") == 2.0
    assert registry.get_sample_value("lumen_wsgi_threads_busy") == 5.0
    assert registry.get_sample_value("lumen_wsgi_threads_total") == 10.0
    assert registry.get_sample_value("lumen_http_requests_total", {
        "method": "GET", "path_template": "<unmatched>", "status": "200",
    }) == 2.0, "the dead worker's counter is real history and must still be summed"


def test_pool_gauges_are_sampled_from_the_bridge_accessors(monkeypatch):
    """Single-process half of the same claim: the gauge tracks the accessor.

    The multiprocess merge above cannot run in this interpreter, so the two
    halves are tested separately — this one proves the value is read live from
    ``wsgi_disconnect`` on every request rather than captured once at import.
    """
    from prometheus_client import REGISTRY

    from lumen.blueprints.metrics import middleware as mw
    from lumen.services import wsgi_disconnect as wd

    monkeypatch.setattr(wd, "_queued", 4)
    monkeypatch.setattr(wd, "_running", 6)
    monkeypatch.setattr(wd, "_threads_total", 10)
    _drive(mw.make_metrics_middleware(_null_app), _fake_environ("/"))

    assert REGISTRY.get_sample_value("lumen_wsgi_queue_depth") == 4.0
    assert REGISTRY.get_sample_value("lumen_wsgi_threads_busy") == 6.0
    assert REGISTRY.get_sample_value("lumen_wsgi_threads_total") == 10.0

    monkeypatch.setattr(wd, "_queued", 0)
    _drive(mw.make_metrics_middleware(_null_app), _fake_environ("/"))
    assert REGISTRY.get_sample_value("lumen_wsgi_queue_depth") == 0.0


# ---------------------------------------------------------------------------
# observe_rejection / lumen_rejections_total
# ---------------------------------------------------------------------------

def _rejection_count(reason, source, model):
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(
        "lumen_rejections_total",
        {"reason": reason, "source": source, "model": model},
    ) or 0.0


def test_observe_rejection_labels_each_reason_separately():
    """The whole point is telling the refusals apart: 'too many requests' and
    'out of coins' are different failures with different remedies."""
    from lumen.blueprints.metrics.middleware import observe_rejection

    reasons = ("coin_budget", "no_access", "needs_consent", "no_healthy_endpoint")
    before = {r: _rejection_count(r, "api", "gpt-4o") for r in reasons}
    for reason in reasons:
        observe_rejection(reason, "api", "gpt-4o")
    observe_rejection("coin_budget", "api", "gpt-4o")

    assert _rejection_count("coin_budget", "api", "gpt-4o") == before["coin_budget"] + 2
    for reason in reasons[1:]:
        assert _rejection_count(reason, "api", "gpt-4o") == before[reason] + 1
    # A different source is a different series, not the same counter.
    assert _rejection_count("no_access", "chat", "gpt-4o") == 0.0


def test_observe_rejection_defaults_the_model_to_empty():
    """rate_limit and queue_shed happen before the body is parsed, so no model
    is known; the label is empty on purpose rather than invented."""
    from prometheus_client import REGISTRY, generate_latest

    from lumen.blueprints.metrics.middleware import observe_rejection

    before_rate = _rejection_count("rate_limit", "api", "")
    before_shed = _rejection_count("queue_shed", "api", "")
    observe_rejection("rate_limit", "api")          # model omitted entirely
    observe_rejection("queue_shed", "api", "")      # model explicitly empty

    assert _rejection_count("rate_limit", "api", "") == before_rate + 1
    assert _rejection_count("queue_shed", "api", "") == before_shed + 1
    scrape = generate_latest(REGISTRY).decode()
    assert "# TYPE lumen_rejections_total counter" in scrape
    assert 'lumen_rejections_total{model="",reason="rate_limit",source="api"}' in scrape


# ---------------------------------------------------------------------------
# observe_pool_wait / lumen_db_pool_wait_seconds
# ---------------------------------------------------------------------------

def test_observe_pool_wait_records_the_checkout_wait():
    """lumen_db_pool_connections shows the pool is full; only this shows whether
    anything is queued behind it."""
    import pytest
    from prometheus_client import REGISTRY

    from lumen.blueprints.metrics.middleware import observe_pool_wait

    def sample(suffix):
        return REGISTRY.get_sample_value(f"lumen_db_pool_wait_seconds_{suffix}") or 0.0

    before_count, before_sum = sample("count"), sample("sum")
    observe_pool_wait(0.25)
    observe_pool_wait(1.5)

    assert sample("count") - before_count == 2
    assert sample("sum") - before_sum == pytest.approx(1.75)
