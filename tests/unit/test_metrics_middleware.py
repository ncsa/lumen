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


def test_middleware_records_500_on_app_exception():
    """If the wrapped app raises, status defaults to '500' and the exception propagates."""
    import pytest
    from lumen.blueprints.metrics.middleware import make_metrics_middleware

    def exploding_app(environ, start_response):
        raise RuntimeError("boom")

    wrapped = make_metrics_middleware(exploding_app)
    with pytest.raises(RuntimeError, match="boom"):
        wrapped(_fake_environ("/crash"), lambda *a: None)
