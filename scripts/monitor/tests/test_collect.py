from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
import requests
from sqlalchemy import func, select

from monitor import migrate, modelsdev
from monitor.cli import main
from monitor.collect import collect_prices
from monitor.config import ConfigStore
from monitor.daily import local_day
from monitor.db import make_engine, make_session_factory
from monitor.models import DailyStat

CONFIG = """
lumen: {url: "https://lumen.test", api_key: secret}
timezone: America/Chicago
price:
  kwh: 0.24
  rtx6000: {energy: 600, price: 625}
models:
  qwen: {gpu: rtx6000, gpus: 1}
  big: {gpu: rtx6000, gpus: 2, modelsdev_id: glm-4.5-air}
"""
# 2026-10-17 12:00 in Chicago
NOON = datetime(2026, 10, 17, 17, 0)
DAY = date(2026, 10, 17)
RTX6000_DAY = 625 * 12 / 365 + 0.6 * 24 * 0.24


def lumen_model(model_id, input_cost=0.2, output_cost=0.8, healthy=1, parent=None):
    return {
        "id": model_id, "root": f"org/{model_id}", "parent": parent, "status": "ok",
        "input_cost_per_million": input_cost, "output_cost_per_million": output_cost,
        "instances": {"configured": 1, "healthy": healthy},
        "requests": {"last_minute": 0, "last_hour": 0, "last_day": 0},
    }


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(str(self.status))

    def json(self):
        return self.payload


class FakeHttp:
    """Answers GET /v1/models and models.dev from mutable payloads."""

    def __init__(self):
        self.lumen = {"data": [
            lumen_model("qwen"),
            lumen_model("qwen-alias", parent="qwen"),
            lumen_model("big", input_cost=0.3, output_cost=1.2),
            lumen_model("unconfigured"),
        ]}
        self.lumen_status = 200
        self.modelsdev = {
            "a": {"models": {"qwen": {"id": "qwen", "cost": {"input": 0.1, "output": 0.4}}}},
            "b": {"models": {"qwen": {"id": "qwen", "cost": {"input": 0.3, "output": 0.0}}}},
            "c": {"models": {"glm-4.5-air": {"id": "glm-4.5-air", "cost": {"input": 0.2, "output": 1.1}}}},
        }

    def get(self, url, headers=None, timeout=None):
        if url == modelsdev.MODELSDEV_URL:
            if self.modelsdev is None:
                raise requests.ConnectionError("models.dev down")
            return Response(self.modelsdev)
        assert url == "https://lumen.test/v1/models"
        assert headers == {"Authorization": "Bearer secret"}
        return Response(self.lumen, self.lumen_status)


@pytest.fixture
def http(monkeypatch):
    fake = FakeHttp()
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


def rows(session_factory, source=None):
    with session_factory() as session:
        stmt = select(DailyStat).order_by(DailyStat.model, DailyStat.source)
        if source:
            stmt = stmt.where(DailyStat.source == source)
        return {(r.model, r.source): r for r in session.execute(stmt).scalars()}


def test_price_run_writes_every_source(env, http):
    config_path, session_factory = env
    written = collect_prices(ConfigStore(config_path), session_factory, now=NOON)
    assert written == {"lumen": 3, "models.dev": 2, "hardware": 2}
    stored = rows(session_factory)
    assert {r.day for r in stored.values()} == {DAY}

    assert (stored["qwen", "lumen"].input_price, stored["qwen", "lumen"].output_price) == (0.2, 0.8)
    assert ("qwen-alias", "lumen") not in stored

    dev = stored["qwen", "models.dev"]
    assert (dev.providers, dev.input_median, dev.output_median, dev.output_p5) == (2, pytest.approx(0.2), 0.4, 0.4)
    assert stored["big", "models.dev"].modelsdev_id == "glm-4.5-air"  # pinned

    hw = stored["qwen", "hardware"]
    assert hw.daily_cost == pytest.approx(24.01, abs=0.01)
    assert (hw.gpu, hw.gpus, hw.gpu_price, hw.energy, hw.kwh, hw.avg_healthy) == ("rtx6000", 1, 625, 600, 0.24, 1.0)
    assert stored["big", "hardware"].daily_cost == pytest.approx(2 * RTX6000_DAY)
    assert ("unconfigured", "hardware") not in stored


def test_day_is_the_local_day(env, http):
    config_path, session_factory = env
    collect_prices(ConfigStore(config_path), session_factory, now=datetime(2026, 10, 18, 3, 0))  # 22:00 in Chicago
    assert {r.day for r in rows(session_factory).values()} == {DAY}


def test_second_run_updates_the_same_rows(env, http):
    config_path, session_factory = env
    store = ConfigStore(config_path)
    collect_prices(store, session_factory, now=NOON)
    http.lumen["data"][0]["input_cost_per_million"] = 0.25
    collect_prices(store, session_factory, now=NOON.replace(hour=20))
    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(DailyStat)).scalar_one() == 7
    assert rows(session_factory)["qwen", "lumen"].input_price == 0.25


def test_hardware_uses_the_probe_average(env, http):
    config_path, session_factory = env
    with session_factory() as session:
        session.add(DailyStat(day=DAY, model="qwen", source="lumen", polls=4, polls_ok=2, healthy_sum=2))
        session.commit()
    collect_prices(ConfigStore(config_path), session_factory, now=NOON)
    stored = rows(session_factory)
    assert stored["qwen", "hardware"].avg_healthy == 0.5
    assert stored["qwen", "hardware"].daily_cost == pytest.approx(RTX6000_DAY / 2)
    # the price run keeps the probe counters
    assert (stored["qwen", "lumen"].polls, stored["qwen", "lumen"].healthy_sum) == (4, 2)


