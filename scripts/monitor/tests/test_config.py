import logging
from datetime import timedelta
from pathlib import Path

import pytest

from monitor.config import ConfigError, ConfigStore, Gpu, load_config, model_key, normalize_base_path, parse_config, parse_duration

EXAMPLE = Path(__file__).resolve().parent.parent / "config.yaml.example"


def test_defaults():
    config = parse_config({}, env={})
    assert config.database_url == "sqlite:///monitor.db"
    assert config.base_path == ""
    assert str(config.timezone) == "UTC"
    assert config.retention == timedelta(days=31)
    assert config.refresh.web == timedelta(minutes=1)
    assert config.refresh.models == timedelta(minutes=5)
    assert config.refresh.price == timedelta(hours=1)
    assert config.refresh.page == timedelta(minutes=5)
    assert config.refresh.timeout == timedelta(seconds=15)
    assert config.lumen_url == ""
    assert config.lumen_api_key == ""
    assert config.kwh is None
    assert config.gpus == {}
    assert config.models == {}
    assert config.down_above == 50
    assert config.warnings == []


def test_missing_file_uses_defaults(tmp_path):
    assert load_config(tmp_path / "nope.yaml", env={}) == parse_config({}, env={})


def test_example_loads_without_warnings():
    config = load_config(EXAMPLE, env={})
    assert config.warnings == []
    assert str(config.timezone) == "America/Chicago"
    assert config.lumen_url == "https://lumen.example.edu"
    assert config.kwh == 0.24
    assert config.hardware_for("nemotron-3-super-120b-a12b-fp8") == (Gpu(energy=600, price=625), 2)
    assert config.hardware_for("deepseek-v4.1-flash") == (Gpu(energy=600, price=625), 4)


@pytest.mark.parametrize(("value", "expected"), [
    ("15s", timedelta(seconds=15)),
    ("5m", timedelta(minutes=5)),
    ("1h", timedelta(hours=1)),
    ("31d", timedelta(days=31)),
    ("1.5h", timedelta(minutes=90)),
    (30, timedelta(seconds=30)),
    ("30", timedelta(seconds=30)),
])
def test_parse_duration(value, expected):
    assert parse_duration(value, "x") == expected


@pytest.mark.parametrize("value", ["5 minutes", "", "-1m", "0s", True, None])
def test_parse_duration_rejects(value):
    with pytest.raises(ConfigError):
        parse_duration(value, "x")


@pytest.mark.parametrize("refresh", [
    {"timeout": "1m"},                           # timeout == web
    {"timeout": "2m"},                           # timeout > web
    {"web": "10m"},                              # models < web
    {"models": "30s"},                           # models < web
    {"models": "2h", "price": "3h"},             # models > 1h
    {"web": "5m", "models": "5m", "price": "1m"},  # price < web
])
def test_refresh_out_of_order_rejected(refresh):
    with pytest.raises(ConfigError, match="refresh"):
        parse_config({"refresh": refresh}, env={})


def test_refresh_boundaries_accepted():
    config = parse_config({"refresh": {"timeout": "59s", "web": "1m", "models": "1h", "price": "1h"}}, env={})
    assert config.refresh.models == timedelta(hours=1)


def test_refresh_unknown_key_rejected():
    with pytest.raises(ConfigError, match="health"):
        parse_config({"refresh": {"health": "1m"}}, env={})


def test_short_retention_warns():
    assert any("retention" in w for w in parse_config({"retention": "7d"}, env={}).warnings)
    assert parse_config({"retention": "30d"}, env={}).warnings == []


@pytest.mark.parametrize(("value", "expected"), [
    (None, ""),
    ("", ""),
    ("/", ""),
    ("monitor", "/monitor"),
    ("/monitor/", "/monitor"),
    ("  monitor/  ", "/monitor"),
    ("/a/b/", "/a/b"),
])
def test_base_path_normalized(value, expected):
    assert normalize_base_path(value) == expected
    assert parse_config({"base_path": value}, env={}).base_path == expected


def test_unknown_timezone_rejected():
    with pytest.raises(ConfigError, match="timezone"):
        parse_config({"timezone": "Mars/Olympus"}, env={})


def test_unknown_gpu_type_reported():
    config = parse_config({"price": {"kwh": 0.24, "rtx6000": {"energy": 600, "price": 625}}, "models": {"m": {"gpu": "h100", "gpus": 1}}}, env={})
    assert any("unknown GPU type 'h100'" in w for w in config.warnings)
    assert config.hardware_for("m") is None


