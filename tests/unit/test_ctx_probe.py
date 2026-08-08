"""Tests for the app-context push/pop imbalance probe."""
import logging

from flask.ctx import AppContext

from lumen.services import ctx_probe
from lumen.services.ctx_probe import format_context_anomalies, install_ctx_probe


def test_probe_is_installed_and_idempotent(app):
    # create_app already installed it; a second install must not re-wrap.
    install_ctx_probe()
    push_before = AppContext.push
    install_ctx_probe()
    assert AppContext.push is push_before
    assert getattr(AppContext.push, "_lumen_ctx_probe", False)


def test_balanced_push_pop_is_silent(app, caplog):
    with caplog.at_level(logging.WARNING, logger="lumen.services.ctx_probe"):
        ctx = app.app_context()
        ctx.push()
        ctx.pop()
    assert caplog.records == []


def test_double_push_warns_with_stack(app, caplog):
    """Pushing the same context twice is the mechanism that makes the inner pop
    skip teardown; the warning must carry the stack that did it."""
    ctx_probe._anomalies.clear()
    ctx = app.app_context()
    ctx.push()
    try:
        with caplog.at_level(logging.WARNING, logger="lumen.services.ctx_probe"):
            ctx.push()
            assert len(caplog.records) == 1
            msg = caplog.records[0].getMessage()
            assert "pushed again" in msg
            assert "test_double_push_warns_with_stack" in msg

            # The inner pop skips teardown — that is warned about too.
            ctx.pop()
            assert len(caplog.records) == 2
            assert "teardown skipped" in caplog.records[1].getMessage()
    finally:
        ctx.pop()  # outer pop, balanced — no further warning
    assert len(caplog.records) == 2

    # Both anomalies are kept for /metrics/debug, keyed to the context's id so
    # they can be matched to a teardown=NEVER scope key.
    report = format_context_anomalies()
    assert f"double-push  ctx=0x{id(ctx):x}  depth=2" in report
    assert f"skipped-teardown-pop  ctx=0x{id(ctx):x}  depth=2" in report
    assert "test_double_push_warns_with_stack" in report


def test_anomaly_report_empty():
    ctx_probe._anomalies.clear()
    assert format_context_anomalies().strip() == "(none)"


def test_push_provenance_is_recorded(app):
    ctx = app.app_context()
    assert "push not recorded" in ctx_probe.describe_push(ctx)
    ctx.push()
    try:
        described = ctx_probe.describe_push(ctx)
        assert "on thread" in described
        assert "test_push_provenance_is_recorded" in described
    finally:
        ctx.pop()


def test_cross_thread_pop_is_recorded(app):
    """A context popped on a different thread than it was pushed on consumes
    the token without the reset taking effect in the pushing thread — that
    thread stays poisoned with the context current at depth 0."""
    import threading

    ctx_probe._anomalies.clear()
    ctx = app.app_context()
    ctx.push()
    try:
        def pop_elsewhere():
            try:
                ctx.pop()
            except Exception:
                pass  # expected: the pop cannot complete outside the push thread

        t = threading.Thread(target=pop_elsewhere, name="other-thread")
        t.start()
        t.join()
        report = format_context_anomalies()
        assert f"cross-thread-pop  ctx=0x{id(ctx):x}" in report
        assert "popped on other-thread" in report
        assert "test_cross_thread_pop_is_recorded" in report  # push stack included
    finally:
        # The cross-thread pop consumed the token; drain any leftover state so
        # this test does not poison the suite's main thread.
        if ctx._cv_tokens:
            ctx.pop()
        else:
            from flask.globals import _cv_app
            if _cv_app.get(None) is ctx:
                _cv_app.set(None)


def test_pop_raising_is_recorded(app):
    """A pop that raises leaves the app context current with teardown never
    run — the silent variant of the leak. The exception must be recorded."""
    import pytest

    ctx_probe._anomalies.clear()
    ctx = app.app_context()
    # Popping a context that was never pushed makes the original pop raise.
    with pytest.raises(Exception):
        ctx.pop()
    report = format_context_anomalies()
    assert f"app-ctx-pop-raised  ctx=0x{id(ctx):x}" in report
