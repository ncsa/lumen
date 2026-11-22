"""Tests for field precedence in lumen/services/model_sync.sync_model."""
import pytest

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


def test_find_rejects_version_digit_only_overlap():
    """Single-digit version fragments (e.g. "1" and "0" from "1.0") must not
    create a false match between unrelated models. Without filtering single
    digits, ornith-1.0-35b (tokens: ornith,1,0) would match xai/grok-build-0.1
    (tokens: xai,grok,build,0,1) with score 2 — purely on version digits."""
    models = [
        {"id": "xai/grok-build-0.1", "name": "Grok Build 0.1", "modalities": {"input": ["text"]}},
    ]
    assert model_sync.find_in_modelsdev(
        "deepreinforce-ai/Ornith-1.0-35B-FP8", models,
        config_name="ornith-1.0-35b") is None


@pytest.mark.parametrize(("model_id", "listing"), [
    ("deepseek-ai/DeepSeek-Coder-V2-Instruct", "deepseek-coder-v1"),
    ("deepseek-ai/DeepSeek-V5-Flash", "deepseek-v4-flash"),
    ("Qwen/Qwen4-Coder-30B-A3B-Instruct", "qwen3-coder-30b-a3b-instruct"),
])
def test_find_rejects_version_mismatch(model_id, listing):
    """A different major version never matches (v2 vs v1, qwen4 vs qwen3), even though the
    names share enough tokens; digits are read for the guard though they never score."""
    assert model_sync.find_in_modelsdev(model_id, [{"id": listing, "name": ""}]) is None


def test_find_matches_minor_version_and_ignores_sizes():
    """v4.1 is generation 4, and sizes/precisions (30B, A3B, 8x7B, FP8) are not versions."""
    models = [{"id": "deepseek-v4-flash", "name": ""}, {"id": "mixtral-8x7b-instruct", "name": ""}]
    assert model_sync.find_in_modelsdev("deepseek-ai/DeepSeek-V4.1-Flash-FP8", models)["id"] == "deepseek-v4-flash"
    assert model_sync.find_in_modelsdev("mistralai/Mixtral-8x7B-Instruct-v0.1", models)["id"] == "mixtral-8x7b-instruct"


@pytest.mark.parametrize(("value", "versions"), [
    ("deepseek-coder-v2", {"2"}),
    ("DeepSeek-V4.1-Flash-FP8", {"4"}),
    ("Qwen3-Coder-30B-A3B-Instruct-FP8", {"3"}),
    ("mixtral-8x7b-bf16-int4-q5_k_m", set()),
    ("gpt-4o-2024-08-06", {"4", "2024", "8", "6"}),
])
def test_versions(value, versions):
    assert model_sync._versions(value) == versions


