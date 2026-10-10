"""CLI sync_models.py mirrors the SGLang modality and reasoning rules of lumen.services.model_sync."""

import pytest

import sync_models
from lumen.services import model_sync

_SGLANG = {"max_model_len": 32768, "backend": "sglang", "is_embedding": False}
_DEV_IMAGE = {"modalities": {"input": ["text", "image"], "output": ["text"]}}


def _changes(model_def, ep_model, dev_model):
    changes, _ = sync_models.compute_changes(model_def, ep_model, dev_model)
    return changes


def test_cli_multimodal_null_keeps_dev_image():
    """enable_multimodal=null keeps models.dev's image claim."""
    changes = _changes({"input_modalities": ["text"]},
                       {**_SGLANG, "enable_multimodal": None}, _DEV_IMAGE)
    assert changes["input_modalities"] == (["text"], ["text", "image"])


def test_cli_multimodal_null_no_dev_match_keeps_operator():
    """enable_multimodal=null + no models.dev match → operator modalities untouched."""
    changes = _changes({"input_modalities": ["text", "audio"]},
                       {**_SGLANG, "enable_multimodal": None}, None)
    assert "input_modalities" not in changes
    assert "output_modalities" not in changes


def test_cli_multimodal_missing_keeps_operator():
    """enable_multimodal absent from /get_server_info behaves like null."""
    changes = _changes({"input_modalities": ["text", "image"]}, dict(_SGLANG), None)
    assert "input_modalities" not in changes


def test_cli_multimodal_false_strips_image():
    """enable_multimodal=False vetoes models.dev's image claim."""
    changes = _changes({"input_modalities": ["text", "image"]},
                       {**_SGLANG, "enable_multimodal": False}, _DEV_IMAGE)
    assert changes["input_modalities"] == (["text", "image"], ["text"])


def test_cli_embedding_sets_text_in_empty_out():
    """is_embedding=True → input=['text'], output=[]."""
    changes = _changes({}, {**_SGLANG, "is_embedding": True, "enable_multimodal": None}, None)
    assert changes["input_modalities"] == (None, ["text"])
    assert changes["output_modalities"] == (None, [])


def test_cli_multimodal_false_keeps_audio():
    """enable_multimodal=False strips image/video but keeps audio."""
    dev = {"modalities": {"input": ["text", "image", "audio", "video"], "output": ["text"]}}
    changes = _changes({"input_modalities": ["text"]},
                       {**_SGLANG, "enable_multimodal": False}, dev)
    assert changes["input_modalities"] == (["text"], ["text", "audio"])


def test_cli_multimodal_false_keeps_operator_audio():
    """enable_multimodal=False + no models.dev match → operator's audio kept, no change."""
    changes = _changes({"input_modalities": ["text", "audio"]},
                       {**_SGLANG, "enable_multimodal": False}, None)
    assert "input_modalities" not in changes


def test_cli_multimodal_enabled_operator_audio_only_unchanged():
    """enable_multimodal=True + no models.dev match + operator ['text','audio'] → no image added."""
    changes = _changes({"input_modalities": ["text", "audio"]},
                       {**_SGLANG, "enable_multimodal": True}, None)
    assert "input_modalities" not in changes


def test_cli_multimodal_enabled_adds_image():
    """enable_multimodal=True + text-only base → image added."""
    changes = _changes({"input_modalities": ["text"]},
                       {**_SGLANG, "enable_multimodal": True}, None)
    assert changes["input_modalities"] == (["text"], ["text", "image"])


_DEV_PRICED = {"id": "p/m", "cost": {"input": 5.0, "output": 15.0}}


def _price_changes(model_def):
    index = sync_models._build_price_index([_DEV_PRICED])
    changes, _ = sync_models.compute_changes(model_def, None, _DEV_PRICED, index)
    return changes


def test_cli_auto_price_updates_price():
    """auto_price: true lets the CLI propose models.dev prices."""
    changes = _price_changes({"auto_price": True, "input_cost_per_million": 1.0,
                              "output_cost_per_million": 2.0})
    assert changes["input_cost_per_million"] == (1.0, 5.0)
    assert changes["output_cost_per_million"] == (2.0, 15.0)


@pytest.mark.parametrize("flag", [{}, {"auto_price": False}, {"auto_price": "yes"}, {"auto_price": 1}])
def test_cli_manual_price_untouched(flag):
    """Without auto_price: true, the CLI never proposes a price change."""
    changes = _price_changes({"input_cost_per_million": 1.0,
                              "output_cost_per_million": 2.0, **flag})
    assert "input_cost_per_million" not in changes
    assert "output_cost_per_million" not in changes


