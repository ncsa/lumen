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

def _fake_environ(path="/", method="GET"):
    return {"PATH_INFO": path, "REQUEST_METHOD": method}


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


def test_middleware_normalizes_path_label(monkeypatch):
    """Numeric path segments are collapsed before being recorded as a label."""
    from lumen.blueprints.metrics import middleware as mw

    recorded = []
    orig = mw._http_requests.labels

    def spy(**kwargs):
        recorded.append(kwargs.get("path_template"))
        return orig(**kwargs)

    monkeypatch.setattr(mw._http_requests, "labels", spy)

    def fake_app(environ, start_response):
        start_response("200 OK", [])
        return []

    mw.make_metrics_middleware(fake_app)(
        _fake_environ("/admin/groups/123"), lambda *a: None
    )
    assert recorded and recorded[-1] == "/admin/groups/{id}"


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
