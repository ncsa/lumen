"""backfill_tokens.backfill_history against a stub Lumen database and a SQLite monitor database."""

import importlib.util
from collections import namedtuple
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select

from monitor import migrate
from monitor.config import parse_config
from monitor.db import make_engine, make_session_factory
from monitor.hardware import daily_cost
from monitor.models import DailyStat

_spec = importlib.util.spec_from_file_location("backfill_tokens", Path(__file__).parent.parent / "backfill_tokens.py")
backfill_tokens = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill_tokens)

Day = namedtuple("Day", "model_name day requests input_tokens output_tokens ii io oo ci co")
Model = namedtuple("Model", "model_name input_cost_per_million output_cost_per_million endpoints")

CONFIG = parse_config({
    "timezone": "UTC",
    "price": {"kwh": 0.24, "rtx6000": {"energy": 600, "price": 625}},
    "models": {"m1": {"gpu": "rtx6000", "gpus": 1}},
}, env={})
MODELS = [Model("m1", 3.0, 4.0, 2)]
D1, D2 = date(2026, 1, 1), date(2026, 1, 2)
# Two text requests on D1 billed at $1/M input and $2/M output:
# (1000 in, 100 out) for $0.0012 and (100 in, 1000 out) for $0.0021.
FITTED = Day("m1", D1, 2, 1100, 1100, 1_010_000.0, 200_000.0, 1_010_000.0, 1.41, 2.22)
# D2 has only audio traffic: nothing to fit, so today's prices are used.
UNFITTED = Day("m1", D2, 5, 50, 60, None, None, None, None, None)


class StubLumen:
    """Stands in for the Lumen engine: returns fixed rows per query, no PostgreSQL."""

    def __init__(self, days, models):
        self.rows = {backfill_tokens.HISTORY_SQL: days, backfill_tokens.MODELS_SQL: models}

    @contextmanager
    def connect(self):
        yield self

    def execute(self, sql, params=None):
        rows = self.rows[sql]
        return namedtuple("Result", "all")(lambda: rows)


@pytest.fixture
def monitor_db(tmp_path):
    url = f"sqlite:///{tmp_path / 'monitor.db'}"
    migrate.upgrade(url)
    engine = make_engine(url)
    yield engine
    engine.dispose()


def run(engine, days, rows=()):
    """Run backfill_history on `days` over a monitor holding `rows`; returns its result and the daily_stats SELECTs."""
    selects = []

    def count(_conn, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT") and "FROM daily_stats" in statement:
            selects.append(statement)

    with make_session_factory(engine)() as session:
        session.add_all(rows)
        session.commit()
        event.listen(engine, "before_cursor_execute", count)
        try:
            result = backfill_tokens.backfill_history(StubLumen(days, MODELS), session, CONFIG)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        session.commit()
    return result, len(selects)


def stats(engine):
    with make_session_factory(engine)() as session:
        return {(r.model, r.day, r.source): r for r in session.execute(select(DailyStat)).scalars()}


def test_fills_estimated_and_missing_rows_and_missing_hardware(monitor_db):
    existing = [
        DailyStat(day=D1, model="m1", source="lumen", requests=1, input_tokens=10, output_tokens=10,
                  input_price=9, output_price=9, estimated=True, final=False),
        DailyStat(day=D1, model="m1", source="hardware", daily_cost=1.0, avg_healthy=1.0),
    ]
    assert run(monitor_db, [FITTED, UNFITTED], existing)[0] == (2, 1, 1)

    rows = stats(monitor_db)
    assert set(rows) == {("m1", D1, "lumen"), ("m1", D1, "hardware"), ("m1", D2, "lumen"), ("m1", D2, "hardware")}
    d1 = rows["m1", D1, "lumen"]
    assert (d1.requests, d1.input_tokens, d1.output_tokens) == (2, 1100, 1100)
    assert (d1.input_price, d1.output_price) == (pytest.approx(1.0), pytest.approx(2.0))
    assert (d1.estimated, d1.final, d1.configured) == (False, True, 2)
    assert d1.covered_until == datetime(2026, 1, 2)
    d2 = rows["m1", D2, "lumen"]
    assert (d2.requests, d2.input_tokens, d2.output_tokens) == (5, 50, 60)
    assert (d2.input_price, d2.output_price) == (3.0, 4.0)
    assert (d2.estimated, d2.final, d2.configured) == (False, True, 2)
    assert d2.covered_until == datetime(2026, 1, 3)
    # No live watermark for m1, so the backfill sets none.
    assert d2.input_tokens_covered_until is None and d2.output_tokens_covered_until is None
    # The existing hardware row is kept; the missing one is created for the active endpoints.
    assert rows["m1", D1, "hardware"].daily_cost == 1.0
    hw = rows["m1", D2, "hardware"]
    assert (hw.gpu, hw.gpus, hw.kwh, hw.avg_healthy) == ("rtx6000", 1, 0.24, 2.0)
    assert hw.daily_cost == pytest.approx(daily_cost(CONFIG.gpus["rtx6000"], 1, 0.24, 2))


def test_final_lumen_row_is_kept(monitor_db):
    final = DailyStat(day=D1, model="m1", source="lumen", requests=7, estimated=False, final=True)
    assert run(monitor_db, [FITTED], [final])[0] == (0, 1, 1)
    assert stats(monitor_db)["m1", D1, "lumen"].requests == 7


def test_repeated_key_is_added_once(monitor_db):
    assert run(monitor_db, [UNFITTED, UNFITTED])[0] == (1, 1, 0)
    assert len(stats(monitor_db)) == 2


def test_daily_stats_are_read_once_whatever_the_number_of_days(tmp_path):
    counts = []
    for n in (1, 10):
        url = f"sqlite:///{tmp_path / f'monitor-{n}.db'}"
        migrate.upgrade(url)
        engine = make_engine(url)
        days = [UNFITTED._replace(day=D2 + timedelta(days=i)) for i in range(n)]
        (created, hw_created, _), selects = run(engine, days)
        engine.dispose()
        assert (created, hw_created) == (n, n)
        counts.append(selects)
    assert counts[0] == counts[1]
