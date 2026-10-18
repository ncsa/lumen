from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from monitor import migrate
from monitor.config import ConfigStore
from monitor.db import make_engine, make_session_factory
from monitor.models import DailyStat
from monitor.probe import LiveSnapshot, Snapshot
from monitor.totals import Span, plan_span, run_models, split_by_day

CONFIG = """
lumen: {url: "https://lumen.test"}
timezone: America/Chicago
"""
# Local midnight starting 2026-10-17 in Chicago (CDT, UTC-5)
MIDNIGHT = datetime(2026, 10, 17, 5, 0)
DAY = date(2026, 10, 17)
HOUR, DAY_COUNT, MONTH = 120, 2400, 72000


def model(model_id="qwen", hour=HOUR, day=DAY_COUNT, month=None, tokens=True):
    requests = {"last_minute": 2, "last_hour": hour, "last_day": day, "last_month": month}
    return {
        "id": model_id,
        "status": "ok",
        "requests": requests,
        "tokens": {key: None if value is None else value * 1000 for key, value in requests.items()} if tokens else None,
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'monitor.db'}"
    monkeypatch.setenv("MONITOR_DATABASE_URL", url)
    migrate.upgrade(url)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG)
    engines = []

    def factory():
        engine = make_engine(url)
        engines.append(engine)
        return make_session_factory(engine)

    yield ConfigStore(config_path), factory
    for engine in engines:
        engine.dispose()


def run(store, session_factory, at, models=None, snapshot=None, now=None):
    """A models run at `at` on a probe snapshot taken at `at`."""
    snapshot = snapshot or LiveSnapshot()
    snapshot.set(Snapshot(at, models if models is not None else [model()]))
    return run_models(store, session_factory, snapshot, now=now or at)


def rows(session_factory, model_id="qwen"):
    with session_factory() as session:
        stmt = select(DailyStat).where(DailyStat.model == model_id, DailyStat.source == "lumen").order_by(DailyStat.day)
        return {row.day: row for row in session.execute(stmt).scalars()}


def test_five_minute_runs_accumulate_last_hour(env):
    store, factory = env
    sf = factory()
    run(store, sf, MIDNIGHT)
    for i in range(1, 4):
        run(store, sf, MIDNIGHT + timedelta(minutes=5 * i))
    [row] = rows(sf).values()
    assert row.requests == pytest.approx(3 * HOUR * 5 / 60)
    assert row.tokens == pytest.approx(3 * HOUR * 1000 * 5 / 60)
    assert row.covered_until == MIDNIGHT + timedelta(minutes=15)
    assert not row.estimated and not row.final


def test_first_run_counts_from_local_midnight(env):
    store, factory = env
    sf = factory()
    run(store, sf, MIDNIGHT + timedelta(hours=12))
    row = rows(sf)[DAY]
    assert row.requests == pytest.approx(DAY_COUNT * 12 / 24)
    assert row.estimated


def test_five_hour_gap_is_estimated_from_last_day(env):
    store, factory = env
    sf = factory()
    run(store, sf, MIDNIGHT + timedelta(hours=1))
    first = rows(sf)[DAY].requests
    run(store, sf, MIDNIGHT + timedelta(hours=6))
    row = rows(sf)[DAY]
    assert row.requests - first == pytest.approx(DAY_COUNT * 5 / 24)
    assert row.estimated


def test_three_day_gap_fills_estimated_rows_from_last_month(env):
    store, factory = env
    sf = factory()
    noon = MIDNIGHT + timedelta(hours=12)
    run(store, sf, noon, [model(month=MONTH)])
    before = rows(sf)[DAY].requests
    run(store, sf, noon + timedelta(days=3), [model(month=MONTH)])
    got = rows(sf)
    per_day = MONTH / 30
    assert sorted(got) == [DAY + timedelta(days=i) for i in range(4)]
    assert got[DAY].requests - before == pytest.approx(per_day / 2)
    assert got[DAY + timedelta(days=1)].requests == pytest.approx(per_day)
    assert got[DAY + timedelta(days=2)].requests == pytest.approx(per_day)
    assert got[DAY + timedelta(days=3)].requests == pytest.approx(per_day / 2)
    assert all(row.estimated for row in got.values())
    assert [row.final for row in got.values()] == [True, True, True, False]
    assert got[DAY + timedelta(days=3)].covered_until == noon + timedelta(days=3)