# ── supports_reasoning: models.dev provider consensus ──────────────────────────

def _reasoning_models(*flags):
    # Distinct provider ids that all normalize to "m", so they share one price-index group.
    return [{"id": f"p{i}/m", "reasoning": f} for i, f in enumerate(flags)]


def _reasoning_changes(model_def, flags, match=0, ep_model=None):
    dev_models = _reasoning_models(*flags)
    index = sync_models._build_price_index(dev_models)
    changes, _ = sync_models.compute_changes(model_def, ep_model, dev_models[match], index)
    return changes


def test_cli_reasoning_majority_wins_over_first_match():
    changes = _reasoning_changes({"supports_reasoning": False}, [False, True, True])
    assert changes["supports_reasoning"] == (False, True)


def test_cli_reasoning_tie_leaves_field_untouched():
    changes = _reasoning_changes({"supports_reasoning": False}, [True, False])
    assert "supports_reasoning" not in changes


def test_cli_reasoning_ignores_providers_without_boolean():
    changes = _reasoning_changes({}, [None, "yes", True])
    assert changes["supports_reasoning"] == (None, True)


def test_cli_reasoning_no_data_leaves_field_untouched():
    changes = _reasoning_changes({"supports_reasoning": True}, [None, None])
    assert "supports_reasoning" not in changes


def test_cli_reasoning_missing_key_treated_as_false():
    changes = _reasoning_changes({}, [False, False, True])
    assert "supports_reasoning" not in changes


def test_cli_reasoning_true_already_matches_consensus():
    changes = _reasoning_changes({"supports_reasoning": True}, [True, True, False], match=2)
    assert "supports_reasoning" not in changes


def test_cli_reasoning_downgraded_when_consensus_false():
    changes = _reasoning_changes({"supports_reasoning": True}, [True, False, False])
    assert changes["supports_reasoning"] == (True, False)


def test_cli_consensus_without_dev_id_is_none():
    index = sync_models._build_price_index(_reasoning_models(True, True))
    assert sync_models._consensus_bool({"reasoning": True}, index, "reasoning") is None


# ── reasoning from the backend's /server_info ─────────────────────────────────

class _Resp:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self.ok = ok

    def json(self):
        return self._payload


def _patch_get(monkeypatch, fake_get):
    calls = []
    def get(url, **kw):
        calls.append(url)
        return fake_get(url)
    monkeypatch.setattr(sync_models.requests, "get", get)
    return calls


def _fetch(monkeypatch, fake_get):
    calls = _patch_get(monkeypatch, fake_get)
    return sync_models.fetch_endpoint_model({"url": "http://x/v1", "api_key": "k"}), calls


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


def test_cli_sglang_reasoning_parser_set_overrides_dev_false(monkeypatch):
    ep, _ = _fetch(monkeypatch, _sglang_server_info("qwen3"))
    assert ep["reasoning"] is True
    changes = _reasoning_changes({}, [False, False], ep_model=ep)
    assert changes["supports_reasoning"] == (None, True)


def test_cli_sglang_reasoning_parser_null_overrides_dev_true(monkeypatch):
    ep, _ = _fetch(monkeypatch, _sglang_server_info(None))
    assert ep["reasoning"] is False
    changes = _reasoning_changes({"supports_reasoning": True}, [True, True], ep_model=ep)
    assert changes["supports_reasoning"] == (True, False)


def test_cli_sglang_reasoning_parser_null_with_missing_key_is_no_change(monkeypatch):
    """Backend false + unset field: no change, even when models.dev says true."""
    ep, _ = _fetch(monkeypatch, _sglang_server_info(None))
    changes = _reasoning_changes({}, [True, True], ep_model=ep)
    assert "supports_reasoning" not in changes


def test_cli_old_sglang_get_server_info_falls_back_to_dev_consensus(monkeypatch):
    """/server_info 404 → /get_server_info without reasoning_parser: models.dev
    decides reasoning; the context and modality flags still come through."""
    def fake_get(url):
        if url == "http://x/get_server_info":
            return _Resp({"max_req_input_len": 500410, "served_model_name": "m",
                          "is_embedding": False, "enable_multimodal": False})
        return _Resp({}, ok=False)
    ep, calls = _fetch(monkeypatch, fake_get)
    assert calls[:2] == ["http://x/server_info?config_format=json", "http://x/get_server_info"]
    assert ep["backend"] == "sglang"
    assert ep["max_model_len"] == 500410
    assert "reasoning" not in ep

    changes = _reasoning_changes({}, [False, True, True], ep_model=ep)
    assert changes["supports_reasoning"] == (None, True)
    assert changes["context_window"] == (None, 500410)
    assert changes["input_modalities"] == (None, ["text"])


