"""The SQL Lumen card (`lumen_card`, `utc_slot`) against the Python path it replaces.

Each test runs on SQLite, and on PostgreSQL when `MONITOR_TEST_POSTGRES_URL` is set.
"""

import dataclasses
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from monitor import migrate
from monitor.buckets import (
    DOWN,
    EPOCH,
    OK,
    RANGES,
    Grid,
    bucket_statuses,
    lumen_daily_status,
    lumen_uptime,
    make_grid,
    probe_status,
    range_days,
    slot_start,
    utc_slot,
)
from monitor.config import ConfigStore
from monitor.daily import local_day, local_midnight
from monitor.db import make_engine, make_session_factory
from monitor.models import Probe
from monitor.overview import _iso, lumen_card
from monitor.probe import lumen_state

# Off the second, like utcnow(), and two days after Chicago leaves DST (2026-11-01 07:00 UTC).
NOW = datetime(2026, 11, 3, 17, 0, 12, 345678)
DST_END = datetime(2026, 11, 1, 7, 0)
MICROSECOND = timedelta(microseconds=1)
SECOND = timedelta(seconds=1)
SLOT = timedelta(minutes=15)
TIMEZONES = ["UTC", "America/Chicago", "Asia/Kolkata", "Asia/Kathmandu"]


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)])
def session_factory(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path / 'monitor.db'}"
    else:
        url = request.getfixturevalue("postgres_url")
    migrate.upgrade(url)
    engine = make_engine(url)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()
        if request.param == "postgresql":
            migrate.downgrade(url, "base")


@pytest.fixture
def make_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text('lumen: {url: "https://lumen.test"}\n')
    base = ConfigStore(path, env={}).current

    def make(tz="UTC", web=timedelta(minutes=1), down_above=base.down_above):
        return dataclasses.replace(base, timezone=ZoneInfo(tz), refresh=dataclasses.replace(base.refresh, web=web),
                                   down_above=down_above)

    return make


def add(session_factory, rows):
    with session_factory() as session:
        session.add_all(rows)
        session.commit()


def old_lumen_card(session, config, rng, now):
    """`lumen_card` as it was before the SQL bucketing: every probe into Python."""
    grid = make_grid(rng, config.refresh.web, now, config.timezone)
    probes = session.execute(
        select(Probe.polled_at, Probe.success, Probe.response_time_ms, Probe.model_count, Probe.models_up)
        .where(Probe.polled_at > now - rng.span, Probe.polled_at <= now)
        .order_by(Probe.polled_at, Probe.id)
    ).all()

    max_rt = [None] * grid.count
    polls, polls_up, polls_ok = [0] * grid.count, [0] * grid.count, [0] * grid.count
    points = []
    for p in probes:
        status = probe_status(p.success, p.model_count, p.models_up, config.down_above)
        points.append((p.polled_at, status))
        i = grid.index(p.polled_at)
        if i is None:
            continue
        polls[i] += 1
        polls_up[i] += status != DOWN
        polls_ok[i] += status == OK
        if p.response_time_ms is not None:
            max_rt[i] = max(max_rt[i] or 0.0, p.response_time_ms)
    if rng.daily:
        statuses = [lumen_daily_status(n, up, ok) for n, up, ok in zip(polls, polls_up, polls_ok)]
        uptime = 100 * sum(polls_up) / sum(polls) if sum(polls) else None
    else:
        statuses = bucket_statuses(points, grid)
        uptime = lumen_uptime(statuses)

    rts = [p.response_time_ms for p in probes if p.response_time_ms is not None]
    last = probes[-1] if probes else None
    return {
        "state": lumen_state(session, config.down_above),
        "last_ms": last.response_time_ms if last else None,
        "last_at": _iso(last.polled_at) if last else None,
        "min_ms": min(rts) if rts else None,
        "avg_ms": sum(rts) / len(rts) if rts else None,
        "max_ms": max(rts) if rts else None,
        "uptime": uptime,
        "buckets": [{"t": _iso(t), "status": s, "rt": rt} for t, s, rt in zip(grid.times(), statuses, max_rt)],
    }


def cards(session_factory, config, rng, now=NOW):
    """`(new, old)` cards; the average may differ in its last bits only (the database adds in another order)."""
    with session_factory() as session:
        new = lumen_card(session, config, rng, now)
        old = old_lumen_card(session, config, rng, now)
    assert new["avg_ms"] == pytest.approx(old["avg_ms"], rel=1e-12)
    return {**new, "avg_ms": None}, {**old, "avg_ms": None}


