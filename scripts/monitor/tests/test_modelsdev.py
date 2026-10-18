import pytest
import requests

from monitor import modelsdev


def listing(model_id, input_cost, output_cost, name=""):
    return {"id": model_id, "name": name or model_id, "cost": {"input": input_cost, "output": output_cost}}


def stats_for(listings, model_id="glm-4.6", root=None, pin=None):
    dev_match = modelsdev.match(model_id, root, listings, pin=pin)
    return modelsdev.provider_stats(dev_match, modelsdev.build_price_index(listings)) if dev_match else None


def test_price_range_one_value():
    assert modelsdev.price_range([0.5]) == (0.5, 0.5, 0.5)


def test_price_range_two_values():
    median, p5, p95 = modelsdev.price_range([1.0, 3.0])
    assert median == 2.0
    assert p5 == pytest.approx(1.1)
    assert p95 == pytest.approx(2.9)


def test_price_range_many_values():
    median, p5, p95 = modelsdev.price_range([float(v) for v in range(1, 22)])  # 1..21
    assert (median, p5, p95) == (11.0, 2.0, 20.0)


def test_price_range_empty():
    assert modelsdev.price_range([]) is None


def test_zero_listings_are_dropped():
    listings = [
        listing("glm-4.6", 0, 0),
        listing("glm-4.6", 0.6, 2.2),
        listing("glm-4.6", None, 0),
        listing("GLM-4.6", 0.4, 0),  # output price $0: only the input counts
    ]
    stats = stats_for(listings)
    assert stats["providers"] == 2
    assert stats["input_median"] == pytest.approx(0.5)
    assert (stats["output_median"], stats["output_p5"], stats["output_p95"]) == (2.2, 2.2, 2.2)


def test_only_zero_listings_give_no_stats():
    assert stats_for([listing("glm-4.6", 0, 0), listing("glm-4.6", "0", None)]) is None


def test_one_provider():
    stats = stats_for([listing("glm-4.6", 0.6, 2.2)])
    assert stats == {
        "modelsdev_id": "glm-4.6", "providers": 1,
        "input_median": 0.6, "input_p5": 0.6, "input_p95": 0.6,
        "output_median": 2.2, "output_p5": 2.2, "output_p95": 2.2,
    }


def test_many_providers():
    listings = [listing("glm-4.6", float(v), float(v) * 2) for v in range(1, 22)]
    stats = stats_for(listings)
    assert stats["providers"] == 21
    assert (stats["input_median"], stats["input_p5"], stats["input_p95"]) == (11.0, 2.0, 20.0)
    assert (stats["output_median"], stats["output_p5"], stats["output_p95"]) == (22.0, 4.0, 40.0)


def test_other_models_are_not_counted():
    listings = [listing("glm-4.6", 1.0, 2.0), listing("glm-4.5", 9.0, 9.0)]
    assert stats_for(listings)["providers"] == 1


def test_fuzzy_match_on_root():
    listings = [listing("qwen3-coder-30b-a3b-instruct", 0.1, 0.4, name="Qwen3 Coder 30B")]
    stats = stats_for(listings, model_id="coder", root="Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8")
    assert stats["modelsdev_id"] == "qwen3-coder-30b-a3b-instruct"


def test_pin_overrides_the_match():
    listings = [listing("glm-4.6", 1.0, 2.0), listing("glm-4.5-air", 0.2, 1.1)]
    assert stats_for(listings, pin="zai/glm-4.5-air")["modelsdev_id"] == "glm-4.5-air"


def test_pin_without_listing_does_not_fall_back():
    assert stats_for([listing("glm-4.6", 1.0, 2.0)], pin="does-not-exist") is None


def test_fetch_flattens_providers(monkeypatch):
    payload = {
        "zai": {"models": {"glm-4.6": listing("glm-4.6", 0.6, 2.2)}},
        "other": {"models": {"glm-4.6": listing("glm-4.6", 0.5, 2.0), "bad": "x"}},
        "junk": "x",
    }

    class Response:
        def json(self):
            return payload

    seen = {}

    def fake_get(url, timeout):
        seen.update(url=url, timeout=timeout)
        return Response()

    monkeypatch.setattr(requests, "get", fake_get)
    assert len(modelsdev.fetch_modelsdev(15)) == 2
    assert seen == {"url": modelsdev.MODELSDEV_URL, "timeout": 15}


def test_fetch_failure_is_empty(monkeypatch):
    def fail(*_args, **_kwargs):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "get", fail)
    assert modelsdev.fetch_modelsdev(15) == []
