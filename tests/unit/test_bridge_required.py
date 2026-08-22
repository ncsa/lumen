"""The no-ASGI-bridge guard in create_app's `_record_queue_wait` hook.

Serving the bare Flask app instead of `asgi:app` leaves every bridge-derived timing
column NULL and disables disconnect detection, and until this guard existed it did
so in complete silence -- a 500-user load test produced 2,753 rows with NULL
queue_wait and nothing in the log to explain why. See tests/unit/test_asgi_entrypoint.py
for the static half of the same defence.
"""
import logging

import pytest

import lumen


@pytest.fixture(autouse=True)
def _reset_warn_once():
    """The flag is module-global and latches; each test needs a clean slate."""
    lumen._warned_no_bridge = False
    yield
    lumen._warned_no_bridge = False


def test_testing_app_stays_silent(client, caplog):
    """The Flask test client legitimately has no bridge and must not be nagged."""
    with caplog.at_level(logging.ERROR, logger="lumen"):
        client.get("/")
    assert "without the ASGI bridge" not in caplog.text


def test_dev_server_stays_silent(app, caplog):
    """The Werkzeug dev server identifies itself and is also a legitimate no-bridge path."""
    app.config["TESTING"] = False
    try:
        with caplog.at_level(logging.ERROR, logger="lumen"):
            app.test_client().get("/", environ_overrides={"SERVER_SOFTWARE": "Werkzeug/3.0.1"})
        assert "without the ASGI bridge" not in caplog.text
    finally:
        app.config["TESTING"] = True


def test_unbridged_real_server_logs_once(app, caplog):
    """A real server with no T0 mark is a misconfiguration: say so, once per process."""
    app.config["TESTING"] = False
    try:
        with caplog.at_level(logging.ERROR, logger="lumen"):
            for _ in range(3):
                app.test_client().get("/", environ_overrides={"SERVER_SOFTWARE": "uvicorn"})
        assert caplog.text.count("without the ASGI bridge") == 1, (
            "the warning must be once-per-process, not once-per-request: under load "
            "the per-request form would flood the log"
        )
        assert "asgi:app" in caplog.text, "the message must name the fix"
    finally:
        app.config["TESTING"] = True


def test_require_bridge_rejects_the_request(app, monkeypatch):
    """LUMEN_REQUIRE_BRIDGE turns an unmeasurable request into a hard failure."""
    monkeypatch.setenv("LUMEN_REQUIRE_BRIDGE", "1")
    app.config["TESTING"] = False
    try:
        resp = app.test_client().get("/", environ_overrides={"SERVER_SOFTWARE": "uvicorn"})
        assert resp.status_code == 500
        assert b"ASGI bridge" in resp.data
    finally:
        app.config["TESTING"] = True


@pytest.mark.parametrize("value", ["", "0", "false"])
def test_require_bridge_off_values_do_not_reject(app, monkeypatch, value):
    monkeypatch.setenv("LUMEN_REQUIRE_BRIDGE", value)
    app.config["TESTING"] = False
    try:
        resp = app.test_client().get("/", environ_overrides={"SERVER_SOFTWARE": "uvicorn"})
        assert resp.status_code != 500
    finally:
        app.config["TESTING"] = True


def test_bridge_marks_populate_queue_wait(app):
    """With T0 present the hook computes queue_wait, and never warns."""
    import time

    from flask import request

    app.config["TESTING"] = False
    try:
        with app.test_request_context(
            "/",
            environ_overrides={
                "SERVER_SOFTWARE": "uvicorn",
                "lumen.t0_monotonic": time.monotonic() - 0.25,
            },
        ):
            # Runs the registered before_request hooks, _record_queue_wait among
            # them, without needing a route added after the app served a request.
            assert app.preprocess_request() is None
            queue_wait = request.environ.get("lumen.queue_wait")
    finally:
        app.config["TESTING"] = True

    assert queue_wait is not None
    assert queue_wait >= 0.25
    assert lumen._warned_no_bridge is False
