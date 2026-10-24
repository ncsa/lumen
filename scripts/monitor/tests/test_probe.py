import os
from datetime import date, datetime, timedelta

import pytest
import requests
from sqlalchemy import select

from monitor import migrate
from monitor.cli import main
from monitor.config import ConfigStore
from monitor.db import make_engine, make_session_factory
from monitor.models import DailyStat, Probe, Sample
from monitor.probe import DEGRADED, DOWN, UNKNOWN, UP, LiveSnapshot, lumen_state, run_probe

CONFIG = """
lumen: {url: "https://lumen.test", api_key: secret}
timezone: America/Chicago
retention: 31d
"""
# 2026-10-17 12:00 in Chicago
NOON = datetime(2026, 10, 17, 17, 0)
DAY = date(2026, 10, 17)


def lumen_model(model_id, status="ok", configured=1, healthy=1, parent=None, **extra):
    row = {
        "id": model_id, "root": f"org/{model_id}", "parent": parent, "status": status,
        "description": f"{model_id} description",
        "input_cost_per_million": 0.2, "output_cost_per_million": 0.8,
        "instances": {"configured": configured, "healthy": healthy},
        "requests": {"last_minute": 3, "last_hour": 60, "last_day": 900},
    }
    row.update(extra)
    return row


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status} Server Error")

    def json(self):
        return self.payload


class FakeLumen:
    """Answers GET /v1/models and advances a fake clock by `delay` seconds per call."""

    def __init__(self):
        self.now = 100.0
        self.delay = 0.25
        self.status = 200
        self.error = None
        self.data = [
            lumen_model(
                "qwen",
                input_tokens={"last_minute": 1000},
                output_tokens={"last_minute": 200},
            ),
            lumen_model("qwen-alias", parent="qwen"),
            lumen_model("big", status="degraded", configured=2, healthy=1),
            lumen_model("gone", status="down", healthy=0),
        ]

    def clock(self):
        return self.now

    def get(self, url, params=None, headers=None, timeout=None):
        assert url == "https://lumen.test/v1/models"
        assert params == {"tokens": "true"}
        assert headers == {"Authorization": "Bearer secret"}
        assert timeout == 15
        self.now += self.delay
        if self.error:
            raise self.error
        return Response({"data": self.data}, self.status)


@pytest.fixture
def lumen(monkeypatch):
    fake = FakeLumen()
    monkeypatch.setattr(requests, "get", fake.get)
    return fake


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'monitor.db'}"
    monkeypatch.setenv("MONITOR_DATABASE_URL", url)
    monkeypatch.delenv("MONITOR_LUMEN_API_KEY", raising=False)
    migrate.upgrade(url)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG)
    engine = make_engine(url)
    yield config_path, make_session_factory(engine)
    engine.dispose()


def probe(env, lumen, now=NOON, snapshot=None):
    config_path, session_factory = env
    return run_probe(ConfigStore(config_path), session_factory, snapshot=snapshot, now=now, clock=lumen.clock)


def all_rows(session_factory, model):
    with session_factory() as session:
        return session.execute(select(model).order_by(model.id)).scalars().all()


def state(session_factory):
    with session_factory() as session:
        return lumen_state(session)


def add_probes(session_factory, *results, start=NOON):
    """One probe per minute; each result is True, False or (model_count, models_up)."""
    with session_factory() as session:
        for i, result in enumerate(results):
            counts = result if isinstance(result, tuple) else (2, 2) if result else (None, None)
            session.add(Probe(polled_at=start + timedelta(minutes=i), success=result is not False, response_time_ms=100, model_count=counts[0], models_up=counts[1]))
        session.commit()


# --- probes rows ---------------------------------------------------------


def test_success_writes_a_probe_row(env, lumen):
    _, session_factory = env
    result = probe(env, lumen)
    assert result.success and result.error is None
    [row] = all_rows(session_factory, Probe)
    assert (row.polled_at, row.success, row.model_count, row.models_up) == (NOON, True, 3, 2)  # alias dropped, "down" not up
    assert row.response_time_ms == pytest.approx(250)