def test_three_day_gap_without_last_month_goes_back_24h(env):
    store, factory = env
    sf = factory()
    noon = MIDNIGHT + timedelta(hours=12)
    run(store, sf, noon)
    before = rows(sf)[DAY].requests
    run(store, sf, noon + timedelta(days=3))
    got = rows(sf)
    assert sorted(got) == [DAY, DAY + timedelta(days=2), DAY + timedelta(days=3)]  # the day in between stays empty
    assert got[DAY].requests == before
    assert got[DAY + timedelta(days=2)].requests == pytest.approx(DAY_COUNT / 2)
    assert got[DAY + timedelta(days=3)].requests == pytest.approx(DAY_COUNT / 2)
    assert got[DAY + timedelta(days=2)].estimated and got[DAY + timedelta(days=3)].estimated


def test_gap_across_midnight_is_split_by_duration(env):
    store, factory = env
    sf = factory()
    late = MIDNIGHT + timedelta(hours=23, minutes=57)
    run(store, sf, MIDNIGHT)
    run(store, sf, late)
    yesterday = rows(sf)[DAY].requests
    run(store, sf, late + timedelta(minutes=5))
    got = rows(sf)
    assert got[DAY].requests - yesterday == pytest.approx(HOUR * 3 / 60)
    assert got[DAY + timedelta(days=1)].requests == pytest.approx(HOUR * 2 / 60)
    assert got[DAY].covered_until == MIDNIGHT + timedelta(days=1)


def test_first_run_after_midnight_finalizes_yesterday(env):
    store, factory = env
    sf = factory()
    run(store, sf, MIDNIGHT + timedelta(hours=23, minutes=55))
    with sf() as session:  # a model that left Lumen keeps a row; it is closed as well
        session.add(DailyStat(day=DAY, model="gone", source="lumen", requests=5))
        session.commit()
    assert not rows(sf)[DAY].final
    result = run(store, sf, MIDNIGHT + timedelta(days=1, minutes=1))
    assert result.finalized == 2
    assert rows(sf)[DAY].final and rows(sf, "gone")[DAY].final
    assert not rows(sf)[DAY + timedelta(days=1)].final


def test_final_rows_do_not_change(env):
    store, factory = env
    sf = factory()
    run(store, sf, MIDNIGHT + timedelta(hours=12))
    run(store, sf, MIDNIGHT + timedelta(days=1, hours=1))  # closes DAY
    closed = rows(sf)[DAY].requests
    with sf() as session:  # covered_until falls back before the closed day
        for row in session.execute(select(DailyStat)).scalars():
            row.covered_until = MIDNIGHT + timedelta(hours=12) if row.day == DAY else None
        session.commit()
    run(store, sf, MIDNIGHT + timedelta(days=1, hours=2))
    assert rows(sf)[DAY].requests == closed


def test_restart_does_not_double_count(env):
    store, factory = env
    at = MIDNIGHT + timedelta(minutes=30)
    run(store, factory(), MIDNIGHT)
    run(store, factory(), at)
    counted = rows(factory())[DAY].requests
    # A new process: fresh engine and an empty live snapshot, then its first probe.
    run(store, factory(), at)
    assert rows(factory())[DAY].requests == counted
    run(store, factory(), at + timedelta(minutes=5))
    assert rows(factory())[DAY].requests == pytest.approx(counted + HOUR * 5 / 60)


