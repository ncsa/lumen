from datetime import date, datetime, timedelta

import pytest

from monitor import migrate
from monitor.buckets import RANGES
from monitor.config import ConfigStore
from monitor.db import make_engine, make_session_factory
from monitor.detail import series
from monitor.models import DailyStat, Sample
from monitor.probe import LiveSnapshot, Snapshot

CONFIG = """
lumen: {url: "https://lumen.test"}
timezone: America/Chicago
price:
  kwh: 0.24
  rtx6000: {energy: 600, price: 625}
models:
  org/qwen: {gpu: rtx6000, gpus: 1}
"""
MODEL = "org/qwen"
NOW = datetime(2026, 10, 17, 17, 0)  # 12:00 in Chicago (CDT, UTC-5)
TODAY = date(2026, 10, 17)
MIDNIGHT = datetime(2026, 10, 17, 5, 0)  # local midnight starting TODAY, as UTC
# Points per range: buckets below 7d, local days from 7d up.
TRAFFIC_POINTS = {"1h": 60, "12h": 240, "24h": 288, "7d": 7, "30d": 30, "1y": 365}
STRIP_BUCKETS = {"1h": 60, "12h": 240, "24h": 288, "7d": 336, "30d": 360, "1y": 365}
# Local days the price and hardware series cover: 12:00 local minus 12h or 24h reaches into yesterday.
DAYS = {"1h": 1, "12h": 1, "24h": 2, "7d": 7, "30d": 30, "1y": 365}


def snap_model(**extra):
    return {"id": MODEL, "description": "Qwen 80B", "status": "ok", "configured": 2, "healthy": 1,
            "input_price": 0.2, "output_price": 0.8, "requests": {"last_day": 2000}, "tokens": None, **extra}


@pytest.fixture
def env(tmp_path):
    url = f"sqlite:///{tmp_path / 'monitor.db'}"
    migrate.upgrade(url)
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    engine = make_engine(url)
    snapshot = LiveSnapshot()
    snapshot.set(Snapshot(NOW, [snap_model()]))
    yield ConfigStore(path, env={}), make_session_factory(engine), snapshot
    engine.dispose()


def add(session_factory, *rows):
    with session_factory() as session:
        session.add_all(rows)
        session.commit()


