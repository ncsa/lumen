from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from monitor import migrate
from monitor.buckets import RANGES
from monitor.config import ConfigStore
from monitor.db import make_engine, make_session_factory
from monitor.models import DailyStat, Probe, Sample
from monitor.overview import (
    counted_fraction,
    current_models,
    hardware_unit_cost,
    lumen_card,
    model_rows,
    overview,
    sort_rows,
    status_cards,
)
from monitor.probe import LiveSnapshot, Snapshot

CONFIG = """
lumen: {url: "https://lumen.test"}
timezone: America/Chicago
price:
  kwh: 0.24
  rtx6000: {energy: 600, price: 625}
models:
  qwen: {gpu: rtx6000, gpus: 1}
"""
NOW = datetime(2026, 10, 17, 17, 0)  # 12:00 in Chicago (CDT, UTC-5)
TODAY = date(2026, 10, 17)
MIDNIGHT = datetime(2026, 10, 17, 5, 0)  # local midnight starting TODAY, as UTC


def snap_model(model_id="qwen", **extra):
    base = {
        "id": model_id, "description": f"{model_id} description", "status": "ok", "configured": 2, "healthy": 1,
        "input_price": 0.2, "output_price": 0.8,
        "requests": {"last_minute": 3, "last_hour": 100, "last_day": 2000, "last_month": None},
        "tokens": {"last_minute": 3000, "last_hour": 100000, "last_day": 2000000, "last_month": None},
    }
    return {**base, **extra}


@pytest.fixture
def env(tmp_path):
    url = f"sqlite:///{tmp_path / 'monitor.db'}"
    migrate.upgrade(url)
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    engine = make_engine(url)
    yield ConfigStore(path, env={}), make_session_factory(engine)
    engine.dispose()


def add(session_factory, *rows):
    with session_factory() as session:
        session.add_all(rows)
        session.commit()


def probe(minutes_ago, success=True, rt=100.0, count=2, up=2):
    return Probe(polled_at=NOW - timedelta(minutes=minutes_ago), success=success, response_time_ms=rt,
                 model_count=count if success else None, models_up=up if success else None)


def sample(model, minutes_ago, status="ok", rpm=1):
    return Sample(model=model, sampled_at=NOW - timedelta(minutes=minutes_ago), status=status, configured=2, healthy=1, requests_last_minute=rpm)


def split_tokens(values):
    """Stores a combined `tokens` / `tokens_covered_until` as 3/4 input and 1/4 output tokens."""
    if "tokens" in values:
        tokens = values.pop("tokens")
        values["input_tokens"] = None if tokens is None else tokens * 3 / 4
        values["output_tokens"] = None if tokens is None else tokens / 4
    if "tokens_covered_until" in values:
        values["input_tokens_covered_until"] = values["output_tokens_covered_until"] = values.pop("tokens_covered_until")
    return values


def lumen_row(day, model="qwen", **values):
    return DailyStat(day=day, model=model, source="lumen", **split_tokens(values))


# --- Lumen card ------------------------------------------------------------------


def test_lumen_card_response_time_stats_and_strip(env):
    store, sf = env
    add(sf, probe(30, rt=200), probe(20, rt=50, up=1), probe(10, success=False, rt=15000), probe(0.5, rt=120),
        probe(120, rt=9999))  # outside 1h
    with sf() as session:
        card = lumen_card(session, store.current, RANGES["1h"], NOW)
    assert card["state"] == "UP"
    assert card["last_ms"] == 120 and card["last_at"] == "2026-10-17T16:59:30Z"
    assert card["min_ms"] == 50 and card["max_ms"] == 15000
    assert card["avg_ms"] == pytest.approx((200 + 50 + 15000 + 120) / 4)
    statuses = [b["status"] for b in card["buckets"]]
    assert len(statuses) == 60
    # A probe exactly 30 minutes ago starts bucket 30 of the 60.
    assert statuses[30] == "ok" and statuses[40] == "degraded" and statuses[50] == "down" and statuses[59] == "ok"
    assert statuses.count("unknown") == 56
    # Degraded counts as up: 3 of the 4 buckets with data.
    assert card["uptime"] == 75
    assert card["buckets"][50]["rt"] == 15000 and card["buckets"][0]["rt"] is None


