"""Tests for field precedence in lumen/services/model_sync.sync_model."""
from lumen.services import model_sync


def _patch(monkeypatch, ep_model, dev_match):
    monkeypatch.setattr(model_sync, "fetch_endpoint_model", lambda ep: ep_model)
    monkeypatch.setattr(model_sync, "get_modelsdev", lambda: [dev_match] if dev_match else [])
    monkeypatch.setattr(model_sync, "find_in_modelsdev", lambda *a, **k: dev_match)


def test_context_window_comes_from_endpoint_not_dev(monkeypatch):
    """The endpoint's max_model_len wins over models.dev limit.context."""
    _patch(monkeypatch,
           ep_model={"id": "m", "max_model_len": 48712},
           dev_match={"limit": {"context": 200000, "output": 64000}})
    result = model_sync.sync_model({"name": "m", "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["context_window"] == 48712
    # max_output_tokens comes from models.dev, never from the endpoint.
    assert result["updates"]["max_output_tokens"] == 64000


def test_max_output_tokens_comes_from_dev(monkeypatch):
    _patch(monkeypatch,
           ep_model={"id": "m", "max_model_len": 48712},
           dev_match={"limit": {"output": 16384}})
    result = model_sync.sync_model({"name": "m", "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["max_output_tokens"] == 16384


def test_no_dev_match_leaves_max_output_untouched(monkeypatch):
    """With no models.dev match, max_output_tokens is not proposed (not nulled)."""
    _patch(monkeypatch,
           ep_model={"id": "m", "max_model_len": 48712},
           dev_match=None)
    result = model_sync.sync_model(
        {"name": "m", "max_output_tokens": 4096, "endpoints": [{"url": "http://x"}]})
    assert "max_output_tokens" not in result["updates"]
    assert result["updates"]["context_window"] == 48712


def test_context_window_falls_back_to_dev_without_endpoint(monkeypatch):
    """No endpoint max_model_len (e.g. hosted passthrough) → use limit.context."""
    _patch(monkeypatch,
           ep_model=None,
           dev_match={"limit": {"context": 200000, "output": 64000}})
    result = model_sync.sync_model({"name": "m", "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["context_window"] == 200000
    assert result["updates"]["max_output_tokens"] == 64000


def test_no_change_when_values_already_match(monkeypatch):
    _patch(monkeypatch,
           ep_model={"id": "m", "max_model_len": 48712},
           dev_match={"limit": {"context": 200000, "output": 64000}})
    result = model_sync.sync_model({
        "name": "m",
        "context_window": 48712,
        "max_output_tokens": 64000,
        "endpoints": [{"url": "http://x"}],
    })
    assert "context_window" not in result["updates"]
    assert "max_output_tokens" not in result["updates"]
