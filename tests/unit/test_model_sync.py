"""Tests for field precedence in lumen/services/model_sync.sync_model."""
from lumen.services import model_sync


def _patch(monkeypatch, ep_model, dev_match):
    monkeypatch.setattr(model_sync, "fetch_endpoint_model", lambda ep: ep_model)
    monkeypatch.setattr(model_sync, "get_modelsdev", lambda: ([dev_match] if dev_match else [], {}))
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


def _patch_price(monkeypatch, dev_models, dev_match):
    monkeypatch.setattr(model_sync, "fetch_endpoint_model", lambda ep: None)
    monkeypatch.setattr(model_sync, "get_modelsdev", lambda: (dev_models, model_sync._build_price_index(dev_models)))
    monkeypatch.setattr(model_sync, "find_in_modelsdev", lambda *a, **k: dev_match)


def test_price_averaged_across_providers(monkeypatch):
    """Pricing is the mean of every provider's >0 cost for the same base model."""
    dev_models = [
        {"id": "openai/gpt-4o", "cost": {"input": 2.5, "output": 10}},
        {"id": "azure/gpt-4o", "cost": {"input": 3.5, "output": 12}},
        {"id": "github/gpt-4o", "cost": {"input": 0, "output": 12}},  # 0 input excluded
    ]
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({"name": "gpt-4o", "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["input_cost_per_million"] == 3.0          # mean(2.5, 3.5)
    assert result["updates"]["output_cost_per_million"] == round(34 / 3, 6)  # mean(10, 12, 12)


def test_price_zero_listing_excluded(monkeypatch):
    """A provider listing $0 is dropped from the average, not treated as free."""
    dev_models = [
        {"id": "p/llama-3.3-70b", "cost": {"input": 0, "output": 0}},
        {"id": "q/llama-3.3-70b", "cost": {"input": 1.2, "output": 1.8}},
    ]
    _patch_price(monkeypatch, dev_models, dev_models[1])
    result = model_sync.sync_model({"name": "llama-3.3-70b", "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["input_cost_per_million"] == 1.2
    assert result["updates"]["output_cost_per_million"] == 1.8


def test_no_price_update_when_all_zero(monkeypatch):
    """If every provider lists $0, no cost update is proposed (never 0)."""
    dev_models = [{"id": "p/m", "cost": {"input": 0, "output": 0}}]
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({"name": "m", "endpoints": [{"url": "http://x"}]})
    assert "input_cost_per_million" not in result["updates"]
    assert "output_cost_per_million" not in result["updates"]


def test_price_overwrites_stale_operator_value(monkeypatch):
    """A trusted match corrects a stale/zero operator-set price."""
    dev_models = [{"id": "p/m", "cost": {"input": 5.0, "output": 15.0}}]
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({
        "name": "m",
        "input_cost_per_million": 0,
        "output_cost_per_million": 0,
        "endpoints": [{"url": "http://x"}],
    })
    assert result["updates"]["input_cost_per_million"] == 5.0
    assert result["updates"]["output_cost_per_million"] == 15.0


def test_price_untouched_without_dev_match(monkeypatch):
    """No models.dev match → operator-set pricing is preserved."""
    _patch(monkeypatch, ep_model={"id": "m", "max_model_len": 4096}, dev_match=None)
    result = model_sync.sync_model({
        "name": "m",
        "input_cost_per_million": 0,
        "output_cost_per_million": 0,
        "endpoints": [{"url": "http://x"}],
    })
    assert "input_cost_per_million" not in result["updates"]
    assert "output_cost_per_million" not in result["updates"]


# ── fetch_endpoint_model: SGLang /get_server_info first ────────────────────────

class _Resp:
    def __init__(self, payload, ok=True):
        self._p = payload
        self.ok = ok
    def json(self):
        if not self.ok:
            raise ValueError("http error")
        return self._p


def test_sglang_server_info_preferred_over_v1_models(monkeypatch):
    """/get_server_info is called first; when it 200s, /v1/models is never hit.
    SGLang's max_req_input_len wins over /v1/models' theoretical max_model_len."""
    calls = []
    def fake_get(url, **kw):
        calls.append(url)
        if url.endswith("/get_server_info"):
            return _Resp({"max_req_input_len": 500410, "served_model_name": "zai-org/GLM-5.2-FP8",
                          "is_embedding": False, "enable_multimodal": None})
        return _Resp({"data": [{"id": "zai-org/GLM-5.2-FP8", "max_model_len": 1048576}]})
    monkeypatch.setattr(model_sync.requests, "get", fake_get)
    r = model_sync.fetch_endpoint_model({"url": "http://x", "api_key": "k"})
    assert r["max_model_len"] == 500410
    assert r["id"] == "zai-org/GLM-5.2-FP8"
    assert r["backend"] == "sglang"
    assert r["is_embedding"] is False
    assert r["enable_multimodal"] is None
    assert not any(u.endswith("/models") for u in calls), "v1/models must not be called when server_info succeeds"


def test_sglang_server_info_without_max_req_input_len_preserves_flags(monkeypatch):
    """A 200 from /get_server_info without max_req_input_len (e.g. embedding-only
    SGLang) must still carry is_embedding/enable_multimodal through the fallback
    chain — the flags are the point of this PR, they must not be silently dropped."""
    def fake_get(url, **kw):
        if url.endswith("/get_server_info"):
            return _Resp({"served_model_name": "m", "is_embedding": True, "enable_multimodal": None})  # no max_req_input_len
        if url.endswith("/models"):
            return _Resp({"data": [{"id": "m", "max_model_len": 8192}]})
        return _Resp({}, ok=False)
    monkeypatch.setattr(model_sync.requests, "get", fake_get)
    r = model_sync.fetch_endpoint_model({"url": "http://x", "api_key": "k"})
    assert r["backend"] == "sglang"
    assert r["is_embedding"] is True
    assert r["max_model_len"] == 8192  # fell through to /v1/models


def test_sglang_flags_returned_even_without_any_context_length(monkeypatch):
    """If no source yields a context length but /get_server_info 200'd, the
    flags are still returned (max_model_len=None) so the modality override fires."""
    def fake_get(url, **kw):
        if url.endswith("/get_server_info"):
            return _Resp({"is_embedding": False, "enable_multimodal": True})  # no max_req_input_len
        if url.endswith("/models"):
            return _Resp({"data": [{"id": "m"}]})  # no max_model_len
        return _Resp({}, ok=False)  # /get_model_info 404
    monkeypatch.setattr(model_sync.requests, "get", fake_get)
    r = model_sync.fetch_endpoint_model({"url": "http://x", "api_key": "k"})
    assert r["backend"] == "sglang"
    assert r["enable_multimodal"] is True
    assert r["max_model_len"] is None


def test_vllm_used_when_server_info_absent(monkeypatch):
    """vLLM has no /get_server_info (404) → fall back to /v1/models max_model_len."""
    def fake_get(url, **kw):
        if url.endswith("/get_server_info"):
            return _Resp({}, ok=False)  # 404
        if url.endswith("/models"):
            return _Resp({"data": [{"id": "m", "max_model_len": 32768}]})
        return _Resp({}, ok=False)
    monkeypatch.setattr(model_sync.requests, "get", fake_get)
    r = model_sync.fetch_endpoint_model({"url": "http://x", "api_key": "k"})
    assert r["max_model_len"] == 32768
    assert r["id"] == "m"
    assert "backend" not in r  # vLLM path: no sglang flags


def test_get_model_info_fallback(monkeypatch):
    """Older SGLang with neither /get_server_info nor /v1/models max_model_len
    → /get_model_info context_length is used."""
    def fake_get(url, **kw):
        if url.endswith("/get_server_info"):
            return _Resp({}, ok=False)
        if url.endswith("/models"):
            return _Resp({"data": [{"id": "m"}]})  # no max_model_len
        if url.endswith("/get_model_info"):
            return _Resp({"context_length": 8192, "model_path": "m"})
        return _Resp({}, ok=False)
    monkeypatch.setattr(model_sync.requests, "get", fake_get)
    r = model_sync.fetch_endpoint_model({"url": "http://x", "api_key": "k"})
    assert r["max_model_len"] == 8192


# ── server-authoritative modalities ───────────────────────────────────────────

def _patch_ep(monkeypatch, ep_model, dev_match):
    monkeypatch.setattr(model_sync, "fetch_endpoint_model", lambda ep: ep_model)
    monkeypatch.setattr(model_sync, "get_modelsdev", lambda: ([dev_match] if dev_match else [], {}))
    monkeypatch.setattr(model_sync, "find_in_modelsdev", lambda *a, **k: dev_match)


def test_server_embedding_sets_text_in_empty_out(monkeypatch):
    """is_embedding=True from SGLang → input=['text'], output=[] (authoritative)."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 8192, "backend": "sglang", "is_embedding": True, "enable_multimodal": None},
              dev_match={"modalities": {"input": ["text"], "output": ["text"]}})
    result = model_sync.sync_model({"name": "m", "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["input_modalities"] == ["text"]
    assert result["updates"]["output_modalities"] == []


def test_server_multimodal_disabled_strips_image(monkeypatch):
    """enable_multimodal=null on the server vetoes models.dev's image claim."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 32768, "backend": "sglang", "is_embedding": False, "enable_multimodal": None},
              dev_match={"modalities": {"input": ["text", "image"], "output": ["text"]}})
    result = model_sync.sync_model({"name": "m", "input_modalities": ["text", "image"],
                                    "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["input_modalities"] == ["text"]


def test_server_multimodal_enabled_adds_image(monkeypatch):
    """enable_multimodal=True + models.dev says text only → ensure image present."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 32768, "backend": "sglang", "is_embedding": False, "enable_multimodal": True},
              dev_match={"modalities": {"input": ["text"], "output": ["text"]}})
    result = model_sync.sync_model({"name": "m", "endpoints": [{"url": "http://x"}]})
    assert "image" in result["updates"]["input_modalities"]


def test_server_multimodal_enabled_keeps_dev_image(monkeypatch):
    """enable_multimodal=True + models.dev already lists image → no double-add."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 32768, "backend": "sglang", "is_embedding": False, "enable_multimodal": True},
              dev_match={"modalities": {"input": ["text", "image"], "output": ["text"]}})
    result = model_sync.sync_model({"name": "m", "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["input_modalities"] == ["text", "image"]


def test_vllm_modalities_use_dev_only(monkeypatch):
    """vLLM path (no backend=sglang) → models.dev modalities win, no veto."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 32768},  # vLLM: no backend key
              dev_match={"modalities": {"input": ["text", "image"], "output": ["text"]}})
    result = model_sync.sync_model({"name": "m", "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["input_modalities"] == ["text", "image"]