def test_lumen_card_bucket_takes_the_worst_probe_and_max_response_time(env):
    store, sf = env
    add(sf, probe(4.5, rt=80), probe(4.2, success=False, rt=300))
    with sf() as session:
        card = lumen_card(session, store.current, RANGES["24h"], NOW)
    last = card["buckets"][-1]
    assert (last["status"], last["rt"]) == ("down", 300)
    assert card["uptime"] == 0
    assert card["state"] == "UP"  # 1 failure of 2 probes is not DOWN


def test_lumen_card_empty(env):
    store, sf = env
    with sf() as session:
        card = lumen_card(session, store.current, RANGES["7d"], NOW)
    assert card["state"] == "UNKNOWN"
    assert card["uptime"] is None and card["last_ms"] is None and card["avg_ms"] is None
    assert {b["status"] for b in card["buckets"]} == {"unknown"}


def test_lumen_card_1y_uses_daily_availability(env):
    store, sf = env
    two_days_ago = [probe(60 * 37 + i) for i in range(99)] + [probe(60 * 37 + 99, success=False)]  # 99% ok
    yesterday = [probe(60 * 13 + i, rt=10) for i in range(3)] + [probe(60 * 13 + 3, success=False)]  # 23:00 local, 75%
    add(sf, *two_days_ago, *yesterday, probe(60, success=False), probe(59, success=False))
    with sf() as session:
        card = lumen_card(session, store.current, RANGES["1y"], NOW)
    assert len(card["buckets"]) == 365
    assert [b["status"] for b in card["buckets"][-4:]] == ["unknown", "ok", "degraded", "down"]
    assert card["buckets"][-1]["t"] == "2026-10-17T05:00:00Z"
    assert card["uptime"] == pytest.approx(100 * 102 / 106)


def test_lumen_card_bucket_with_most_models_down_is_down(env):
    store, sf = env
    add(sf, probe(30, count=3, up=1), probe(20, count=3, up=2), probe(10))
    with sf() as session:
        card = lumen_card(session, store.current, RANGES["1h"], NOW)
    statuses = [b["status"] for b in card["buckets"]]
    assert (statuses[30], statuses[40], statuses[50]) == ("down", "degraded", "ok")
    assert card["uptime"] == pytest.approx(200 / 3)
    assert card["state"] == "UP"


def test_lumen_card_follows_down_above_but_model_strips_do_not(env, tmp_path):
    store, sf = env
    add(sf, probe(30, count=2, up=1), sample("qwen", 30, "degraded"), sample("qwen", 20, "down"), sample("qwen", 10))
    for down_above in (50, 0):
        (tmp_path / "config.yaml").write_text(CONFIG.replace('lumen: {url: "https://lumen.test"}', f'lumen: {{url: "https://lumen.test", down_above: {down_above}}}'))
        assert store.reload()
        with sf() as session:
            card = lumen_card(session, store.current, RANGES["1h"], NOW)
            (row,) = model_rows(session, store.current, RANGES["1h"], NOW, [snap_model()])
        assert card["buckets"][30]["status"] == ("degraded" if down_above == 50 else "down")
        assert (row["strip"][30], row["strip"][40], row["strip"][50]) == ("degraded", "down", "ok")


def test_lumen_card_1y_day_with_only_degraded_probes(env):
    store, sf = env
    add(sf, *[probe(60 * 13 + i, up=1) for i in range(5)])  # yesterday, 1 of 2 models down
    with sf() as session:
        card = lumen_card(session, store.current, RANGES["1y"], NOW)
    assert card["buckets"][-2]["status"] == "degraded"
    assert card["uptime"] == 100


def test_lumen_card_1y_day_with_most_models_down_is_down(env):
    store, sf = env
    add(sf, *[probe(60 * 13 + i, count=3, up=1) for i in range(5)], probe(60 * 13 + 5, success=False))
    with sf() as session:
        card = lumen_card(session, store.current, RANGES["1y"], NOW)
    assert card["buckets"][-2]["status"] == "down"
    assert card["uptime"] == 0


# --- Model table -------------------------------------------------------------------