def sample(minutes_ago, status="ok", rpm=None, tpm=None, healthy=1, configured=2, itpm=None, otpm=None):
    return Sample(model=MODEL, sampled_at=NOW - timedelta(minutes=minutes_ago), status=status,
                  configured=configured, healthy=healthy, requests_last_minute=rpm,
                  input_tokens_last_minute=itpm if tpm is None else tpm * 3 // 4,
                  output_tokens_last_minute=otpm if tpm is None else tpm // 4)


def split_tokens(values):
    """Stores a combined `tokens` / `tokens_covered_until` as 3/4 input and 1/4 output tokens."""
    if "tokens" in values:
        tokens = values.pop("tokens")
        values["input_tokens"] = None if tokens is None else tokens * 3 / 4
        values["output_tokens"] = None if tokens is None else tokens / 4
    if "tokens_covered_until" in values:
        values["input_tokens_covered_until"] = values["output_tokens_covered_until"] = values.pop("tokens_covered_until")
    return values


def daily(day, source, **values):
    return DailyStat(day=day, model=MODEL, source=source, **split_tokens(values))


def get(env, range_key, model=MODEL):
    store, sf, snapshot = env
    with sf() as session:
        return series(session, store.current, snapshot, model, RANGES[range_key], NOW)


@pytest.mark.parametrize("range_key", list(RANGES))
def test_every_series_for_every_range(env, range_key):
    data = get(env, range_key)
    assert data["range"] == range_key and data["model"] == MODEL
    assert data["daily"] is (range_key in ("7d", "30d", "1y"))
    assert data["traffic"]["unit"] == ("day" if data["daily"] else "minute")
    assert len(data["traffic"]["points"]) == TRAFFIC_POINTS[range_key]
    assert len(data["header"]["strip"]) == STRIP_BUCKETS[range_key]
    assert data["instances"]["unit"] == ("day" if range_key == "1y" else "bucket")
    assert len(data["instances"]["points"]) == STRIP_BUCKETS[range_key]
    assert len(data["prices"]) == len(data["hardware"]["points"]) == DAYS[range_key]
    assert data["prices"][-1]["day"] == data["hardware"]["points"][-1]["day"] == "2026-10-17"
    assert set(data["prices"][0]) == {"day", "input_price", "output_price", "input_median", "input_p5", "input_p95",
                                      "output_median", "output_p5", "output_p95"}
    assert set(data["hardware"]["range"]) == {"benefit", "cost", "partial"}
    assert data["header"]["description"] == "Qwen 80B" and data["header"]["status"] == "ok"


def test_short_ranges_use_per_bucket_rates(env):
    _, sf, _ = env
    add(sf, sample(0.5, rpm=4, tpm=400), sample(0.25, rpm=6, tpm=None), sample(10, rpm=2, tpm=200, healthy=2),
        sample(120, rpm=100))  # outside 1h
    data = get(env, "1h")
    points = data["traffic"]["points"]
    assert points[-1] == {"t": "2026-10-17T16:59:00Z", "requests": 5, "tokens": 400,
                          "input_tokens": 300, "output_tokens": 100, "estimated": False}
    assert points[-10]["requests"] == 2 and points[-10]["tokens"] == 200
    assert points[0]["requests"] is None
    assert data["traffic"]["average"] == {"requests": pytest.approx((5 + 2) / 2), "tokens": pytest.approx(300),
                                          "input_tokens": pytest.approx(225), "output_tokens": pytest.approx(75)}
    assert data["instances"]["points"][-1] == {"t": "2026-10-17T16:59:00Z", "healthy": 1, "configured": 2}
    assert data["instances"]["points"][-10]["healthy"] == 2


def test_daily_series_from_7d_mark_inexact_days(env):
    _, sf, _ = env
    yesterday, before = TODAY - timedelta(days=1), TODAY - timedelta(days=2)
    add(sf, daily(before, "lumen", requests=100.0, tokens=1e6, final=True),
        daily(yesterday, "lumen", requests=200.0, tokens=2e6, final=True, estimated=True),
        daily(TODAY, "lumen", requests=50.0, tokens=5e5),
        sample(3 * 60 * 24, rpm=99))  # raw samples are not used for daily traffic
    for range_key in ("7d", "30d", "1y"):
        points = get(env, range_key)["traffic"]["points"]
        assert points[-3:] == [
            {"t": "2026-10-15", "requests": 100.0, "tokens": 1e6, "input_tokens": 7.5e5, "output_tokens": 2.5e5, "estimated": False},
            {"t": "2026-10-16", "requests": 200.0, "tokens": 2e6, "input_tokens": 1.5e6, "output_tokens": 5e5, "estimated": True},
            {"t": "2026-10-17", "requests": 50.0, "tokens": 5e5, "input_tokens": 3.75e5, "output_tokens": 1.25e5,
             "estimated": True},  # not final yet
        ]
        assert points[-4] == {"t": "2026-10-14", "requests": None, "tokens": None, "input_tokens": None, "output_tokens": None,
                              "estimated": False}
    assert get(env, "7d")["traffic"]["average"]["requests"] == pytest.approx(350 / 3)


def test_per_bucket_split_tokens_and_averages(env):
    _, sf, _ = env
    add(sf, sample(0.5, itpm=400, otpm=100), sample(0.25, itpm=600, otpm=300),
        sample(10, itpm=40, otpm=10),
        sample(20, rpm=1))  # no token counts
    data = get(env, "1h")
    points = data["traffic"]["points"]
    assert points[-1]["input_tokens"] == 500 and points[-1]["output_tokens"] == 200 and points[-1]["tokens"] == 700
    assert points[-10]["input_tokens"] == 40 and points[-10]["output_tokens"] == 10 and points[-10]["tokens"] == 50
    assert points[-20] == {"t": "2026-10-17T16:40:00Z", "requests": 1, "tokens": None,
                           "input_tokens": None, "output_tokens": None, "estimated": False}
    average = data["traffic"]["average"]
    # the averages only cover points that have token counts
    assert average["input_tokens"] == pytest.approx((500 + 40) / 2)
    assert average["output_tokens"] == pytest.approx((200 + 10) / 2)
    assert average["tokens"] == pytest.approx((700 + 50) / 2)


def test_per_day_split_tokens_and_averages(env):
    _, sf, _ = env
    d1, d2 = TODAY - timedelta(days=2), TODAY - timedelta(days=1)
    add(sf, daily(d1, "lumen", requests=10.0, final=True),  # a day without token counts
        daily(d2, "lumen", input_tokens=4e6, output_tokens=1e6, final=True),
        daily(TODAY, "lumen", input_tokens=2e5, output_tokens=1e5))
    traffic = get(env, "30d")["traffic"]
    assert [(p["t"], p["tokens"], p["input_tokens"], p["output_tokens"]) for p in traffic["points"][-3:]] == [
        ("2026-10-15", None, None, None),
        ("2026-10-16", 5e6, 4e6, 1e6),
        ("2026-10-17", 3e5, 2e5, 1e5),
    ]
    assert traffic["average"]["input_tokens"] == pytest.approx((4e6 + 2e5) / 2)
    assert traffic["average"]["output_tokens"] == pytest.approx((1e6 + 1e5) / 2)
    assert traffic["average"]["tokens"] == pytest.approx((5e6 + 3e5) / 2)


def test_price_band_median_and_ours_per_day(env):
    _, sf, _ = env
    yesterday = TODAY - timedelta(days=1)
    add(sf, daily(yesterday, "models.dev", input_median=0.25, input_p5=0.12, input_p95=0.4,
                  output_median=1.0, output_p5=0.5, output_p95=2.0, providers=5),
        daily(yesterday, "lumen", input_price=0.2, output_price=0.8),
        daily(TODAY, "lumen", input_price=0.3, output_price=0.9))
    prices = get(env, "7d")["prices"]
    assert prices[-2] == {"day": "2026-10-16", "input_price": 0.2, "output_price": 0.8,
                          "input_median": 0.25, "input_p5": 0.12, "input_p95": 0.4,
                          "output_median": 1.0, "output_p5": 0.5, "output_p95": 2.0}
    assert prices[-1]["input_price"] == 0.3 and prices[-1]["input_median"] is None
    assert prices[0]["input_price"] is None


def split(day, **values):
    """A lumen row with input/output tokens and our prices."""
    return daily(day, "lumen", input_price=0.2, output_price=0.8, **values)


def test_benefit_on_an_exact_final_day(env):
    _, sf, _ = env
    day = TODAY - timedelta(days=1)
    add(sf, daily(day, "hardware", daily_cost=24.0), split(day, input_tokens=10e6, output_tokens=5e6, final=True))
    point = get(env, "7d")["hardware"]["points"][-2]
    # 10 M input at $0.2 + 5 M output at $0.8
    assert point == {"day": "2026-10-16", "benefit": pytest.approx(6.0), "cost": 24.0, "partial": False, "estimated": False}


def test_no_benefit_without_split_counts(env):
    _, sf, _ = env
    day = TODAY - timedelta(days=1)
    add(sf, daily(day, "hardware", daily_cost=24.0), split(day, requests=100.0, final=True))
    hardware = get(env, "7d")["hardware"]
    assert hardware["points"][-2]["benefit"] is None and hardware["points"][-2]["cost"] == 24.0
    assert hardware["range"] == {"benefit": None, "cost": None, "partial": False}


def test_no_benefit_without_a_price(env):
    _, sf, _ = env
    day = TODAY - timedelta(days=1)
    add(sf, daily(day, "hardware", daily_cost=24.0),
        daily(day, "lumen", input_tokens=10e6, output_tokens=5e6, input_price=0.2, final=True))
    assert get(env, "7d")["hardware"]["points"][-2]["benefit"] is None


def test_today_cost_scaled_to_the_counted_part_and_partial(env):
    _, sf, _ = env
    six = MIDNIGHT + timedelta(hours=6)
    add(sf, daily(TODAY, "hardware", daily_cost=48.0),
        # counted until 06:00 local (output) and 12:00 (input): the cost covers the shorter quarter day
        split(TODAY, input_tokens=4e6, output_tokens=1e6, input_tokens_covered_until=six + timedelta(hours=6),
              output_tokens_covered_until=six, covered_until=six + timedelta(hours=6)))
    point = get(env, "7d")["hardware"]["points"][-1]
    assert point["cost"] == pytest.approx(12.0) and point["partial"] is True
    assert point["benefit"] == pytest.approx(4 * 0.2 + 0.8)


def test_today_without_split_scales_by_the_requests_watermark(env):
    _, sf, _ = env
    add(sf, daily(TODAY, "hardware", daily_cost=48.0),
        daily(TODAY, "lumen", requests=1000.0, covered_until=MIDNIGHT + timedelta(hours=12)))
    point = get(env, "7d")["hardware"]["points"][-1]
    assert point["cost"] == pytest.approx(24.0) and point["benefit"] is None and point["partial"] is True


def test_range_totals_add_days_with_both(env):
    _, sf, _ = env
    d1, d2, d3 = (TODAY - timedelta(days=n) for n in (3, 2, 1))
    add(sf,
        daily(d1, "hardware", daily_cost=24.0), split(d1, input_tokens=10e6, output_tokens=5e6, final=True),
        daily(d2, "hardware", daily_cost=24.0), split(d2, input_tokens=20e6, output_tokens=10e6, final=True, estimated=True),
        # no counts: its cost stays out of the totals
        daily(d3, "hardware", daily_cost=24.0), split(d3, requests=100.0, final=True),
        daily(TODAY, "hardware", daily_cost=48.0),
        split(TODAY, input_tokens=1e6, output_tokens=1e6, input_tokens_covered_until=MIDNIGHT + timedelta(hours=6),
              output_tokens_covered_until=MIDNIGHT + timedelta(hours=6)))
    hardware = get(env, "7d")["hardware"]
    assert hardware["points"][-3]["estimated"] is True
    assert hardware["range"] == {"benefit": pytest.approx(6 + 12 + 1), "cost": pytest.approx(24 + 24 + 12), "partial": True}


def test_1y_instances_from_the_daily_counters(env):
    _, sf, _ = env
    add(sf, daily(TODAY - timedelta(days=1), "lumen", polls=4, polls_ok=4, healthy_sum=6, configured=2))
    data = get(env, "1y")
    assert data["instances"]["points"][-2] == {"t": "2026-10-16", "healthy": 1.5, "configured": 2}
    assert data["instances"]["points"][-1] == {"t": "2026-10-17", "healthy": None, "configured": None}
    assert data["header"]["strip"][-2] == "ok"


def test_model_with_history_only_and_unknown_model(env):
    _, sf, _ = env
    add(sf, Sample(model="gone", sampled_at=NOW - timedelta(minutes=5), status="ok", configured=1, healthy=1))
    data = get(env, "1h", "gone")
    assert data["header"]["id"] == "gone" and data["header"]["status"] == "unknown"
    assert data["header"]["strip"][-5] == "ok"
    assert get(env, "1h", "never-seen") is None