@pytest.mark.parametrize("error, status, delay", [
    (requests.Timeout("read timed out"), 200, 15.0),
    (None, 503, 0.5),
    (requests.ConnectionError("connection refused"), 200, 0.01),
])
def test_failure_writes_a_failed_probe_row(env, lumen, error, status, delay):
    _, session_factory = env
    lumen.error, lumen.status, lumen.delay = error, status, delay
    result = probe(env, lumen)
    assert not result.success and result.error
    [row] = all_rows(session_factory, Probe)
    assert (row.success, row.model_count, row.models_up) == (False, None, None)
    assert row.response_time_ms == pytest.approx(delay * 1000)
    assert all_rows(session_factory, Sample) == []
    assert all_rows(session_factory, DailyStat) == []


def test_invalid_json_is_a_failed_probe(env, lumen, monkeypatch):
    _, session_factory = env
    monkeypatch.setattr(Response, "json", lambda self: [])
    assert not probe(env, lumen).success
    assert all_rows(session_factory, Probe)[0].success is False


# --- samples and snapshot -------------------------------------------------


def test_samples_per_canonical_model(env, lumen):
    _, session_factory = env
    probe(env, lumen)
    samples = {s.model: s for s in all_rows(session_factory, Sample)}
    assert set(samples) == {"qwen", "big", "gone"}
    qwen = samples["qwen"]
    assert (qwen.sampled_at, qwen.status, qwen.configured, qwen.healthy) == (NOON, "ok", 1, 1)
    assert qwen.requests_last_minute == 3
    assert (qwen.input_tokens_last_minute, qwen.output_tokens_last_minute, qwen.tokens_last_minute) == (1000, 200, 1200)
    assert samples["big"].tokens_last_minute is None  # Lumen without token counts
    assert (samples["big"].input_tokens_last_minute, samples["big"].output_tokens_last_minute) == (None, None)
    assert (samples["big"].status, samples["big"].configured, samples["big"].healthy) == ("degraded", 2, 1)


def test_snapshot_keeps_the_last_success(env, lumen):
    snapshot = LiveSnapshot()
    assert snapshot.get() is None
    probe(env, lumen, snapshot=snapshot)
    snap = snapshot.get()
    assert snap.polled_at == NOON
    assert [m["id"] for m in snap.models] == ["qwen", "big", "gone"]
    assert snap.models[0]["description"] == "qwen description"

    lumen.status = 500
    probe(env, lumen, now=NOON + timedelta(minutes=1), snapshot=snapshot)
    assert snapshot.get() is snap


# --- daily counters --------------------------------------------------------


def test_daily_counters(env, lumen):
    _, session_factory = env
    probe(env, lumen)
    lumen.data[0]["status"] = "degraded"
    lumen.data[2]["instances"]["healthy"] = 2
    probe(env, lumen, now=NOON + timedelta(minutes=1))
    lumen.status = 500  # a failed probe counts for nobody
    probe(env, lumen, now=NOON + timedelta(minutes=2))

    rows = {r.model: r for r in all_rows(session_factory, DailyStat)}
    assert {r.source for r in rows.values()} == {"lumen"}
    assert {r.day for r in rows.values()} == {DAY}
    assert (rows["qwen"].polls, rows["qwen"].polls_ok, rows["qwen"].healthy_sum) == (2, 1, 2)
    assert (rows["big"].polls, rows["big"].polls_ok, rows["big"].healthy_sum) == (2, 0, 3)
    assert (rows["gone"].polls, rows["gone"].polls_ok, rows["gone"].healthy_sum) == (2, 0, 0)
    # the latest probe's values
    assert (rows["big"].status, rows["big"].configured, rows["big"].healthy) == ("degraded", 2, 2)


def test_daily_counters_keep_the_price_columns(env, lumen):
    _, session_factory = env
    with session_factory() as session:
        session.add(DailyStat(day=DAY, model="qwen", source="lumen", input_price=0.2, polls=5, polls_ok=5, healthy_sum=5))
        session.commit()
    probe(env, lumen)
    [row] = [r for r in all_rows(session_factory, DailyStat) if r.model == "qwen"]
    assert (row.input_price, row.polls, row.polls_ok, row.healthy_sum) == (0.2, 6, 6, 6)


def test_daily_counters_use_the_local_day(env, lumen):
    _, session_factory = env
    probe(env, lumen, now=datetime(2026, 10, 18, 4, 59))  # 23:59 in Chicago
    probe(env, lumen, now=datetime(2026, 10, 18, 5, 0))  # 00:00 next day
    days = sorted((r.day, r.polls) for r in all_rows(session_factory, DailyStat) if r.model == "qwen")
    assert days == [(DAY, 1), (date(2026, 10, 18), 1)]