def test_short_range_strip_uptime_and_peak(env):
    store, sf = env
    add(sf, sample("qwen", 50, rpm=4), sample("qwen", 50.5, "down", rpm=9), sample("qwen", 30, rpm=2),
        sample("qwen", 10, "degraded", rpm=None), sample("qwen", 90, rpm=99))
    with sf() as session:
        (row,) = model_rows(session, store.current, RANGES["1h"], NOW, [snap_model()])
    assert len(row["strip"]) == 60
    assert row["strip"][9] == "down" and row["strip"][10] == "ok" and row["strip"][30] == "ok" and row["strip"][50] == "degraded"
    assert row["uptime"] == 50
    assert row["peak_rpm"] == 9  # the sample from 90 minutes ago is outside 1h


def test_1y_strip_uptime_and_peak_from_daily_rows(env):
    store, sf = env
    yesterday, before = TODAY - timedelta(days=1), TODAY - timedelta(days=2)
    add(sf,
        lumen_row(before, polls=1440, polls_ok=1440, requests=1440.0, final=True),
        lumen_row(yesterday, polls=1440, polls_ok=720, requests=2880.0, final=True),
        # today: counted up to 06:00 local, 360 minutes
        lumen_row(TODAY, polls=100, polls_ok=0, requests=720.0, covered_until=MIDNIGHT + timedelta(hours=6)))
    with sf() as session:
        (row,) = model_rows(session, store.current, RANGES["1y"], NOW, [snap_model()])
    assert len(row["strip"]) == 365
    assert row["strip"][-3:] == ["ok", "degraded", "down"]
    assert set(row["strip"][:-3]) == {"unknown"}
    assert row["uptime"] == pytest.approx(100 * 2160 / 2980)
    assert row["peak_rpm"] == pytest.approx(2.0)  # yesterday 2880/1440 and today 720/360 tie


def test_prices_modelsdev_and_traffic_columns(env):
    store, sf = env
    add(sf, DailyStat(day=TODAY, model="qwen", source="models.dev", input_median=0.25, input_p5=0.12, input_p95=0.4,
                      output_median=1.0, output_p5=0.5, output_p95=2.0, providers=7),
        DailyStat(day=TODAY - timedelta(days=3), model="qwen", source="models.dev", input_median=9.0))
    with sf() as session:
        (row,) = model_rows(session, store.current, RANGES["24h"], NOW, [snap_model()])
    assert row["id"] == "qwen" and row["description"] == "qwen description"
    assert (row["healthy"], row["configured"]) == (1, 2)
    assert (row["input_price"], row["output_price"]) == (0.2, 0.8)
    assert (row["modelsdev_input_median"], row["modelsdev_input_p5"], row["modelsdev_input_p95"]) == (0.25, 0.12, 0.4)
    assert (row["modelsdev_output_median"], row["modelsdev_providers"]) == (1.0, 7)
    assert (row["requests_24h"], row["tokens_24h"]) == (2000, 2000000)


def test_prices_fall_back_to_the_latest_daily_row(env):
    store, sf = env
    add(sf, lumen_row(TODAY - timedelta(days=1), input_price=0.3, output_price=1.2), lumen_row(TODAY, polls=3))
    with sf() as session:
        (row,) = model_rows(session, store.current, RANGES["24h"], NOW, [snap_model(input_price=None, output_price=None, tokens=None)])
    assert (row["input_price"], row["output_price"]) == (0.3, 1.2)
    assert row["tokens_24h"] is None
    assert row["modelsdev_input_median"] is None


def test_no_gpu_config_is_flagged(env):
    store, sf = env
    with sf() as session:
        rows = model_rows(session, store.current, RANGES["24h"], NOW, [snap_model(), snap_model("other")])
    by_id = {r["id"]: r for r in rows}
    assert by_id["qwen"]["hw_configured"] is True
    assert by_id["other"]["hw_configured"] is False
    assert by_id["other"]["hw_daily_cost"] is None and by_id["other"]["hw_per_mtok"] is None


def test_model_pinned_with_lumen_id_is_configured(env):
    store, sf = env
    store.path.write_text(CONFIG + "  deepseek-v4-flash-0731: {gpu: rtx6000, gpus: 4, lumen_id: deepseek-v4-flash}\n")
    store.reload()
    with sf() as session:
        rows = model_rows(session, store.current, RANGES["24h"], NOW, [snap_model("deepseek-v4-flash"), snap_model("deepseek-v4-flash-0731")])
    by_id = {r["id"]: r for r in rows}
    assert by_id["deepseek-v4-flash"]["hw_configured"] is True
    assert by_id["deepseek-v4-flash-0731"]["hw_configured"] is False


