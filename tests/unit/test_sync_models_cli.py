"""CLI sync_models.py mirrors the SGLang modality rules of lumen.services.model_sync."""

import sync_models

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