# --- Lumen state ------------------------------------------------------------


def test_state_unknown_when_empty(env):
    assert state(env[1]) == UNKNOWN


def test_state_down_after_three_failures(env):
    add_probes(env[1], True, False, False, False)
    assert state(env[1]) == DOWN


def test_state_up_with_two_failures_and_one_success(env):
    add_probes(env[1], False, True, False, False)
    assert state(env[1]) == UP
    add_probes(env[1], True, start=NOON + timedelta(minutes=10))
    assert state(env[1]) == UP


def test_state_degraded_when_a_model_is_down(env):
    add_probes(env[1], True, (3, 2))
    assert state(env[1]) == DEGRADED


def test_state_degraded_only_from_the_latest_probe(env):
    add_probes(env[1], (3, 2), False)
    assert state(env[1]) == UP


def test_state_down_before_three_probes_when_all_failed(env):
    add_probes(env[1], False)
    assert state(env[1]) == DOWN


def test_state_survives_a_restart(env, lumen):
    lumen.status = 502
    for minute in range(3):
        probe(env, lumen, now=NOON + timedelta(minutes=minute))
    # a fresh engine and no in-memory state, as after a restart
    engine = make_engine(os.environ["MONITOR_DATABASE_URL"])
    try:
        assert state(make_session_factory(engine)) == DOWN
    finally:
        engine.dispose()


def test_state_from_probe_runs(env, lumen):
    _, session_factory = env
    probe(env, lumen)
    assert state(session_factory) == DEGRADED  # "gone" is down
    lumen.data[3]["status"] = "ok"
    probe(env, lumen, now=NOON + timedelta(minutes=1))
    assert state(session_factory) == UP


# --- retention ---------------------------------------------------------------


def test_first_probe_of_the_day_prunes_old_rows(env, lumen):
    _, session_factory = env
    yesterday = NOON - timedelta(days=1)
    old = NOON.replace(hour=4) - timedelta(days=31, minutes=1)  # just past 31d at local midnight
    kept = NOON.replace(hour=6) - timedelta(days=31)  # inside 31d from the next probe
    with session_factory() as session:
        for at in (old, kept, yesterday):
            session.add(Probe(polled_at=at, success=True, response_time_ms=100, model_count=1, models_up=1))
            session.add(Sample(model="qwen", sampled_at=at, status="ok"))
        session.commit()

    midnight = datetime(2026, 10, 17, 5, 0)  # 00:00 in Chicago
    assert probe(env, lumen, now=midnight).pruned
    assert {p.polled_at for p in all_rows(session_factory, Probe)} == {kept, yesterday, midnight}
    assert {s.sampled_at for s in all_rows(session_factory, Sample)} == {kept, yesterday, midnight}


def test_later_probes_the_same_day_do_not_prune(env, lumen):
    _, session_factory = env
    probe(env, lumen, now=datetime(2026, 10, 17, 5, 0))
    stale = NOON - timedelta(days=40)
    with session_factory() as session:
        session.add(Probe(polled_at=stale, success=True, response_time_ms=100))
        session.commit()
    assert not probe(env, lumen).pruned
    assert stale in {p.polled_at for p in all_rows(session_factory, Probe)}


def test_failed_probe_also_prunes(env, lumen):
    _, session_factory = env
    with session_factory() as session:
        session.add(Probe(polled_at=NOON - timedelta(days=40), success=True, response_time_ms=100))
        session.commit()
    lumen.error = requests.ConnectionError("refused")
    assert probe(env, lumen).pruned
    assert [p.polled_at for p in all_rows(session_factory, Probe)] == [NOON]


# --- CLI -------------------------------------------------------------------------


def test_cli_collect_probe(env, lumen):
    config_path, session_factory = env
    assert main(["--config", str(config_path), "collect", "--probe"]) == 0
    assert len(all_rows(session_factory, Probe)) == 1
    assert len(all_rows(session_factory, Sample)) == 3


def test_cli_collect_probe_lumen_down(env, lumen):
    config_path, session_factory = env
    lumen.status = 500
    assert main(["--config", str(config_path), "collect", "--probe"]) == 1
    assert all_rows(session_factory, Probe)[0].success is False