def test_description_filled_from_dev_when_blank(monkeypatch):
    """A blank operator description is filled from models.dev on a trusted match."""
    _patch(monkeypatch,
           ep_model=None,
           dev_match={"description": "A great model for coding."})
    result = model_sync.sync_model({"name": "m", "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["description"] == "A great model for coding."


def test_description_not_overwritten_when_set(monkeypatch):
    """A hand-written operator description is never overwritten by models.dev."""
    _patch(monkeypatch,
           ep_model=None,
           dev_match={"description": "A great model for coding."})
    result = model_sync.sync_model({
        "name": "m", "description": "My custom description",
        "endpoints": [{"url": "http://x"}],
    })
    assert "description" not in result["updates"]


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
    result = model_sync.sync_model({"name": "gpt-4o", "auto_price": True, "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["input_cost_per_million"] == 3.0          # mean(2.5, 3.5)
    assert result["updates"]["output_cost_per_million"] == round(34 / 3, 6)  # mean(10, 12, 12)


def test_price_zero_listing_excluded(monkeypatch):
    """A provider listing $0 is dropped from the average, not treated as free."""
    dev_models = [
        {"id": "p/llama-3.3-70b", "cost": {"input": 0, "output": 0}},
        {"id": "q/llama-3.3-70b", "cost": {"input": 1.2, "output": 1.8}},
    ]
    _patch_price(monkeypatch, dev_models, dev_models[1])
    result = model_sync.sync_model({"name": "llama-3.3-70b", "auto_price": True, "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["input_cost_per_million"] == 1.2
    assert result["updates"]["output_cost_per_million"] == 1.8


def test_no_price_update_when_all_zero(monkeypatch):
    """If every provider lists $0, no cost update is proposed (never 0)."""
    dev_models = [{"id": "p/m", "cost": {"input": 0, "output": 0}}]
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({"name": "m", "auto_price": True, "endpoints": [{"url": "http://x"}]})
    assert "input_cost_per_million" not in result["updates"]
    assert "output_cost_per_million" not in result["updates"]


def test_embedding_model_gets_no_output_price(monkeypatch):
    """An embedding model's output price is always 0: auto price proposes only the
    input price, and the models.dev hint reports an output price of 0."""
    dev_models = [{"id": "p/bge-m3", "cost": {"input": 0.1, "output": 0.5}}]
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({
        "name": "bge-m3", "auto_price": True, "output_cost_per_million": 0,
        "input_modalities": ["text"], "output_modalities": ["embedding"],
        "endpoints": [{"url": "http://x"}],
    })
    assert result["updates"]["input_cost_per_million"] == 0.1
    assert "output_cost_per_million" not in result["updates"]
    assert result["modelsdev_price"] == {"input": 0.1, "output": 0}


def test_model_becoming_embedding_gets_no_output_price(monkeypatch):
    """When Update itself marks the model as embedding, no output price is proposed either."""
    dev_models = [{"id": "p/bge-m3", "cost": {"input": 0.1, "output": 0.5},
                   "modalities": {"input": ["text"], "output": ["embedding"]}}]
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({"name": "bge-m3", "auto_price": True, "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["output_modalities"] == ["embedding"]
    assert "output_cost_per_million" not in result["updates"]
    assert result["modelsdev_price"]["output"] == 0


def test_text_model_still_gets_output_price(monkeypatch):
    dev_models = [{"id": "p/m", "cost": {"input": 1.0, "output": 3.0}}]
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({
        "name": "m", "auto_price": True, "output_modalities": ["text"], "endpoints": [{"url": "http://x"}],
    })
    assert result["updates"]["output_cost_per_million"] == 3.0
    assert result["modelsdev_price"] == {"input": 1.0, "output": 3.0}


def test_cache_prices_filled_by_auto_cache_price(monkeypatch):
    """auto_cache_price: true pulls the models.dev cache rates in too."""
    dev_models = [
        {"id": "openai/gpt-4o", "cost": {"input": 2.5, "output": 10, "cache_read": 0.4, "cache_write": 0.8}},
        {"id": "azure/gpt-4o", "cost": {"input": 3.5, "output": 12, "cache_read": 0.6, "cache_write": 1.2}},
    ]
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({
        "name": "gpt-4o", "auto_price": True, "auto_cache_price": True,
        "cached_input_cost_per_million": 0,
        "cache_write_cost_per_million": 0,
        "endpoints": [{"url": "http://x"}],
    })
    assert result["updates"]["cached_input_cost_per_million"] == 0.5  # mean(0.4, 0.6)
    assert result["updates"]["cache_write_cost_per_million"] == 1.0  # mean(0.8, 1.2)


@pytest.mark.parametrize("flag", [{}, {"auto_cache_price": False}])
def test_cache_price_kept_without_auto_cache_price(monkeypatch, flag):
    """Cache rates are a separate opt-in: plain auto_price leaves them alone."""
    dev_models = [{"id": "p/m", "cost": {"input": 5.0, "output": 15.0,
                                          "cache_read": 0.5, "cache_write": 1.0}}]
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({
        "name": "m", "auto_price": True,
        "cached_input_cost_per_million": 0.1,
        "cache_write_cost_per_million": 0.2,
        "endpoints": [{"url": "http://x"}],
        **flag,
    })
    assert "cached_input_cost_per_million" not in result["updates"]
    assert "cache_write_cost_per_million" not in result["updates"]


def test_cache_price_untouched_without_a_modelsdev_rate(monkeypatch):
    """No cache rates listed on models.dev → operator values are preserved."""
    dev_models = [{"id": "p/m", "cost": {"input": 5.0, "output": 15.0}}]
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({
        "name": "m", "auto_cache_price": True,
        "cached_input_cost_per_million": 0.1,
        "cache_write_cost_per_million": 0.2,
        "endpoints": [{"url": "http://x"}],
    })
    assert "cached_input_cost_per_million" not in result["updates"]
    assert "cache_write_cost_per_million" not in result["updates"]


def test_price_overwrites_stale_operator_value(monkeypatch):
    """With auto_price, a trusted match corrects a stale/zero operator-set price."""
    dev_models = [{"id": "p/m", "cost": {"input": 5.0, "output": 15.0}}]
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({
        "name": "m",
        "auto_price": True,
        "input_cost_per_million": 0,
        "output_cost_per_million": 0,
        "endpoints": [{"url": "http://x"}],
    })
    assert result["updates"]["input_cost_per_million"] == 5.0
    assert result["updates"]["output_cost_per_million"] == 15.0


@pytest.mark.parametrize("flag", [{}, {"auto_price": False}])
def test_manual_price_kept_but_other_fields_update(monkeypatch, flag):
    """Without auto_price: true, prices are left alone; other fields still sync."""
    dev_models = [{
        "id": "p/m",
        "cost": {"input": 5.0, "output": 15.0},
        "limit": {"context": 200000, "output": 64000},
        "reasoning": True,
    }]
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({
        "name": "m",
        "input_cost_per_million": 1.0,
        "output_cost_per_million": 2.0,
        "endpoints": [{"url": "http://x"}],
        **flag,
    })
    assert "input_cost_per_million" not in result["updates"]
    assert "output_cost_per_million" not in result["updates"]
    assert result["updates"]["context_window"] == 200000
    assert result["updates"]["max_output_tokens"] == 64000
    assert result["updates"]["supports_reasoning"] is True


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


# ── supports_reasoning: models.dev provider consensus ──────────────────────────

def _reasoning_models(*flags):
    # Distinct provider ids that all normalize to "m", so they share one price-index group.
    return [{"id": f"p{i}/m", "reasoning": f} for i, f in enumerate(flags)]


def test_reasoning_majority_wins_over_first_match(monkeypatch):
    dev_models = _reasoning_models(False, True, True)
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({"name": "m", "supports_reasoning": False, "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["supports_reasoning"] is True


def test_reasoning_tie_leaves_field_untouched(monkeypatch):
    dev_models = _reasoning_models(True, False)
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({"name": "m", "supports_reasoning": False, "endpoints": [{"url": "http://x"}]})
    assert "supports_reasoning" not in result["updates"]


def test_reasoning_tie_does_not_downgrade_true(monkeypatch):
    """An even split leaves an operator's True alone instead of deciding False."""
    dev_models = _reasoning_models(True, False)
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({"name": "m", "supports_reasoning": True, "endpoints": [{"url": "http://x"}]})
    assert "supports_reasoning" not in result["updates"]


def test_reasoning_ignores_providers_without_boolean(monkeypatch):
    dev_models = _reasoning_models(None, "yes", True)
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({"name": "m", "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["supports_reasoning"] is True


def test_reasoning_no_data_leaves_field_untouched(monkeypatch):
    dev_models = _reasoning_models(None, None)
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({"name": "m", "supports_reasoning": True, "endpoints": [{"url": "http://x"}]})
    assert "supports_reasoning" not in result["updates"]


def test_reasoning_missing_key_treated_as_false(monkeypatch):
    dev_models = _reasoning_models(False, False, True)
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({"name": "m", "endpoints": [{"url": "http://x"}]})
    assert "supports_reasoning" not in result["updates"]


def test_reasoning_true_already_matches_consensus(monkeypatch):
    dev_models = _reasoning_models(True, True, False)
    _patch_price(monkeypatch, dev_models, dev_models[2])
    result = model_sync.sync_model({"name": "m", "supports_reasoning": True, "endpoints": [{"url": "http://x"}]})
    assert "supports_reasoning" not in result["updates"]


def test_reasoning_downgraded_when_consensus_false(monkeypatch):
    dev_models = _reasoning_models(True, False, False)
    _patch_price(monkeypatch, dev_models, dev_models[0])
    result = model_sync.sync_model({"name": "m", "supports_reasoning": True, "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["supports_reasoning"] is False


def test_consensus_without_dev_id_is_none():
    index = model_sync._build_price_index(_reasoning_models(True, True))
    assert model_sync._consensus_bool({"reasoning": True}, index, "reasoning") is None


@pytest.mark.parametrize("flags, expected", [
    # Ties: undecided, never False.
    ([True, False], None),
    ([False, True], None),
    ([True, True, False, False], None),
    ([True, False, None, "yes"], None),
    # Strict majority wins.
    ([True, True, False], True),
    ([False, False, True], False),
    ([False, True, True], True),
    ([True, True, True, False, False], True),
    ([True], True),
    ([False], False),
    # No boolean votes at all.
    ([None, None], None),
])
def test_consensus_bool_vote_table(flags, expected):
    dev_models = _reasoning_models(*flags)
    index = model_sync._build_price_index(dev_models)
    assert model_sync._consensus_bool(dev_models[0], index, "reasoning") is expected


# ── fetch_endpoint_model: server info first ────────────────────────────────────

class _Resp:
    def __init__(self, payload, ok=True):
        self._p = payload
        self.status = 200 if ok else 404
    def json(self):
        if self.status != 200:
            raise ValueError("http error")
        return self._p


def _patch_probe(monkeypatch, fake_get):
    """Route the pinned probe through fake_get and skip real DNS validation."""
    monkeypatch.setattr(model_sync, "_validate_endpoint_url", lambda url: "93.184.216.34")
    monkeypatch.setattr(model_sync, "_pinned_get", lambda url, ip, headers: fake_get(url))


def test_sglang_server_info_preferred_over_v1_models(monkeypatch):
    """The server info is read first; when it 200s, /v1/models is never hit.
    SGLang's max_req_input_len wins over /v1/models' theoretical max_model_len."""
    calls = []
    def fake_get(url, **kw):
        calls.append(url)
        if url.endswith("/get_server_info"):
            return _Resp({"max_req_input_len": 500410, "served_model_name": "zai-org/GLM-5.2-FP8",
                          "is_embedding": False, "enable_multimodal": None})
        return _Resp({"data": [{"id": "zai-org/GLM-5.2-FP8", "max_model_len": 1048576}]})
    _patch_probe(monkeypatch, fake_get)
    r = model_sync.fetch_endpoint_model({"url": "http://x", "api_key": "k"})
    assert r["max_model_len"] == 500410
    assert r["id"] == "zai-org/GLM-5.2-FP8"
    assert r["backend"] == "sglang"
    assert r["is_embedding"] is False
    assert r["enable_multimodal"] is None
    assert not any(u.endswith("/models") for u in calls), "v1/models must not be called when server_info succeeds"


def test_sglang_management_endpoints_probed_at_root_not_v1(monkeypatch):
    """SGLang's /get_server_info lives at the server root, not under /v1.
    An endpoint URL like http://host/v1 must probe http://host/get_server_info,
    not http://host/v1/get_server_info (which 404s on real SGLang). Without the
    /v1 strip, max_req_input_len is never seen and the fallback to /v1/models
    reports the model's theoretical max (e.g. 1048576) instead of the configured
    window (e.g. 500410)."""
    calls = []
    def fake_get(url, **kw):
        calls.append(url)
        if url == "http://x/get_server_info":
            return _Resp({"max_req_input_len": 500410, "served_model_name": "zai-org/GLM-5.2-FP8",
                          "is_embedding": False, "enable_multimodal": None})
        return _Resp({}, ok=False)
    _patch_probe(monkeypatch, fake_get)
    r = model_sync.fetch_endpoint_model({"url": "http://x/v1", "api_key": "k"})
    assert r["max_model_len"] == 500410
    assert r["backend"] == "sglang"
    assert "http://x/get_server_info" in calls
    assert not any("/v1/get_server_info" in u for u in calls), "must probe root, not /v1"


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
    _patch_probe(monkeypatch, fake_get)
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
    _patch_probe(monkeypatch, fake_get)
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
    _patch_probe(monkeypatch, fake_get)
    r = model_sync.fetch_endpoint_model({"url": "http://x", "api_key": "k"})
    assert r["max_model_len"] == 32768
    assert r["id"] == "m"
    assert "backend" not in r  # vLLM path: no sglang flags


def test_proxy_200_empty_body_not_tagged_sglang(monkeypatch):
    """A proxy/gateway in front of vLLM that returns 200 {} for /get_server_info
    must not be tagged backend="sglang" — that would trigger the text-only veto
    on a real multimodal vLLM server behind it."""
    def fake_get(url, **kw):
        if url.endswith("/get_server_info"):
            return _Resp({})  # 200 but empty — not SGLang
        if url.endswith("/models"):
            return _Resp({"data": [{"id": "m", "max_model_len": 32768}]})
        return _Resp({}, ok=False)
    _patch_probe(monkeypatch, fake_get)
    r = model_sync.fetch_endpoint_model({"url": "http://x", "api_key": "k"})
    assert "backend" not in r  # must fall through to vLLM path
    assert r["max_model_len"] == 32768


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
    _patch_probe(monkeypatch, fake_get)
    r = model_sync.fetch_endpoint_model({"url": "http://x", "api_key": "k"})
    assert r["max_model_len"] == 8192


# ── reasoning from the backend's /server_info ─────────────────────────────────

def _sync_with_probe(monkeypatch, fake_get, dev_flags, current=None):
    """Run sync_model with a stubbed probe and models.dev providers reporting dev_flags."""
    _patch_probe(monkeypatch, fake_get)
    dev_models = _reasoning_models(*dev_flags)
    monkeypatch.setattr(model_sync, "get_modelsdev", lambda: (dev_models, model_sync._build_price_index(dev_models)))
    monkeypatch.setattr(model_sync, "find_in_modelsdev", lambda *a, **k: dev_models[0])
    model_def = {"name": "m", "endpoints": [{"url": "http://x/v1"}]}
    if current is not None:
        model_def["supports_reasoning"] = current
    return model_sync.sync_model(model_def)


def _sglang_server_info(reasoning_parser):
    def fake_get(url):
        if url == "http://x/server_info?config_format=json":
            return _Resp({"max_req_input_len": 32768, "served_model_name": "m", "is_embedding": False,
                          "enable_multimodal": None, "reasoning_parser": reasoning_parser})
        return _Resp({}, ok=False)
    return fake_get


def _vllm_server_info(server_info):
    def fake_get(url):
        if url == "http://x/server_info?config_format=json":
            return server_info
        if url == "http://x/v1/models":
            return _Resp({"data": [{"id": "m", "max_model_len": 32768}]})
        return _Resp({}, ok=False)
    return fake_get


def _vllm_config(reasoning_parser):
    return {"vllm_config": {"structured_outputs_config": {"reasoning_parser": reasoning_parser}}}


def test_sglang_reasoning_parser_set_overrides_dev_false(monkeypatch):
    result = _sync_with_probe(monkeypatch, _sglang_server_info("qwen3"), [False, False])
    assert result["updates"]["supports_reasoning"] is True


def test_sglang_reasoning_parser_null_overrides_dev_true(monkeypatch):
    result = _sync_with_probe(monkeypatch, _sglang_server_info(None), [True, True], current=True)
    assert result["updates"]["supports_reasoning"] is False


def test_sglang_reasoning_parser_null_with_missing_key_is_no_change(monkeypatch):
    """Backend false + unset field: no update, even when models.dev says true."""
    result = _sync_with_probe(monkeypatch, _sglang_server_info(None), [True, True])
    assert "supports_reasoning" not in result["updates"]


def test_old_sglang_get_server_info_falls_back_to_dev_consensus(monkeypatch):
    """/server_info 404 → /get_server_info without reasoning_parser: models.dev
    decides reasoning; the context and modality flags still come through."""
    calls = []
    def fake_get(url):
        calls.append(url)
        if url == "http://x/get_server_info":
            return _Resp({"max_req_input_len": 500410, "served_model_name": "m",
                          "is_embedding": False, "enable_multimodal": False})
        return _Resp({}, ok=False)
    _patch_probe(monkeypatch, fake_get)
    r = model_sync.fetch_endpoint_model({"url": "http://x/v1", "api_key": "k"})
    assert calls[:2] == ["http://x/server_info?config_format=json", "http://x/get_server_info"]
    assert r["backend"] == "sglang"
    assert r["max_model_len"] == 500410
    assert "reasoning" not in r

    result = _sync_with_probe(monkeypatch, fake_get, [False, True, True])
    assert result["updates"]["supports_reasoning"] is True
    assert result["updates"]["context_window"] == 500410
    assert result["updates"]["input_modalities"] == ["text"]


def test_vllm_dev_mode_reasoning_parser_set(monkeypatch):
    fake_get = _vllm_server_info(_Resp(_vllm_config("qwen3")))
    _patch_probe(monkeypatch, fake_get)
    r = model_sync.fetch_endpoint_model({"url": "http://x/v1", "api_key": "k"})
    assert r == {"id": "m", "max_model_len": 32768, "backend": "vllm", "reasoning": True}

    result = _sync_with_probe(monkeypatch, fake_get, [False, False])
    assert result["updates"]["supports_reasoning"] is True


def test_vllm_dev_mode_reasoning_parser_empty(monkeypatch):
    result = _sync_with_probe(monkeypatch, _vllm_server_info(_Resp(_vllm_config(""))), [True, True], current=True)
    assert result["updates"]["supports_reasoning"] is False


def test_vllm_config_without_reasoning_parser_falls_back_to_dev(monkeypatch):
    fake_get = _vllm_server_info(_Resp({"vllm_config": {"model_config": {}}}))
    _patch_probe(monkeypatch, fake_get)
    r = model_sync.fetch_endpoint_model({"url": "http://x/v1", "api_key": "k"})
    assert r["backend"] == "vllm"
    assert "reasoning" not in r


def test_vllm_server_info_404_falls_back_to_dev(monkeypatch):
    fake_get = _vllm_server_info(_Resp({}, ok=False))
    _patch_probe(monkeypatch, fake_get)
    r = model_sync.fetch_endpoint_model({"url": "http://x/v1", "api_key": "k"})
    assert "backend" not in r
    assert "reasoning" not in r

    result = _sync_with_probe(monkeypatch, fake_get, [False, True, True])
    assert result["updates"]["supports_reasoning"] is True


def test_vllm_text_format_falls_back_to_dev(monkeypatch):
    """The default text format returns vllm_config as one string: unreadable, so models.dev decides."""
    fake_get = _vllm_server_info(_Resp({"vllm_config": "model='m', reasoning_parser='qwen3', ..."}))
    _patch_probe(monkeypatch, fake_get)
    r = model_sync.fetch_endpoint_model({"url": "http://x/v1", "api_key": "k"})
    assert "backend" not in r
    assert "reasoning" not in r

    result = _sync_with_probe(monkeypatch, fake_get, [True, False, False], current=True)
    assert result["updates"]["supports_reasoning"] is False


def test_server_info_non_json_falls_back_to_get_server_info(monkeypatch):
    class _BadJson(_Resp):
        def json(self):
            raise ValueError("not json")
    def fake_get(url):
        if url.endswith("/server_info?config_format=json"):
            return _BadJson({})
        if url.endswith("/get_server_info"):
            return _Resp({"max_req_input_len": 8192, "reasoning_parser": "deepseek-r1"})
        return _Resp({}, ok=False)
    _patch_probe(monkeypatch, fake_get)
    r = model_sync.fetch_endpoint_model({"url": "http://x", "api_key": "k"})
    assert r["max_model_len"] == 8192
    assert r["reasoning"] is True


# ── server-authoritative modalities ───────────────────────────────────────────

def _patch_ep(monkeypatch, ep_model, dev_match):
    monkeypatch.setattr(model_sync, "fetch_endpoint_model", lambda ep: ep_model)
    monkeypatch.setattr(model_sync, "get_modelsdev", lambda: ([dev_match] if dev_match else [], {}))
    monkeypatch.setattr(model_sync, "find_in_modelsdev", lambda *a, **k: dev_match)


def test_server_embedding_sets_text_in_embedding_out(monkeypatch):
    """is_embedding=True from SGLang → input=['text'], output=['embedding'] (authoritative)."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 8192, "backend": "sglang", "is_embedding": True, "enable_multimodal": None},
              dev_match={"modalities": {"input": ["text"], "output": ["text"]}})
    result = model_sync.sync_model({"name": "m", "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["input_modalities"] == ["text"]
    assert result["updates"]["output_modalities"] == ["embedding"]


def test_server_non_embedding_leaves_empty_output_unmarked(monkeypatch):
    """The text-in / []-out pair alone is not proof: a chat backend
    (is_embedding false) is never re-synced to the marker."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 8192, "backend": "sglang", "is_embedding": False},
              dev_match=None)
    result = model_sync.sync_model({
        "name": "m", "input_modalities": ["text"], "output_modalities": [],
        "endpoints": [{"url": "http://x"}],
    })
    assert "output_modalities" not in result["updates"]


def test_server_embedding_replaces_legacy_empty_output(monkeypatch):
    """A model synced before the marker existed (output []) is re-synced to it."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 8192, "backend": "sglang", "is_embedding": True},
              dev_match=None)
    result = model_sync.sync_model({
        "name": "m", "input_modalities": ["text"], "output_modalities": [],
        "endpoints": [{"url": "http://x"}],
    })
    assert "input_modalities" not in result["updates"]
    assert result["updates"]["output_modalities"] == ["embedding"]


def test_server_multimodal_null_keeps_dev_image(monkeypatch):
    """enable_multimodal=null (SGLang auto-detect) keeps models.dev's image claim."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 32768, "backend": "sglang", "is_embedding": False, "enable_multimodal": None},
              dev_match={"modalities": {"input": ["text", "image"], "output": ["text"]}})
    result = model_sync.sync_model({"name": "m", "input_modalities": ["text"],
                                    "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["input_modalities"] == ["text", "image"]


def test_server_multimodal_null_keeps_operator_image(monkeypatch):
    """enable_multimodal=null + operator already has image → no change proposed."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 32768, "backend": "sglang", "is_embedding": False, "enable_multimodal": None},
              dev_match={"modalities": {"input": ["text", "image"], "output": ["text"]}})
    result = model_sync.sync_model({"name": "m", "input_modalities": ["text", "image"],
                                    "output_modalities": ["text"],
                                    "endpoints": [{"url": "http://x"}]})
    assert "input_modalities" not in result["updates"]
    assert "output_modalities" not in result["updates"]


def test_server_multimodal_null_no_dev_match_keeps_operator(monkeypatch):
    """enable_multimodal=null + no models.dev match → operator modalities untouched."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 32768, "backend": "sglang", "is_embedding": False, "enable_multimodal": None},
              dev_match=None)
    result = model_sync.sync_model({"name": "m", "input_modalities": ["text", "audio"],
                                    "endpoints": [{"url": "http://x"}]})
    assert "input_modalities" not in result["updates"]
    assert "output_modalities" not in result["updates"]


def test_server_multimodal_missing_keeps_operator(monkeypatch):
    """enable_multimodal absent from /get_server_info behaves like null."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 32768, "backend": "sglang", "is_embedding": False},
              dev_match=None)
    result = model_sync.sync_model({"name": "m", "input_modalities": ["text", "image"],
                                    "endpoints": [{"url": "http://x"}]})
    assert "input_modalities" not in result["updates"]
    assert "output_modalities" not in result["updates"]


def test_server_multimodal_false_strips_image(monkeypatch):
    """enable_multimodal=False on the server vetoes models.dev's image claim."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 32768, "backend": "sglang", "is_embedding": False, "enable_multimodal": False},
              dev_match={"modalities": {"input": ["text", "image"], "output": ["text"]}})
    result = model_sync.sync_model({"name": "m", "input_modalities": ["text", "image"],
                                    "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["input_modalities"] == ["text"]


def test_server_multimodal_false_keeps_audio(monkeypatch):
    """enable_multimodal=False strips image/video but keeps audio."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 32768, "backend": "sglang", "is_embedding": False, "enable_multimodal": False},
              dev_match={"modalities": {"input": ["text", "image", "audio", "video"], "output": ["text"]}})
    result = model_sync.sync_model({"name": "m", "input_modalities": ["text"],
                                    "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["input_modalities"] == ["text", "audio"]


def test_server_multimodal_false_keeps_operator_audio(monkeypatch):
    """enable_multimodal=False + no models.dev match → operator's audio kept, no change."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 32768, "backend": "sglang", "is_embedding": False, "enable_multimodal": False},
              dev_match=None)
    result = model_sync.sync_model({"name": "m", "input_modalities": ["text", "audio"],
                                    "endpoints": [{"url": "http://x"}]})
    assert "input_modalities" not in result["updates"]


def test_server_multimodal_enabled_audio_only_unchanged(monkeypatch):
    """enable_multimodal=True + audio-only model → image is not added."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 32768, "backend": "sglang", "is_embedding": False, "enable_multimodal": True},
              dev_match={"modalities": {"input": ["text", "audio"], "output": ["text"]}})
    result = model_sync.sync_model({"name": "m", "input_modalities": ["text", "audio"],
                                    "endpoints": [{"url": "http://x"}]})
    assert "input_modalities" not in result["updates"]


def test_server_multimodal_enabled_operator_audio_only_unchanged(monkeypatch):
    """enable_multimodal=True + no models.dev match + operator ['text','audio'] → no image added."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 32768, "backend": "sglang", "is_embedding": False, "enable_multimodal": True},
              dev_match=None)
    result = model_sync.sync_model({"name": "m", "input_modalities": ["text", "audio"],
                                    "endpoints": [{"url": "http://x"}]})
    assert "input_modalities" not in result["updates"]


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


# ---------------------------------------------------------------------------
# knowledge cutoff normalization — models.dev sometimes reports YYYY-MM-DD but
# the DB column is String(7) YYYY-MM
# ---------------------------------------------------------------------------

def test_normalize_knowledge_truncates_full_date():
    assert model_sync._normalize_knowledge("2024-06-15") == "2024-06"


def test_normalize_knowledge_passes_year_month():
    assert model_sync._normalize_knowledge("2024-06") == "2024-06"


def test_normalize_knowledge_empty_is_none():
    assert model_sync._normalize_knowledge(None) is None
    assert model_sync._normalize_knowledge("") is None


def test_normalize_knowledge_drops_long_garbage():
    assert model_sync._normalize_knowledge("not-a-date-at-all") is None
    assert model_sync._normalize_knowledge("2024") is None
    assert model_sync._normalize_knowledge("2024-13") is None


def test_validate_url_rejects_cgnat_and_unspecified_ips():
    for url in ("http://100.64.0.1/v1", "http://[::ffff:100.64.0.1]/v1",
                "http://0.0.0.0/v1", "http://[::]/v1"):
        try:
            model_sync._validate_endpoint_url(url)
        except ValueError as exc:
            assert "private/reserved" in str(exc)
        else:
            raise AssertionError(f"unsafe endpoint accepted: {url}")


def test_sync_model_truncates_dev_knowledge_date(monkeypatch):
    """A YYYY-MM-DD knowledge value from models.dev is stored as YYYY-MM."""
    _patch(monkeypatch,
           ep_model=None,
           dev_match={"knowledge": "2024-06-15"})
    result = model_sync.sync_model({"name": "m", "endpoints": [{"url": "http://x"}]})
    assert result["updates"]["knowledge_cutoff"] == "2024-06"


# ---------------------------------------------------------------------------
# _validate_endpoint_url — SSRF guard
# ---------------------------------------------------------------------------

def _fake_addrinfo(ip, family=None):
    """Minimal getaddrinfo result resolving to the given IP.

    ``family`` defaults to AF_INET; pass ``socket.AF_INET6`` to get the IPv6
    entry shape, whose sockaddr is the 4-tuple ``(ip, port, flowinfo, scope_id)``.
    """
    import socket
    family = family or socket.AF_INET
    sockaddr = (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


def test_validate_url_rejects_non_http_schemes():
    import pytest

    from lumen.services.model_sync import _validate_endpoint_url
    for url in ("ftp://example.com/v1", "file:///etc/passwd", "gopher://example.com"):
        with pytest.raises(ValueError, match="unsupported scheme"):
            _validate_endpoint_url(url)


def test_validate_url_rejects_missing_hostname():
    import pytest

    from lumen.services.model_sync import _validate_endpoint_url
    with pytest.raises(ValueError, match="no hostname"):
        _validate_endpoint_url("http:///v1")


def test_validate_url_rejects_private_and_loopback_ips():
    import pytest

    from lumen.services.model_sync import _validate_endpoint_url
    for url in ("http://10.0.0.1/v1", "http://192.168.1.1/v1", "http://172.16.0.1/v1",
                "http://127.0.0.1/v1", "http://169.254.169.254/v1"):
        with pytest.raises(ValueError, match="private/reserved"):
            _validate_endpoint_url(url)


def test_validate_url_rejects_hostname_resolving_to_private_ip(monkeypatch):
    import socket

    import pytest

    from lumen.services import model_sync
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _fake_addrinfo("127.0.0.1"))
    with pytest.raises(ValueError, match="private/reserved"):
        model_sync._validate_endpoint_url("http://internal.example.com/v1")


def _echo_literal_addrinfo(host, port, *a, **k):
    """getaddrinfo stand-in for IP-literal URLs: echo the literal back, no host IPv6 needed."""
    import socket
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return _fake_addrinfo(host, family)


@pytest.mark.parametrize("url, resolves_to", [
    # IP literals across IPv4 127.0.0.0/8, IPv6 ::1 and IPv4-mapped IPv6.
    ("http://127.0.0.1/v1", None),
    ("http://127.1.2.3/v1", None),
    ("http://[::1]/v1", None),
    ("http://[::ffff:127.0.0.1]/v1", None),
    # Hostnames that resolve to loopback over AF_INET and AF_INET6.
    ("http://internal.example.com/v1", ("127.0.0.1", "AF_INET")),
    ("http://internal.example.com/v1", ("::1", "AF_INET6")),
], ids=["v4-literal", "v4-literal-127/8", "v6-literal", "v4-mapped-v6-literal",
        "hostname-to-v4", "hostname-to-v6"])
def test_validate_url_rejects_loopback(monkeypatch, url, resolves_to):
    import socket

    if resolves_to is None:
        monkeypatch.setattr(socket, "getaddrinfo", _echo_literal_addrinfo)
    else:
        ip, family = resolves_to
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda *a, **k: _fake_addrinfo(ip, getattr(socket, family)))
    with pytest.raises(ValueError, match="private/reserved"):
        model_sync._validate_endpoint_url(url)


def test_validate_url_rejects_loopback_after_public_ip(monkeypatch):
    """Every resolved IP is checked, not only the first (pinned) one."""
    import socket

    infos = _fake_addrinfo("93.184.216.34") + _fake_addrinfo("::1", socket.AF_INET6)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: infos)
    with pytest.raises(ValueError, match="private/reserved"):
        model_sync._validate_endpoint_url("https://api.example.com/v1")


def test_validate_url_allows_public_ip_and_returns_pinned_ip(monkeypatch):
    import socket

    from lumen.services import model_sync
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _fake_addrinfo("93.184.216.34"))
    assert model_sync._validate_endpoint_url("https://api.example.com/v1") == "93.184.216.34"


def test_validate_url_dns_failure_rejected(monkeypatch):
    import socket

    import pytest

    from lumen.services import model_sync

    def boom(*a, **k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(ValueError, match="does not resolve"):
        model_sync._validate_endpoint_url("https://does-not-resolve.example/v1")


# ---------------------------------------------------------------------------
# get_modelsdev — TTL cache
# ---------------------------------------------------------------------------

@pytest.fixture
def modelsdev_clock(monkeypatch):
    """A fresh, empty cache, a controllable clock and a counting _fetch_modelsdev."""
    from types import SimpleNamespace
    monkeypatch.setattr(model_sync, "_cache", {"data": None, "ts": 0.0, "index": {}})
    clock = SimpleNamespace(now=1000.0, fetches=0, payload=[{"id": "m-1"}])
    monkeypatch.setattr(model_sync, "time", SimpleNamespace(monotonic=lambda: clock.now))

    def fetch():
        clock.fetches += 1
        return list(clock.payload)

    monkeypatch.setattr(model_sync, "_fetch_modelsdev", fetch)
    return clock


def test_get_modelsdev_first_call_fetches_and_indexes(modelsdev_clock):
    models, index = model_sync.get_modelsdev()
    assert modelsdev_clock.fetches == 1
    assert models == [{"id": "m-1"}]
    assert index == {"m1": [{"id": "m-1"}]}


def test_get_modelsdev_reuses_cache_within_ttl(modelsdev_clock):
    first = model_sync.get_modelsdev()
    modelsdev_clock.payload = [{"id": "other"}]
    modelsdev_clock.now += model_sync._TTL  # exactly at the TTL is still fresh
    assert model_sync.get_modelsdev() == first
    assert modelsdev_clock.fetches == 1


def test_get_modelsdev_refetches_after_ttl(modelsdev_clock):
    model_sync.get_modelsdev()
    modelsdev_clock.payload = [{"id": "other"}]
    modelsdev_clock.now += model_sync._TTL + 1
    models, index = model_sync.get_modelsdev()
    assert modelsdev_clock.fetches == 2
    assert models == [{"id": "other"}]
    assert index == {"other": [{"id": "other"}]}


def test_get_modelsdev_empty_fetch_returns_empty_list(modelsdev_clock):
    """A failed fetch caches [] and the caller still gets a list."""
    modelsdev_clock.payload = []
    assert model_sync.get_modelsdev() == ([], {})


def test_get_modelsdev_failed_fetch_reused_within_failed_ttl(modelsdev_clock):
    """A failed fetch is not retried on every call: it is reused for _FAILED_TTL."""
    modelsdev_clock.payload = []
    model_sync.get_modelsdev()
    modelsdev_clock.payload = [{"id": "m-1"}]
    modelsdev_clock.now += model_sync._FAILED_TTL  # exactly at the TTL is still fresh
    assert model_sync.get_modelsdev() == ([], {})
    assert modelsdev_clock.fetches == 1


def test_get_modelsdev_failed_fetch_retried_after_failed_ttl(modelsdev_clock):
    """A failed fetch is retried after _FAILED_TTL, well before the full _TTL."""
    assert model_sync._FAILED_TTL == 60 < model_sync._TTL
    modelsdev_clock.payload = []
    model_sync.get_modelsdev()
    modelsdev_clock.payload = [{"id": "m-1"}]
    modelsdev_clock.now += model_sync._FAILED_TTL + 1
    models, index = model_sync.get_modelsdev()
    assert modelsdev_clock.fetches == 2
    assert models == [{"id": "m-1"}]
    assert index == {"m1": [{"id": "m-1"}]}
    # The successful result now gets the full TTL.
    modelsdev_clock.payload = [{"id": "other"}]
    modelsdev_clock.now += model_sync._TTL
    assert model_sync.get_modelsdev()[0] == [{"id": "m-1"}]
    assert modelsdev_clock.fetches == 2


def test_get_modelsdev_success_not_refetched_after_failed_ttl(modelsdev_clock):
    """The shorter TTL applies only to a failed fetch."""
    model_sync.get_modelsdev()
    modelsdev_clock.now += model_sync._FAILED_TTL + 1
    model_sync.get_modelsdev()
    assert modelsdev_clock.fetches == 1


@pytest.mark.parametrize("payload, ok", [([], False), ([{"id": "m-1"}], True)])
def test_sync_model_reports_modelsdev_ok(modelsdev_clock, monkeypatch, payload, ok):
    """sync_model says whether models.dev could be fetched."""
    modelsdev_clock.payload = payload
    monkeypatch.setattr(model_sync, "fetch_endpoint_model", lambda ep: None)
    result = model_sync.sync_model({"name": "m-1", "endpoints": [{"url": "http://x"}]})
    assert result["modelsdev_ok"] is ok
    assert modelsdev_clock.fetches == 1


# ---------------------------------------------------------------------------
# _fetch_modelsdev
# ---------------------------------------------------------------------------

class _JsonResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_fetch_modelsdev_flattens_providers_and_skips_non_dicts(monkeypatch):
    calls = []
    payload = {
        "acme": {"models": {"a": {"id": "a"}, "junk": "not-a-model", "b": {"id": "b"}}},
        "no-models": {},
        "bad-provider": ["not", "a", "dict"],
        "other": {"models": {"c": {"id": "c"}}},
    }

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return _JsonResp(payload)

    monkeypatch.setattr(model_sync.requests, "get", fake_get)
    assert model_sync._fetch_modelsdev() == [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert calls == [(model_sync.MODELSDEV_URL, model_sync.MODELSDEV_TIMEOUT)]


@pytest.mark.parametrize("failure", ["network", "bad-json"])
def test_fetch_modelsdev_failure_returns_empty(monkeypatch, failure):
    import requests

    def fake_get(url, timeout):
        if failure == "network":
            raise requests.ConnectionError("down")
        resp = _JsonResp(None)
        resp.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
        return resp

    monkeypatch.setattr(model_sync.requests, "get", fake_get)
    assert model_sync._fetch_modelsdev() == []


# ---------------------------------------------------------------------------
# _pinned_get — connects to the validated IP, never re-resolves
# ---------------------------------------------------------------------------

class _FakePool:
    instances: list = []
    fail = False

    def __init__(self, host, port, **kwargs):
        self.host, self.port, self.kwargs = host, port, kwargs
        self.requests = []
        self.closed = False
        _FakePool.instances.append(self)

    def request(self, method, path, headers, redirect):
        self.requests.append((method, path, headers, redirect))
        if self.fail:
            raise ConnectionError("refused")
        return "response"

    def close(self):
        self.closed = True


@pytest.fixture
def fake_pools(monkeypatch):
    _FakePool.instances = []
    http = type("HTTPPool", (_FakePool,), {})
    https = type("HTTPSPool", (_FakePool,), {})
    monkeypatch.setattr(model_sync.urllib3, "HTTPConnectionPool", http)
    monkeypatch.setattr(model_sync.urllib3, "HTTPSConnectionPool", https)
    return http, https


def test_pinned_get_https_verifies_against_hostname(fake_pools):
    _, https = fake_pools
    resp = model_sync._pinned_get("https://api.example.com/v1/models?x=1", "93.184.216.34", {"Authorization": "Bearer k"})
    assert resp == "response"
    (pool,) = _FakePool.instances
    assert type(pool) is https
    assert (pool.host, pool.port) == ("93.184.216.34", 443)
    assert pool.kwargs["server_hostname"] == "api.example.com"
    assert pool.kwargs["assert_hostname"] == "api.example.com"
    assert pool.kwargs["ca_certs"]
    assert pool.kwargs["retries"] is False
    assert pool.requests == [(
        "GET", "/v1/models?x=1", {"Authorization": "Bearer k", "Host": "api.example.com"}, False,
    )]
    assert pool.closed


def test_pinned_get_http_with_explicit_port_and_no_path(fake_pools):
    http, _ = fake_pools
    model_sync._pinned_get("http://api.example.com:8000", "93.184.216.34", {})
    (pool,) = _FakePool.instances
    assert type(pool) is http
    assert (pool.host, pool.port) == ("93.184.216.34", 8000)
    assert pool.kwargs == {"timeout": model_sync.ENDPOINT_TIMEOUT, "retries": False}
    assert pool.requests == [("GET", "/", {"Host": "api.example.com:8000"}, False)]
    assert pool.closed


def test_pinned_get_closes_pool_when_request_fails(fake_pools, monkeypatch):
    monkeypatch.setattr(_FakePool, "fail", True)
    with pytest.raises(ConnectionError):
        model_sync._pinned_get("http://api.example.com/v1/models", "93.184.216.34", {})
    (pool,) = _FakePool.instances
    assert pool.closed


# ---------------------------------------------------------------------------
# Remaining fetch_endpoint_model / matching / pricing branches
# ---------------------------------------------------------------------------

def test_server_info_flags_non_dict_is_empty():
    assert model_sync._server_info_flags(["not", "a", "dict"]) == {}


def test_fetch_endpoint_model_blocked_url_is_none(monkeypatch):
    # Record, don't raise: fetch_endpoint_model swallows exceptions from the
    # probes, so an assertion inside the stub would never reach pytest.
    fetched = []
    monkeypatch.setattr(model_sync, "_pinned_get", lambda url, ip, headers: fetched.append(url))
    assert model_sync.fetch_endpoint_model({"url": "http://127.0.0.1/v1"}) is None
    assert fetched == []


def test_fetch_endpoint_model_all_probes_fail_is_none(monkeypatch):
    seen = []

    def fake_get(url):
        seen.append(url)
        raise ConnectionError("refused")

    _patch_probe(monkeypatch, fake_get)
    assert model_sync.fetch_endpoint_model({"url": "http://api.example.com/v1"}) is None
    assert seen == [
        "http://api.example.com/server_info?config_format=json",
        "http://api.example.com/get_server_info",
        "http://api.example.com/v1/models",
        "http://api.example.com/get_model_info",
    ]


def test_fetch_endpoint_model_v1_models_error_falls_back_to_get_model_info(monkeypatch):
    def fake_get(url):
        if url.endswith("/get_model_info"):
            return _Resp({"model_path": "org/m", "context_length": 4096})
        if url.endswith("/models"):
            raise ConnectionError("refused")
        return _Resp({}, ok=False)

    _patch_probe(monkeypatch, fake_get)
    assert model_sync.fetch_endpoint_model({"url": "http://api.example.com/v1"}) == {
        "id": "org/m", "max_model_len": 4096,
    }


def test_find_in_modelsdev_config_name_exact_match_wins():
    models = [{"id": "acme/llama-3.1-8b", "name": "Llama"}, {"id": "Llama_3.1-70B", "name": "Llama 70"}]
    assert model_sync.find_in_modelsdev("whatever", models, config_name="llama-3.1-70b") is models[1]


def test_find_in_modelsdev_only_noise_tokens_is_none():
    assert model_sync.find_in_modelsdev("org/70b-instruct", [{"id": "70b-instruct"}]) is None


def test_average_price_without_id_is_all_none():
    assert model_sync._average_price({}, {"x": [{"cost": {"input": 1}}]}) == (None, None, None, None)


def test_average_price_ignores_non_numeric_costs():
    index = {"m": [
        {"id": "m", "cost": {"input": "n/a", "output": 4, "cache_read": [1], "cache_write": None}},
        {"id": "m", "cost": {"input": 2, "output": 6}},
    ]}
    assert model_sync._average_price({"id": "m"}, index) == (2.0, 5.0, None, None)


def test_server_modalities_matching_operator_drop_dev_updates(monkeypatch):
    """When the server's authoritative modalities equal the operator's values, the
    models.dev proposals for both fields are withdrawn instead of applied."""
    _patch_ep(monkeypatch,
              ep_model={"id": "m", "max_model_len": 8192, "backend": "sglang", "is_embedding": True},
              dev_match={"modalities": {"input": ["text", "image"], "output": ["text"]}})
    result = model_sync.sync_model({
        "name": "m", "input_modalities": ["text"], "output_modalities": ["embedding"],
        "endpoints": [{"url": "http://x"}],
    })
    assert "input_modalities" not in result["updates"]
    assert "output_modalities" not in result["updates"]


# ---------------------------------------------------------------------------
# qwen3-embedding-8b (#680): id variants, a $0 listing with limit.output: 0
# ---------------------------------------------------------------------------

_QWEN_EMBED_LISTINGS = [
    # First listing: embedding provider reporting no output budget and no price.
    {"id": "qwen3-embedding-8b", "name": "Qwen3 Embedding 8B",
     "limit": {"context": 0, "output": 0}, "cost": {"input": 0, "output": 0}},
    {"id": "Qwen/Qwen3-Embedding-8B", "name": "Qwen3 Embedding 8B",
     "limit": {"context": 32768, "output": 0}, "cost": {"input": 0.01, "output": 0}},
    {"id": "qwen/qwen3-embedding-8b", "name": "Qwen3 Embedding 8B",
     "limit": {"context": 32768}, "cost": {"input": 0.07, "output": 0}},
    {"id": "alibaba/qwen3-embedding-8b", "name": "Qwen3 Embedding 8B",
     "limit": {"context": 32768}, "cost": {"input": 0.1, "output": 0.1}},
    {"id": "evroc/qwen3-embedding-8b", "name": "Qwen3 Embedding 8B",
     "cost": {"input": 0.115, "output": 0.115}},
]


def _patch_qwen_embed(monkeypatch):
    """Real matcher and price index over the listings; no endpoint."""
    models = list(_QWEN_EMBED_LISTINGS)
    monkeypatch.setattr(model_sync, "fetch_endpoint_model", lambda ep: None)
    monkeypatch.setattr(model_sync, "get_modelsdev",
                        lambda: (models, model_sync._build_price_index(models)))


_QWEN_IN = round((0.01 + 0.07 + 0.1 + 0.115) / 4, 6)
_QWEN_OUT = round((0.1 + 0.115) / 2, 6)


def test_qwen3_embedding_auto_price_sets_averages(monkeypatch):
    """A new model (auto_price ticked) gets the averaged models.dev price, and the
    0 limits of the first listing never become a 0 context or output cap."""
    _patch_qwen_embed(monkeypatch)
    result = model_sync.sync_model({
        "name": "qwen3-embedding-8b", "auto_price": True,
        "input_cost_per_million": 0, "output_cost_per_million": 0,
        "endpoints": [{"url": "http://x"}],
    })
    assert result["matched"] == "qwen3-embedding-8b"
    assert result["updates"]["input_cost_per_million"] == _QWEN_IN
    assert result["updates"]["output_cost_per_million"] == _QWEN_OUT
    assert result["modelsdev_price"] == {"input": _QWEN_IN, "output": _QWEN_OUT}
    assert "max_output_tokens" not in result["updates"]
    assert "context_window" not in result["updates"]


@pytest.mark.parametrize("flag", [{}, {"auto_price": False}])
def test_qwen3_embedding_manual_price_kept_but_reported(monkeypatch, flag):
    """Without auto_price the price stays, but modelsdev_price is still filled
    in so the editor can offer it."""
    _patch_qwen_embed(monkeypatch)
    result = model_sync.sync_model({
        "name": "qwen3-embedding-8b",
        "input_cost_per_million": 0, "output_cost_per_million": 0,
        "endpoints": [{"url": "http://x"}],
        **flag,
    })
    assert "input_cost_per_million" not in result["updates"]
    assert "output_cost_per_million" not in result["updates"]
    assert result["modelsdev_price"] == {"input": _QWEN_IN, "output": _QWEN_OUT}


@pytest.mark.parametrize("ep_id", [
    "Qwen/Qwen3-Embedding-8B", "qwen/qwen3-embedding-8b", "alibaba/qwen3-embedding-8b",
])
def test_qwen3_embedding_id_variants_resolve_same_price(monkeypatch, ep_id):
    """Every id a provider uses for the model gives the same averaged price."""
    _patch_qwen_embed(monkeypatch)
    result = model_sync.sync_model({
        "name": "my-embedder", "auto_price": True,
        "endpoints": [{"url": "http://x", "model": ep_id}],
    })
    assert result["matched"] is not None
    assert result["updates"]["input_cost_per_million"] == _QWEN_IN
    assert result["updates"]["output_cost_per_million"] == _QWEN_OUT


def test_zero_dev_limits_are_ignored(monkeypatch):
    """A models.dev limit of 0 is "not reported": the operator values stay."""
    _patch(monkeypatch, ep_model=None,
           dev_match={"limit": {"context": 0, "output": 0}})
    result = model_sync.sync_model({
        "name": "m", "context_window": 8192, "max_output_tokens": 1024,
        "endpoints": [{"url": "http://x"}],
    })
    assert "context_window" not in result["updates"]
    assert "max_output_tokens" not in result["updates"]


def test_modelsdev_price_none_without_match_or_price(monkeypatch):
    """modelsdev_price is null with no match, and with a match that has no price."""
    _patch(monkeypatch, ep_model=None, dev_match=None)
    assert model_sync.sync_model({"name": "m", "endpoints": []})["modelsdev_price"] is None
    dev_models = [{"id": "p/m", "cost": {"input": 0, "output": 0}}]
    _patch_price(monkeypatch, dev_models, dev_models[0])
    assert model_sync.sync_model({"name": "m", "endpoints": []})["modelsdev_price"] is None