UPS = (None, 2, 3, 4, 4, 4)


def probe(at, rnd=None, ups=UPS, **values):
    """A probe at `at`; random success, response time and models up (one of `ups`, of 4) unless given."""
    if rnd is not None:
        success = rnd.random() > 0.05
        count = rnd.choice([None, 4, 4, 4])
        values = {
            "success": success,
            "response_time_ms": None if rnd.random() < 0.1 else rnd.uniform(0, 2000),
            "model_count": count if success else None,
            "models_up": rnd.choice(ups) if success and count else None,
            **values,
        }
    return Probe(polled_at=at, **{"success": True, "response_time_ms": 100.0, **values})


def edges(grid: Grid):
    """Every bucket edge, and one microsecond either side of it."""
    for k in range(grid.count + 1):
        edge = grid.start + k * grid.size
        yield from (edge - MICROSECOND, edge, edge + MICROSECOND)


def random_probes(rnd, start, now, extra=(), ups=UPS):
    """Random probes over `(start, now]`, plus `extra` times, one at `now` and some just outside the range."""
    span = (now - start) // MICROSECOND
    times = [start + rnd.randrange(1, span + 1) * MICROSECOND for _ in range(400)]
    times += list(extra) + [now, start, start - MICROSECOND, now + MICROSECOND]
    return [probe(t, rnd, ups) for t in times]


# --- utc_slot ------------------------------------------------------------------


def sql_slots(session_factory, times):
    add(session_factory, [probe(t) for t in times])
    with session_factory() as session:
        return dict(session.execute(select(Probe.polled_at, utc_slot(Probe.polled_at))).all())


def test_utc_slot_is_aligned_to_the_utc_quarter_hour(session_factory):
    edge = datetime(2026, 10, 17, 15, 15)
    times = [edge - SECOND, edge, edge - MICROSECOND, edge + SLOT - MICROSECOND,
             edge - SECOND + 999500 * MICROSECOND,  # SQLite before 3.53 would round this up to the edge
             datetime(2026, 10, 17, 18, 30), datetime(2026, 10, 17, 18, 15)]  # local midnight in Kolkata, Kathmandu
    found = sql_slots(session_factory, times)
    for t in times:
        assert found[t] == (t - EPOCH) // SLOT, t
        assert slot_start(found[t]) <= t < slot_start(found[t]) + SLOT
    assert found[edge - SECOND] != found[edge]  # 15:14:59 and 15:15:00
    assert found[edge - MICROSECOND] != found[edge]
    assert slot_start(found[edge]) == edge