def test_hardware_cost_per_million_tokens_over_the_range(env):
    store, sf = env
    yesterday = TODAY - timedelta(days=1)
    add(sf,
        DailyStat(day=yesterday, model="qwen", source="hardware", daily_cost=24.0),
        lumen_row(yesterday, tokens=4e6, requests=4000.0, final=True),
        DailyStat(day=TODAY, model="qwen", source="hardware", daily_cost=48.0),
        # today is counted until 06:00 local: a quarter of the day's cost
        lumen_row(TODAY, tokens=2e6, requests=1000.0, tokens_covered_until=MIDNIGHT + timedelta(hours=6),
                  covered_until=MIDNIGHT + timedelta(hours=6)))
    with sf() as session:
        (week,) = model_rows(session, store.current, RANGES["7d"], NOW, [snap_model()])
        (hour,) = model_rows(session, store.current, RANGES["1h"], NOW, [snap_model()])
    assert week["hw_daily_cost"] == 48.0  # the latest day
    assert week["hw_per_mtok"] == pytest.approx((24 + 12) / 6)
    assert week["hw_partial"] is True  # today is not final
    assert hour["hw_per_mtok"] == pytest.approx(12 / 2)


def test_hardware_cost_marker_only_for_inexact_days(env):
    store, sf = env
    days = [TODAY - timedelta(days=2), TODAY - timedelta(days=1)]
    add(sf, *(DailyStat(day=d, model="qwen", source="hardware", daily_cost=24.0) for d in days),
        lumen_row(days[0], tokens=1e6, final=True), lumen_row(days[1], tokens=1e6, final=True))
    tz = store.current.timezone
    with sf() as session:
        rows = {(r.source, r.model, r.day): r for r in session.execute(select(DailyStat)).scalars().all()}
    assert hardware_unit_cost(rows, "qwen", days, tz) == {"hw_per_mtok": 24.0, "hw_per_request": None, "hw_partial": False}
    rows[("lumen", "qwen", days[1])].estimated = True
    assert hardware_unit_cost(rows, "qwen", days, tz)["hw_partial"] is True


def test_hardware_cost_falls_back_to_per_request(env):
    store, sf = env
    day = TODAY - timedelta(days=1)
    add(sf, DailyStat(day=day, model="qwen", source="hardware", daily_cost=24.0), lumen_row(day, requests=4800.0, final=True))
    with sf() as session:
        (row,) = model_rows(session, store.current, RANGES["7d"], NOW, [snap_model(tokens=None)])
    assert row["hw_per_mtok"] is None
    assert row["hw_per_request"] == pytest.approx(0.005)
    assert row["hw_partial"] is False


def test_counted_fraction(env):
    store, _ = env
    tz = store.current.timezone
    assert counted_fraction(DailyStat(day=TODAY, final=True), "covered_until", tz) == 1
    assert counted_fraction(DailyStat(day=TODAY, final=False), "covered_until", tz) == 0
    row = DailyStat(day=TODAY, final=False, covered_until=MIDNIGHT + timedelta(hours=18))
    assert counted_fraction(row, "covered_until", tz) == 0.75


# --- Sorting ------------------------------------------------------------------------


ROWS = [
    {"id": "b", "uptime": 50.0, "peak_rpm": None},
    {"id": "a", "uptime": 100.0, "peak_rpm": 3},
    {"id": "C", "uptime": None, "peak_rpm": 7},
]


def test_numeric_columns_sort_descending_first_with_missing_last():
    rows, sort, direction = sort_rows(ROWS, "uptime", None)
    assert (sort, direction) == ("uptime", "desc")
    assert [r["id"] for r in rows] == ["a", "b", "C"]
    rows, _, _ = sort_rows(ROWS, "uptime", "asc")
    assert [r["id"] for r in rows] == ["b", "a", "C"]
    rows, _, _ = sort_rows(ROWS, "peak", None)
    assert [r["id"] for r in rows] == ["C", "a", "b"]