def test_cli_vllm_dev_mode_reasoning_parser_set(monkeypatch):
    ep, _ = _fetch(monkeypatch, _vllm_server_info(_Resp(_vllm_config("qwen3"))))
    assert ep == {"id": "m", "max_model_len": 32768, "backend": "vllm", "reasoning": True}
    changes = _reasoning_changes({}, [False, False], ep_model=ep)
    assert changes["supports_reasoning"] == (None, True)


def test_cli_vllm_dev_mode_reasoning_parser_empty(monkeypatch):
    ep, _ = _fetch(monkeypatch, _vllm_server_info(_Resp(_vllm_config(""))))
    assert ep["reasoning"] is False
    changes = _reasoning_changes({"supports_reasoning": True}, [True, True], ep_model=ep)
    assert changes["supports_reasoning"] == (True, False)


def test_cli_vllm_config_without_reasoning_parser_falls_back_to_dev(monkeypatch):
    ep, _ = _fetch(monkeypatch, _vllm_server_info(_Resp({"vllm_config": {"model_config": {}}})))
    assert ep["backend"] == "vllm"
    assert "reasoning" not in ep


def test_cli_vllm_server_info_404_falls_back_to_dev(monkeypatch):
    ep, _ = _fetch(monkeypatch, _vllm_server_info(_Resp({}, ok=False)))
    assert "backend" not in ep
    assert "reasoning" not in ep
    changes = _reasoning_changes({}, [False, True, True], ep_model=ep)
    assert changes["supports_reasoning"] == (None, True)


def test_cli_vllm_text_format_falls_back_to_dev(monkeypatch):
    """The default text format returns vllm_config as one string: unreadable, so models.dev decides."""
    ep, _ = _fetch(monkeypatch, _vllm_server_info(_Resp({"vllm_config": "model='m', reasoning_parser='qwen3', ..."})))
    assert "backend" not in ep
    assert "reasoning" not in ep
    changes = _reasoning_changes({"supports_reasoning": True}, [True, False, False], ep_model=ep)
    assert changes["supports_reasoning"] == (True, False)


def test_cli_server_info_non_json_falls_back_to_get_server_info(monkeypatch):
    class _BadJson(_Resp):
        def json(self):
            raise ValueError("not json")
    def fake_get(url):
        if url.endswith("/server_info?config_format=json"):
            return _BadJson({})
        if url.endswith("/get_server_info"):
            return _Resp({"max_req_input_len": 8192, "reasoning_parser": "deepseek-r1"})
        return _Resp({}, ok=False)
    ep, _ = _fetch(monkeypatch, fake_get)
    assert ep["max_model_len"] == 8192
    assert ep["reasoning"] is True


# ── CLI and web Update agree on supports_reasoning ────────────────────────────

@pytest.mark.parametrize("ep_model", [
    None,
    {"id": "m", "max_model_len": 32768, "backend": "sglang", "is_embedding": False,
     "enable_multimodal": None, "reasoning": True},
    {"id": "m", "max_model_len": 32768, "backend": "vllm", "reasoning": False},
    {"id": "m", "max_model_len": 32768},
])
@pytest.mark.parametrize("flags", [[False, True, True], [True, False], [True, False, False], [None]])
@pytest.mark.parametrize("current", [{}, {"supports_reasoning": True}, {"supports_reasoning": False}])
def test_cli_and_web_agree_on_reasoning(monkeypatch, ep_model, flags, current):
    dev_models = _reasoning_models(*flags)
    index = sync_models._build_price_index(dev_models)
    model_def = {"name": "m", "endpoints": [{"url": "http://x"}], **current}

    monkeypatch.setattr(model_sync, "fetch_endpoint_model", lambda ep: ep_model)
    monkeypatch.setattr(model_sync, "get_modelsdev", lambda: (dev_models, model_sync._build_price_index(dev_models)))
    monkeypatch.setattr(model_sync, "find_in_modelsdev", lambda *a, **k: dev_models[0])
    web = model_sync.sync_model(model_def)["updates"].get("supports_reasoning")

    cli_changes, _ = sync_models.compute_changes(model_def, ep_model, dev_models[0], index)
    cli = cli_changes.get("supports_reasoning", (None, None))[1]
    assert cli == web
