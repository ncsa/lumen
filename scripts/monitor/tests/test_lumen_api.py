import pytest
import requests

from monitor import lumen_api


def model_row(model_id, **extra):
    row = {
        "id": model_id, "object": "model", "root": f"org/{model_id}", "parent": None,
        "status": "ok", "input_cost_per_million": 0.2, "output_cost_per_million": 0.8,
        "instances": {"configured": 2, "healthy": 1},
        "requests": {"last_minute": 1, "last_hour": 60, "last_day": 900},
    }
    row.update(extra)
    return row


def test_aliases_are_dropped_and_prices_captured():
    payload = {"data": [model_row("qwen"), model_row("coder", parent="qwen"), {"id": ""}, "junk"]}
    [model] = lumen_api.parse_models(payload)
    assert model["id"] == "qwen"
    assert model["root"] == "org/qwen"
    assert (model["input_price"], model["output_price"]) == (0.2, 0.8)
    assert (model["configured"], model["healthy"], model["status"]) == (2, 1, "ok")


def test_tokens_and_last_month_are_none_when_missing():
    [model] = lumen_api.parse_models({"data": [model_row("qwen")]})
    assert model["tokens"] is None
    assert model["input_tokens"] is None and model["output_tokens"] is None
    assert model["requests"]["last_month"] is None
    assert model["requests"]["last_day"] == 900


def test_tokens_and_last_month_are_read_when_present():
    row = model_row(
        "qwen",
        requests={"last_minute": 1, "last_hour": 60, "last_day": 900, "last_month": 27000},
        tokens={"last_minute": 10, "last_hour": 600, "last_day": 9000, "last_month": 270000},
        description="A model",
    )
    [model] = lumen_api.parse_models({"data": [row]})
    assert model["requests"]["last_month"] == 27000
    assert model["tokens"] == {"last_minute": 10, "last_hour": 600, "last_day": 9000, "last_month": 270000}
    assert model["description"] == "A model"


def test_input_and_output_tokens_are_read_when_present():
    row = model_row(
        "qwen",
        tokens={"last_minute": 12, "last_hour": 720, "last_day": 9000, "last_month": 270000},
        input_tokens={"last_minute": 10, "last_hour": 600, "last_day": 7000, "last_month": 200000},
        output_tokens={"last_minute": 2, "last_hour": 120, "last_day": 2000},
    )
    [model] = lumen_api.parse_models({"data": [row]})
    assert model["input_tokens"] == {"last_minute": 10, "last_hour": 600, "last_day": 7000, "last_month": 200000}
    assert model["output_tokens"] == {"last_minute": 2, "last_hour": 120, "last_day": 2000, "last_month": None}
    assert model["tokens"]["last_day"] == 9000


def test_rows_with_and_without_the_split_parse_together():
    split = model_row("qwen", input_tokens={"last_minute": 10}, output_tokens={"last_minute": 2})
    plain = model_row("big", tokens={"last_minute": 5})
    by_id = {m["id"]: m for m in lumen_api.parse_models({"data": [split, plain]})}
    assert by_id["qwen"]["input_tokens"]["last_minute"] == 10
    assert by_id["big"]["input_tokens"] is None and by_id["big"]["output_tokens"] is None
    assert by_id["big"]["tokens"]["last_minute"] == 5


def test_not_an_object_is_an_error():
    with pytest.raises(ValueError):
        lumen_api.parse_models([])


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status}")

    def json(self):
        return self.payload


def test_fetch_sends_the_token_and_asks_for_token_counts(monkeypatch):
    seen = {}

    def fake_get(url, params, headers, timeout):
        seen.update(url=url, params=params, headers=headers, timeout=timeout)
        return Response({"data": [model_row("qwen")]})

    monkeypatch.setattr(requests, "get", fake_get)
    assert [m["id"] for m in lumen_api.fetch_models("https://lumen.test", "secret", 15)] == ["qwen"]
    assert seen == {"url": "https://lumen.test/v1/models", "params": {"tokens": "true"}, "headers": {"Authorization": "Bearer secret"}, "timeout": 15}


def test_fetch_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response({}, status=503))
    with pytest.raises(requests.HTTPError):
        lumen_api.fetch_models("https://lumen.test", "", 15)