def test_before_any_probe_uses_current_healthy(env, http):
    config_path, session_factory = env
    http.lumen["data"][0]["instances"]["healthy"] = 0
    collect_prices(ConfigStore(config_path), session_factory, now=NOON)
    assert rows(session_factory)["qwen", "hardware"].daily_cost == 0


def test_config_is_reread_each_run(env, http):
    config_path, session_factory = env
    store = ConfigStore(config_path)
    collect_prices(store, session_factory, now=NOON)
    config_path.write_text(CONFIG.replace("qwen: {gpu: rtx6000, gpus: 1}", "qwen: {gpu: rtx6000, gpus: 2}"))
    collect_prices(store, session_factory, now=NOON)
    assert rows(session_factory)["qwen", "hardware"].gpus == 2


def test_modelsdev_failure_keeps_the_other_rows(env, http):
    config_path, session_factory = env
    http.modelsdev = None
    assert collect_prices(ConfigStore(config_path), session_factory, now=NOON) == {"lumen": 3, "models.dev": 0, "hardware": 2}


def test_lumen_failure_raises_and_writes_nothing(env, http):
    config_path, session_factory = env
    http.lumen_status = 503
    with pytest.raises(requests.HTTPError):
        collect_prices(ConfigStore(config_path), session_factory, now=NOON)
    assert rows(session_factory) == {}


def test_cli_collect_price(env, http):
    config_path, session_factory = env
    assert main(["--config", str(config_path), "collect", "--price"]) == 0
    assert rows(session_factory, "lumen")


def test_cli_collect_price_lumen_down(env, http):
    config_path, _ = env
    http.lumen_status = 500
    assert main(["--config", str(config_path), "collect", "--price"]) == 1


def test_cli_collect_needs_a_collector(env):
    config_path, _ = env
    with pytest.raises(SystemExit):
        main(["--config", str(config_path), "collect"])


def test_dotted_lumen_id_uses_the_dashed_key(env, http):
    config_path, session_factory = env
    config_path.write_text(CONFIG + "  deepseek-v4-1-flash: {gpu: rtx6000, gpus: 4}\n")
    http.lumen["data"].append(lumen_model("deepseek-v4.1-flash"))
    assert main(["--config", str(config_path), "collect", "--price"]) == 0
    hw = rows(session_factory, "hardware")["deepseek-v4.1-flash", "hardware"]
    assert (hw.gpus, hw.daily_cost) == (4, pytest.approx(4 * RTX6000_DAY))


def test_quoted_dotted_key_still_works(env, http):
    config_path, session_factory = env
    config_path.write_text(CONFIG + '  "deepseek-v4.1-flash": {gpu: rtx6000, gpus: 4}\n')
    http.lumen["data"].append(lumen_model("deepseek-v4.1-flash"))
    collect_prices(ConfigStore(config_path), session_factory, now=NOON)
    assert ("deepseek-v4.1-flash", "hardware") in rows(session_factory)


def test_colliding_lumen_ids_get_no_hardware_rows(env, http, caplog):
    config_path, session_factory = env
    config_path.write_text(CONFIG + "  deepseek-v4-1-flash: {gpu: rtx6000, gpus: 4}\n")
    http.lumen["data"] += [lumen_model("deepseek-v4.1-flash"), lumen_model("DeepSeek-V4.1-Flash")]
    collect_prices(ConfigStore(config_path), session_factory, now=NOON)
    stored = rows(session_factory, "hardware")
    assert not any(model.lower() == "deepseek-v4.1-flash" for model, _ in stored)
    assert ("qwen", "hardware") in stored
    assert "share the key" in caplog.text


def test_local_day_rejects_aware_datetimes():
    with pytest.raises(ValueError):
        local_day(datetime(2026, 10, 17, tzinfo=timezone.utc), ZoneInfo("America/Chicago"))


def test_model_reached_through_lumen_id_gets_hardware_and_modelsdev_pin(env, http):
    config_path, session_factory = env
    config_path.write_text(CONFIG + "  deepseek-v4-flash-0731: {gpu: rtx6000, gpus: 4, lumen_id: deepseek-v4-flash, modelsdev_id: glm-4.5-air}\n")
    http.lumen["data"].append(lumen_model("deepseek-v4-flash"))
    collect_prices(ConfigStore(config_path), session_factory, now=NOON)
    stored = rows(session_factory)
    hw = stored["deepseek-v4-flash", "hardware"]
    assert (hw.gpus, hw.daily_cost) == (4, pytest.approx(4 * RTX6000_DAY))
    assert stored["deepseek-v4-flash", "models.dev"].modelsdev_id == "glm-4.5-air"
    assert not any(model == "deepseek-v4-flash-0731" for model, _ in stored)


@pytest.mark.parametrize("pin", ["missing-model", "qwen-alias"])
def test_unmatched_lumen_id_warns_and_writes_no_hardware_row(env, http, caplog, pin):
    config_path, session_factory = env
    config_path.write_text(CONFIG + f"  deploy: {{gpu: rtx6000, gpus: 4, lumen_id: {pin}}}\n")
    collect_prices(ConfigStore(config_path), session_factory, now=NOON)
    stored = rows(session_factory, "hardware")
    assert set(stored) == {("qwen", "hardware"), ("big", "hardware")}
    assert f"lumen_id '{pin}' matches no Lumen model" in caplog.text
