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


def test_find_skips_cross_kind_match():
    """A speech model must not fuzzy-match a text model that merely shares family+version."""
    models = [
        {"id": "ibm-granite/granite-4.0-tiny", "name": "Granite 4.0 Tiny", "modalities": {"input": ["text"]}},
    ]
    assert model_sync.find_in_modelsdev("ibm-granite/granite-speech-4.1-2b-plus", models,
                                        config_name="granite-speech-4.1-2b-plus") is None


def test_find_allows_same_kind_match():
    """Same task/modality token still matches."""
    models = [
        {"id": "qwen/qwen2-audio-instruct", "name": "Qwen2 Audio", "modalities": {"input": ["text", "audio"]}},
    ]
    assert model_sync.find_in_modelsdev("qwen2-audio", models, config_name="qwen2-audio") is not None


def test_modalities_overridden_on_match(monkeypatch):
    """A trusted models.dev match corrects modalities (e.g. text -> text+image)."""
    _patch(monkeypatch,
           ep_model=None,
           dev_match={"modalities": {"input": ["text", "image"], "output": ["text"]}})
    result = model_sync.sync_model({
        "name": "m",
        "input_modalities": ["text"],
        "endpoints": [{"url": "http://x"}],
    })
    assert result["updates"]["input_modalities"] == ["text", "image"]
    assert result["updates"]["output_modalities"] == ["text"]


def test_modalities_filled_when_missing(monkeypatch):
    """When modalities are unset, models.dev still populates them."""
    _patch(monkeypatch,
           ep_model=None,
           dev_match={"modalities": {"input": ["text", "image"], "output": ["text"]}})
    result = model_sync.sync_model({"name": "m", "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["input_modalities"] == ["text", "image"]
    assert result["updates"]["output_modalities"] == ["text"]


def test_modalities_untouched_without_match(monkeypatch):
    """No models.dev match (e.g. granite-speech) → operator-set modalities are preserved."""
    _patch(monkeypatch, ep_model={"id": "m", "max_model_len": 4096}, dev_match=None)
    result = model_sync.sync_model({
        "name": "granite-speech",
        "input_modalities": ["text", "audio"],
        "endpoints": [{"url": "http://x"}],
    })
    assert "input_modalities" not in result["updates"]
    assert "output_modalities" not in result["updates"]


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
