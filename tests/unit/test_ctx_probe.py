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
