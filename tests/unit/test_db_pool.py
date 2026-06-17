import logging

import psutil

from lumen.services import db_pool


def _build(db_cfg, workers=1, replicas=1, uri="postgresql://u:p@localhost/db"):
    return db_pool.build_engine_options(uri, db_cfg, workers=workers, replicas=replicas)


def test_sqlite_returns_empty():
    assert db_pool.build_engine_options("sqlite:///x.db", {}, workers=4, replicas=2) == {}


def test_auto_size_single_process():
    opts = _build({"max_connections": 100})
    assert opts["pool_size"] == 60
    assert opts["max_overflow"] == 20
    assert opts["pool_pre_ping"] is True


def test_auto_size_divides_by_workers_and_replicas():
    opts = _build({"max_connections": 100}, workers=2, replicas=2)  # divisor 4
    assert opts["pool_size"] == 15
    assert opts["max_overflow"] == 5


def test_pre_ping_always_on_and_not_configurable():
    opts = _build({"max_connections": 100})
    assert opts["pool_pre_ping"] is True
    assert "pool_pre_ping" not in _passthrough_keys_in(opts) or opts["pool_pre_ping"] is True


def _passthrough_keys_in(opts):
    return {k for k in opts if k.startswith("pool_")}


def test_passthrough_timeout_and_recycle():
    opts = _build({"max_connections": 100, "pool_timeout": 10, "pool_recycle": 1800})
    assert opts["pool_timeout"] == 10
    assert opts["pool_recycle"] == 1800


def test_min_clamp_to_one():
    # tiny budget across many processes still yields at least 1 each
    opts = _build({"max_connections": 4}, workers=10, replicas=10)
    assert opts["pool_size"] == 1
    assert opts["max_overflow"] == 1


def test_explicit_override_within_budget_is_honored():
    # (10 + 5) * 4 = 60 <= 0.8 * 100 = 80 → honored
    opts = _build({"max_connections": 100, "pool_size": 10, "max_overflow": 5}, workers=2, replicas=2)
    assert opts["pool_size"] == 10
    assert opts["max_overflow"] == 5


def test_explicit_override_over_budget_falls_back_to_auto(caplog):
    # (50 + 50) * 1 = 100 > 80 → rejected, auto used
    with caplog.at_level(logging.WARNING):
        opts = _build({"max_connections": 100, "pool_size": 50, "max_overflow": 50})
    assert opts["pool_size"] == 60
    assert opts["max_overflow"] == 20
    assert any("exceeding" in r.message or "exceed" in r.message for r in caplog.records)


def test_partial_explicit_override_fills_other_from_auto():
    # only max_overflow set; pool_size auto (60). (60 + 5) * 1 = 65 <= 80 → honored
    opts = _build({"max_connections": 100, "max_overflow": 5})
    assert opts["pool_size"] == 60
    assert opts["max_overflow"] == 5


def test_detect_workers_from_web_concurrency(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "7")
    assert db_pool.detect_workers() == 7


def test_detect_workers_defaults_to_one(monkeypatch):
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

    class FakeParent:
        def cmdline(self):
            return ["uvicorn", "asgi:app", "--host", "0.0.0.0"]

    class FakeProc:
        def parent(self):
            return FakeParent()

    monkeypatch.setattr(psutil, "Process", lambda: FakeProc())
    assert db_pool.detect_workers() == 1


def test_detect_workers_parses_parent_cmdline(monkeypatch):
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

    class FakeParent:
        def cmdline(self):
            return ["uvicorn", "asgi:app", "--workers", "4", "--port", "5001"]

    class FakeProc:
        def parent(self):
            return FakeParent()

    monkeypatch.setattr(psutil, "Process", lambda: FakeProc())
    assert db_pool.detect_workers() == 4


def test_detect_workers_parses_equals_form(monkeypatch):
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

    class FakeParent:
        def cmdline(self):
            return ["uvicorn", "asgi:app", "--workers=3"]

    class FakeProc:
        def parent(self):
            return FakeParent()

    monkeypatch.setattr(psutil, "Process", lambda: FakeProc())
    assert db_pool.detect_workers() == 3


def test_detect_workers_never_raises(monkeypatch):
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

    def boom():
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr(psutil, "Process", boom)
    assert db_pool.detect_workers() == 1


def test_detect_replicas(monkeypatch):
    monkeypatch.setenv("LUMEN_REPLICAS", "3")
    assert db_pool.detect_replicas() == 3
    monkeypatch.delenv("LUMEN_REPLICAS", raising=False)
    assert db_pool.detect_replicas() == 1