def test_utc_slot_does_not_depend_on_now_or_the_range(session_factory, make_config):
    """The slot is a function of the time alone, so a probe's local day is the same whatever `now` is."""
    times = [datetime(2026, 10, 30, 4, 59, 59), datetime(2026, 10, 30, 5, 0)]  # either side of midnight in Chicago
    found = sql_slots(session_factory, times)
    assert [found[t] for t in times] == [(t - EPOCH) // SLOT for t in times]
    config = make_config("America/Chicago")
    for now in (NOW, NOW + timedelta(minutes=7, seconds=3, microseconds=11), NOW + timedelta(days=1, hours=5)):
        new, old = cards(session_factory, config, RANGES["1y"], now)
        assert new == old
        statuses = [b["status"] for b in new["buckets"]]
        assert statuses.count("ok") == 2, now  # the two probes are on two local days


# --- Short ranges ---------------------------------------------------------------------


@pytest.mark.parametrize("key", ["1h", "12h", "24h", "7d", "30d"])
@pytest.mark.parametrize("web", [timedelta(minutes=1), timedelta(minutes=7, seconds=13)])
def test_short_ranges_match_the_python_path(session_factory, make_config, key, web):
    config = make_config(web=web)
    rng = RANGES[key]
    grid = make_grid(rng, web, NOW, None)
    start = NOW - rng.span
    rnd = random.Random(f"{key}-{web}")
    add(session_factory, random_probes(rnd, start, NOW, [t for t in edges(grid) if start < t <= NOW and rnd.random() < 0.3]))
    new, old = cards(session_factory, config, rng)
    assert new == old
    assert len({b["status"] for b in new["buckets"]} - {"unknown"}) >= 2


@pytest.mark.parametrize("key", ["1h", "30d", "1y"])
@pytest.mark.parametrize("down_above", [0, 30, 50, 75, 100])
def test_down_above_matches_the_python_path(session_factory, make_config, key, down_above):
    """`lumen.down_above` decides when a successful probe with models down counts as down, in SQL as in Python."""
    config = make_config(down_above=down_above)
    rng = RANGES[key]
    start = NOW - min(rng.span, timedelta(days=31))
    rnd = random.Random(f"{key}-{down_above}")
    add(session_factory, random_probes(rnd, start, NOW, ups=(None, 0, 1, 2, 3, 4)))
    new, old = cards(session_factory, config, rng)
    assert new == old


@pytest.mark.parametrize("key", ["1h", "7d", "1y"])
def test_empty_card_matches_the_python_path(session_factory, make_config, key):
    new, old = cards(session_factory, make_config(), RANGES[key])
    assert new == old
    assert new["last_ms"] is None and new["min_ms"] is None and new["uptime"] is None


# --- Tied probes -------------------------------------------------------------------


@pytest.mark.parametrize("key", ["1h", "1y"])
@pytest.mark.parametrize("order", ["slow first", "fast first"])
def test_tied_probes_take_the_last_one_by_id(session_factory, make_config, key, order):
    """Two probes at the same second: the card's last probe is the one with the higher id, as in `lumen_state`."""
    at = NOW - timedelta(minutes=5)
    slow = probe(at, response_time_ms=900.0, model_count=4, models_up=4)
    fast = probe(at, response_time_ms=40.0, model_count=4, models_up=3)
    add(session_factory, [probe(at - timedelta(minutes=1)), *((slow, fast) if order == "slow first" else (fast, slow))])
    second_ms = 40.0 if order == "slow first" else 900.0
    new, old = cards(session_factory, make_config(), RANGES[key])
    assert new == old
    assert new["last_ms"] == second_ms and new["last_at"] == _iso(at)
    # lumen_state reads the same probe: degraded exactly when the later one had a model down.
    assert new["state"] == ("DEGRADED" if second_ms == 40.0 else "UP")


# --- 1y ------------------------------------------------------------------------------


def boundary_times(tz, now):
    """One second and one microsecond either side of each local midnight in the last 10 days and of the DST change."""
    today = local_day(now, tz)
    edges_ = [local_midnight(today - timedelta(days=k), tz) for k in range(10)] + [DST_END]
    return [e + d for e in edges_ for d in (-SECOND, -MICROSECOND, timedelta(0), MICROSECOND, SECOND) if e + d <= now]


@pytest.mark.parametrize("tz", TIMEZONES)
def test_1y_matches_the_python_path(session_factory, make_config, tz):
    config = make_config(tz)
    rnd = random.Random(tz)
    retention = NOW - timedelta(days=31)
    add(session_factory, random_probes(rnd, retention, NOW, boundary_times(config.timezone, NOW)))
    new, old = cards(session_factory, config, RANGES["1y"])
    assert new == old


@pytest.mark.parametrize("tz", TIMEZONES)
def test_1y_probes_around_local_midnight_land_on_their_local_day(session_factory, make_config, tz):
    """Each probe just before midnight carries the day's highest response time, so its day's `rt` shows where it landed."""
    config = make_config(tz)
    zone = config.timezone
    today = local_day(NOW, zone)
    rows, expected = [], {}
    for k in range(10):
        midnight = local_midnight(today - timedelta(days=k), zone)
        before, after = 1000.0 + k, 10.0 + k
        rows += [probe(midnight - SECOND, response_time_ms=before), probe(midnight + SECOND, response_time_ms=after)]
        expected[local_day(midnight - SECOND, zone)] = before
        expected.setdefault(local_day(midnight + SECOND, zone), after)
    for d in (-SECOND, SECOND):  # both on Nov 1 in Chicago, where the clock goes back at 07:00 UTC
        rows.append(probe(DST_END + d, response_time_ms=5000.0 + d.total_seconds()))
        day = local_day(DST_END + d, zone)
        expected[day] = max(expected.get(day, 0.0), 5000.0 + d.total_seconds())
    add(session_factory, rows)
    new, old = cards(session_factory, config, RANGES["1y"])
    assert new == old
    found = {day: b["rt"] for day, b in zip(range_days(RANGES["1y"], NOW, zone), new["buckets"]) if b["rt"] is not None}
    assert found == expected