def test_restart_after_a_short_outage_uses_last_hour(env):
    store, factory = env
    run(store, factory(), MIDNIGHT)
    run(store, factory(), MIDNIGHT + timedelta(minutes=40))
    row = rows(factory())[DAY]
    assert row.requests == pytest.approx(HOUR * 40 / 60)
    assert not row.estimated


def test_stale_snapshot_is_skipped(env):
    store, factory = env
    sf = factory()
    result = run(store, sf, MIDNIGHT, now=MIDNIGHT + timedelta(minutes=2, seconds=1))  # 2 × refresh.web = 2m
    assert result.skipped == "stale snapshot"
    assert rows(sf) == {}
    assert run(store, sf, MIDNIGHT, now=MIDNIGHT + timedelta(minutes=2)).skipped is None


def test_no_snapshot_is_skipped(env):
    store, factory = env
    assert run_models(store, factory(), LiveSnapshot(), now=MIDNIGHT).skipped == "no snapshot"


def test_missing_windows_leave_totals_empty(env):
    store, factory = env
    sf = factory()
    run(store, sf, MIDNIGHT, [model(tokens=False)])
    run(store, sf, MIDNIGHT + timedelta(minutes=5), [model(tokens=False)])
    row = rows(sf)[DAY]
    assert row.tokens is None
    assert row.requests == pytest.approx(HOUR * 5 / 60)


def test_a_snapshot_without_windows_defers_its_gap(env):
    store, factory = env
    sf = factory()
    bare = {"id": "qwen", "status": "ok", "requests": None, "tokens": None}
    run(store, sf, MIDNIGHT + timedelta(minutes=5))
    run(store, sf, MIDNIGHT + timedelta(minutes=10), [bare])
    assert rows(sf)[DAY].covered_until == MIDNIGHT + timedelta(minutes=5)
    run(store, sf, MIDNIGHT + timedelta(minutes=15))
    row = rows(sf)[DAY]
    assert row.requests == pytest.approx(HOUR * 15 / 60)  # the 5 minutes without windows are not lost
    assert row.covered_until == MIDNIGHT + timedelta(minutes=15)


@pytest.mark.parametrize("gap, windows, expected", [
    (timedelta(minutes=30), {"last_day": 2400}, Span(MIDNIGHT, 2400 / 86400, True)),  # no last_hour
    (timedelta(hours=5), {"last_hour": 120, "last_month": 72000}, Span(MIDNIGHT, 72000 / (30 * 86400), True)),  # no last_day
    (timedelta(days=40), {"last_month": 72000}, Span(MIDNIGHT + timedelta(days=10), 72000 / (30 * 86400), True)),  # 30 days back
    (timedelta(hours=5), {"last_hour": 120}, None),
])
def test_plan_span_fallbacks(gap, windows, expected):
    assert plan_span(windows, MIDNIGHT, MIDNIGHT + gap) == expected


def test_totals_keep_the_other_columns(env):
    store, factory = env
    sf = factory()
    with sf() as session:
        session.add(DailyStat(day=DAY, model="qwen", source="lumen", input_price=0.2, polls=3, polls_ok=2, healthy_sum=3))
        session.commit()
    run(store, sf, MIDNIGHT + timedelta(minutes=5))
    row = rows(sf)[DAY]
    assert (row.input_price, row.polls, row.polls_ok, row.healthy_sum) == (0.2, 3, 2, 3)


def test_split_by_day():
    tz = ZoneInfo("America/Chicago")
    start = MIDNIGHT - timedelta(hours=1)
    pieces = split_by_day(start, MIDNIGHT + timedelta(days=1, hours=1), tz)
    assert [(day, b - a) for day, a, b in pieces] == [
        (DAY - timedelta(days=1), timedelta(hours=1)),
        (DAY, timedelta(days=1)),
        (DAY + timedelta(days=1), timedelta(hours=1)),
    ]