def test_missing_kwh_reported():
    config = parse_config({"price": {"rtx6000": {"energy": 600, "price": 625}}, "models": {"m": {"gpu": "rtx6000", "gpus": 1}}}, env={})
    assert any("kwh" in w for w in config.warnings)
    assert config.hardware_for("m") is None


@pytest.mark.parametrize(("entry", "fragment"), [
    ({"gpu": "rtx6000"}, "gpus is missing"),
    ({"gpus": 1}, "gpu is missing"),
    ({"gpu": "rtx6000", "gpus": 0}, "positive integer"),
    ({"gpu": "rtx6000", "gpus": "two"}, "positive integer"),
])
def test_incomplete_model_reported(entry, fragment):
    config = parse_config({"price": {"kwh": 0.24, "rtx6000": {"energy": 600, "price": 625}}, "models": {"m": entry}}, env={})
    assert any(fragment in w for w in config.warnings)
    assert config.hardware_for("m") is None


def test_bad_gpu_price_reported():
    config = parse_config({"price": {"kwh": 0.24, "rtx6000": {"energy": "lots", "price": 625}}}, env={})
    assert any("price.rtx6000.energy" in w for w in config.warnings)
    assert "rtx6000" not in config.gpus


def test_model_without_entry_has_no_hardware():
    config = parse_config({"price": {"kwh": 0.24, "rtx6000": {"energy": 600, "price": 625}}}, env={})
    assert config.hardware_for("unlisted") is None


def test_modelsdev_id_kept():
    config = parse_config({"models": {"m": {"gpu": "x", "gpus": 1, "modelsdev_id": "p/m"}}}, env={})
    assert config.models["m"].modelsdev_id == "p/m"


def test_env_overrides():
    raw = {"database_url": "sqlite:///file.db", "base_path": "/file", "lumen": {"url": "https://l.example/", "api_key": "file-key"}}
    env = {"MONITOR_DATABASE_URL": "postgresql://u@h/db", "MONITOR_BASE_PATH": "env/", "MONITOR_LUMEN_API_KEY": "env-key"}
    config = parse_config(raw, env=env)
    assert config.database_url == "postgresql://u@h/db"
    assert config.base_path == "/env"
    assert config.lumen_api_key == "env-key"
    assert config.lumen_url == "https://l.example"


def test_env_base_path_empty_overrides_file():
    assert parse_config({"base_path": "/file"}, env={"MONITOR_BASE_PATH": ""}).base_path == ""