def test_text_columns_sort_ascending_first_and_unknown_keys_fall_back():
    rows, sort, direction = sort_rows(ROWS, "model", None)
    assert (sort, direction) == ("model", "asc")
    assert [r["id"] for r in rows] == ["a", "b", "C"]
    rows, sort, direction = sort_rows(ROWS, "bogus", "sideways")
    assert (sort, direction) == ("model", "asc")
    rows, _, _ = sort_rows(ROWS, "model", "desc")
    assert [r["id"] for r in rows] == ["C", "b", "a"]


# --- Status cards and the current model list ------------------------------------------


def test_status_cards_sum_the_snapshot_and_today(env):
    store, sf = env
    add(sf, lumen_row(TODAY, requests=500.0, tokens=None), lumen_row(TODAY, model="other", requests=250.0, tokens=None, estimated=True),
        lumen_row(TODAY - timedelta(days=1), requests=9999.0))
    models = [snap_model(tokens=None), snap_model("other", healthy=0, configured=1, tokens=None)]
    with sf() as session:
        cards = status_cards(session, store.current, NOW, models)
    assert cards == {
        "active_models": 2, "healthy": 1, "configured": 3,
        "requests_last_hour": 200, "requests_today": 750,
        "tokens_last_hour": None, "tokens_today": None,  # shown as "—" until Lumen reports tokens
        "today_estimated": True,
    }


def test_status_cards_with_tokens(env):
    store, sf = env
    add(sf, lumen_row(TODAY, requests=10.0, tokens=1000.0))
    with sf() as session:
        cards = status_cards(session, store.current, NOW, [snap_model()])
    assert (cards["tokens_last_hour"], cards["tokens_today"], cards["today_estimated"]) == (100000, 1000, False)


def test_current_models_before_the_first_probe_after_a_restart(env):
    _, sf = env
    add(sf, sample("qwen", 5, "degraded"), sample("old", 10), sample("qwen", 10))
    with sf() as session:
        at, models = current_models(session, LiveSnapshot())
        assert at == NOW - timedelta(minutes=5)
        assert [(m["id"], m["status"], m["healthy"]) for m in models] == [("qwen", "degraded", 1)]
        snapshot = LiveSnapshot()
        snapshot.set(Snapshot(NOW, [snap_model()]))
        assert current_models(session, snapshot) == (NOW, [snap_model()])


def test_overview_payload(env):
    store, sf = env
    snapshot = LiveSnapshot()
    snapshot.set(Snapshot(NOW - timedelta(minutes=1), [snap_model("b"), snap_model("a")]))
    with sf() as session:
        data = overview(session, store.current, snapshot, RANGES["12h"], NOW, "requests", None)
    assert data["range"] == "12h"
    assert data["generated_at"] == "2026-10-17T17:00:00Z" and data["probed_at"] == "2026-10-17T16:59:00Z"
    assert (data["sort"], data["direction"]) == ("requests", "desc")
    assert [m["id"] for m in data["models"]] == ["a", "b"]  # equal values keep the id order
    assert data["cards"]["active_models"] == 2
    assert all(len(m["strip"]) == 240 for m in data["models"])
    # 12h buckets are 3 minutes, the last one starting 3 minutes before now.
    assert len(data["strip_times"]) == 240
    assert data["strip_times"][0] == "2026-10-17T05:00:00Z" and data["strip_times"][-1] == "2026-10-17T16:57:00Z"


@pytest.mark.parametrize("range_key", list(RANGES))
def test_strip_times_match_the_strips(env, range_key):
    store, sf = env
    snapshot = LiveSnapshot()
    snapshot.set(Snapshot(NOW, [snap_model()]))
    with sf() as session:
        data = overview(session, store.current, snapshot, RANGES[range_key], NOW)
    assert len(data["strip_times"]) == len(data["models"][0]["strip"])


def test_1y_strip_times_are_local_dates(env):
    store, sf = env
    snapshot = LiveSnapshot()
    snapshot.set(Snapshot(NOW, [snap_model()]))
    with sf() as session:
        data = overview(session, store.current, snapshot, RANGES["1y"], NOW)
    assert len(data["strip_times"]) == 365
    assert data["strip_times"][-2:] == ["2026-10-16", "2026-10-17"]