def test_warnings_are_logged(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("retention: 1d\n")
    with caplog.at_level(logging.WARNING, logger="monitor.config"):
        load_config(path, env={})
    assert "retention" in caplog.text


def test_invalid_yaml_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("refresh: [\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path, env={})


def test_reload_picks_up_changes(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("refresh: {price: 1h}\n")
    store = ConfigStore(path, env={})
    path.write_text("refresh: {price: 2h}\nprice: {kwh: 0.3}\n")
    assert store.reload() is True
    assert store.current.refresh.price == timedelta(hours=2)
    assert store.current.kwh == 0.3


def test_reload_keeps_previous_config_on_error(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("refresh: {price: 1h}\n")
    store = ConfigStore(path, env={})
    before = store.current
    path.write_text("refresh: {timeout: 5m}\n")
    with caplog.at_level(logging.ERROR, logger="monitor.config"):
        assert store.reload() is False
    assert store.current is before
    assert "reload failed" in caplog.text


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), 10**400, "99999999999999d", "3651d"])
def test_parse_duration_rejects_non_finite_and_out_of_range(value):
    with pytest.raises(ConfigError, match="finite duration"):
        parse_duration(value, "x")


def test_parse_duration_accepts_upper_bound():
    assert parse_duration("3650d", "x") == timedelta(days=3650)


@pytest.mark.parametrize("value", [".inf", ".nan", "-.inf", "1e999", "99999999999999d"])
def test_non_finite_refresh_rejected_from_yaml(tmp_path, value):
    path = tmp_path / "config.yaml"
    path.write_text(f"refresh: {{price: {value}}}\n")
    with pytest.raises(ConfigError):
        load_config(path, env={})


@pytest.mark.parametrize("text", [
    "refresh: {price: .inf}\n",
    "refresh: {price: .nan}\n",
    "retention: 99999999999999d\n",
    "retention: .inf\n",
])
def test_reload_rejects_non_finite_and_keeps_previous(tmp_path, caplog, text):
    path = tmp_path / "config.yaml"
    path.write_text("refresh: {price: 1h}\n")
    store = ConfigStore(path, env={})
    before = store.current
    path.write_text(text)
    with caplog.at_level(logging.ERROR, logger="monitor.config"):
        assert store.reload() is False
    assert store.current is before
    assert "reload failed" in caplog.text


@pytest.mark.parametrize("value", [".inf", ".nan", "-.inf", "1e999"])
def test_non_finite_kwh_reported_and_skipped(tmp_path, value):
    path = tmp_path / "config.yaml"
    path.write_text(f"price: {{kwh: {value}, rtx6000: {{energy: 600, price: 625}}}}\nmodels: {{m: {{gpu: rtx6000, gpus: 1}}}}\n")
    config = load_config(path, env={})
    assert config.kwh is None
    assert any("price.kwh" in w for w in config.warnings)
    assert config.hardware_for("m") is None


@pytest.mark.parametrize("field", ["energy", "price"])
@pytest.mark.parametrize("value", [float("inf"), float("nan"), 10**400])
def test_non_finite_gpu_inputs_reported_and_skipped(field, value):
    entry = {"energy": 600, "price": 625, field: value}
    config = parse_config({"price": {"kwh": 0.24, "rtx6000": entry}, "models": {"m": {"gpu": "rtx6000", "gpus": 1}}}, env={})
    assert any(f"price.rtx6000.{field}" in w for w in config.warnings)
    assert "rtx6000" not in config.gpus
    assert config.hardware_for("m") is None


def test_null_values_use_defaults():
    config = parse_config({"retention": None, "timezone": None, "refresh": {"web": None, "price": None}}, env={})
    assert config == parse_config({}, env={})


def test_zero_retention_still_rejected():
    with pytest.raises(ConfigError, match="retention"):
        parse_config({"retention": 0}, env={})


def test_unknown_top_level_key_warns():
    config = parse_config({"refreshh": {"web": "1m"}}, env={})
    assert any("refreshh" in w for w in config.warnings)


PRICE = {"kwh": 0.24, "rtx6000": {"energy": 600, "price": 625}}


def test_model_key():
    assert model_key(" DeepSeek-V4.1-Flash ") == "deepseek-v4-1-flash"


def test_keys_with_the_same_model_key_are_rejected():
    models = {"a.1": {"gpu": "rtx6000", "gpus": 1}, "a-1": {"gpu": "rtx6000", "gpus": 2}}
    with pytest.raises(ConfigError, match="same model"):
        parse_config({"price": PRICE, "models": models}, env={})


@pytest.mark.parametrize("yaml_key", ["deepseek-v4-1-flash", '"deepseek-v4.1-flash"'])
def test_dashed_and_quoted_dotted_keys_load(tmp_path, yaml_key):
    path = tmp_path / "config.yaml"
    path.write_text(f"price: {{kwh: 0.24, rtx6000: {{energy: 600, price: 625}}}}\nmodels:\n  {yaml_key}: {{gpu: rtx6000, gpus: 2}}\n")
    config = load_config(path, env={})
    assert config.warnings == []
    assert config.hardware_for("deepseek-v4.1-flash") == (Gpu(energy=600, price=625), 2)


def test_exact_key_wins():
    models = {"m.1": {"gpu": "rtx6000", "gpus": 1}, "m-1-x": {"gpu": "rtx6000", "gpus": 3}}
    config = parse_config({"price": PRICE, "models": models}, env={})
    assert config.model_entry("m.1").gpus == 1
    assert config.model_entry("M.1-X").gpus == 3


def test_colliding_lumen_ids_get_no_entry(caplog):
    config = parse_config({"price": PRICE, "models": {"a-1": {"gpu": "rtx6000", "gpus": 1}}}, env={})
    with caplog.at_level(logging.WARNING):
        assert config.match_models(["a.1", "A.1", "b"]) == {}
    assert [r.getMessage() for r in caplog.records if "share the key" in r.getMessage()] == [
        "models: Lumen ids A.1, a.1 share the key 'a-1'; no hardware cost for A.1, a.1"
    ]


def test_repeated_lumen_id_is_not_a_collision():
    config = parse_config({"price": PRICE, "models": {"a-1": {"gpu": "rtx6000", "gpus": 1}}}, env={})
    assert list(config.match_models(["a.1", "a.1"])) == ["a.1"]


def test_exact_lumen_id_keeps_its_entry_in_a_collision():
    config = parse_config({"price": PRICE, "models": {"a-1": {"gpu": "rtx6000", "gpus": 1}}}, env={})
    assert list(config.match_models(["a-1", "a.1"])) == ["a-1"]


PINNED = {"deepseek-v4-flash-0731": {"gpu": "rtx6000", "gpus": 4, "lumen_id": "deepseek-v4-flash"}}


def test_lumen_id_kept():
    config = parse_config({"price": PRICE, "models": PINNED}, env={})
    assert config.models["deepseek-v4-flash-0731"].lumen_id == "deepseek-v4-flash"
    assert config.warnings == []


@pytest.mark.parametrize("value", ["", "  ", 5, True, ["a"]])
def test_bad_lumen_id_warns_and_is_ignored(value):
    config = parse_config({"price": PRICE, "models": {"m": {"gpu": "rtx6000", "gpus": 1, "lumen_id": value}}}, env={})
    assert any("models.m: lumen_id" in w for w in config.warnings)
    assert config.models["m"].lumen_id is None
    assert config.model_entry("m").gpus == 1


def test_duplicate_lumen_id_rejected():
    models = {"a": {"gpu": "rtx6000", "gpus": 1, "lumen_id": "x"}, "b": {"gpu": "rtx6000", "gpus": 2, "lumen_id": "x"}}
    with pytest.raises(ConfigError, match="same lumen_id"):
        parse_config({"price": PRICE, "models": models}, env={})


@pytest.mark.parametrize("other_key", ["deepseek-v4.1-flash", "deepseek-v4-1-flash", "DeepSeek-V4.1-Flash"])
def test_lumen_id_colliding_with_another_key_rejected(other_key):
    models = {"deploy": {"gpu": "rtx6000", "gpus": 4, "lumen_id": "deepseek-v4.1-flash"}, other_key: {"gpu": "rtx6000", "gpus": 1}}
    with pytest.raises(ConfigError, match="same model"):
        parse_config({"price": PRICE, "models": models}, env={})


def test_lumen_id_equal_to_its_own_key_is_fine():
    config = parse_config({"price": PRICE, "models": {"m": {"gpu": "rtx6000", "gpus": 1, "lumen_id": "m"}}}, env={})
    assert config.model_entry("m").gpus == 1


def test_pinned_entry_matches_only_its_lumen_id():
    config = parse_config({"price": PRICE, "models": PINNED}, env={})
    assert config.model_entry("deepseek-v4-flash").gpus == 4
    assert config.model_entry("deepseek-v4-flash-0731") is None
    assert config.model_entry("DeepSeek-V4-Flash") is None  # exact, no model_key folding
    assert config.hardware_for("deepseek-v4-flash") == (Gpu(energy=600, price=625), 4)


def test_pin_is_exact_without_dot_dash_folding():
    models = {"deploy": {"gpu": "rtx6000", "gpus": 4, "lumen_id": "deepseek-v4.1-flash"}}
    config = parse_config({"price": PRICE, "models": models}, env={})
    assert config.model_entry("deepseek-v4.1-flash").gpus == 4
    assert config.model_entry("deepseek-v4-1-flash") is None


def test_unpinned_entries_match_as_before_next_to_a_pin():
    models = {**PINNED, "qwen-3": {"gpu": "rtx6000", "gpus": 1}}
    config = parse_config({"price": PRICE, "models": models}, env={})
    assert config.model_entry("qwen.3").gpus == 1
    assert config.match_models(["deepseek-v4-flash", "qwen.3", "other"]) == {
        "deepseek-v4-flash": config.models["deepseek-v4-flash-0731"],
        "qwen.3": config.models["qwen-3"],
    }


def test_unmatched_pin_warns(caplog):
    config = parse_config({"price": PRICE, "models": PINNED}, env={})
    with caplog.at_level(logging.WARNING, logger="monitor.config"):
        assert config.match_models(["deepseek-v4-flash-0731", "other"]) == {}
    assert "lumen_id 'deepseek-v4-flash' matches no Lumen model" in caplog.text


def test_matched_pin_does_not_warn(caplog):
    config = parse_config({"price": PRICE, "models": PINNED}, env={})
    with caplog.at_level(logging.WARNING, logger="monitor.config"):
        config.match_models(["deepseek-v4-flash"])
    assert caplog.text == ""


def test_down_above_parsed():
    config = parse_config({"lumen": {"down_above": 30}}, env={})
    assert config.down_above == 30
    assert config.warnings == []


@pytest.mark.parametrize("value", [-1, 101, "abc", True, float("nan")])
def test_bad_down_above_warns_and_uses_default(value):
    config = parse_config({"lumen": {"down_above": value}}, env={})
    assert config.down_above == 50
    assert any("lumen.down_above" in w for w in config.warnings)


def test_reload_picks_up_down_above(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("lumen: {url: https://lumen.test}\n")
    store = ConfigStore(path, env={})
    assert store.current.down_above == 50
    path.write_text("lumen: {url: https://lumen.test, down_above: 30}\n")
    assert store.reload() is True
    assert store.current.down_above == 30
