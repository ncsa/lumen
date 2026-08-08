"""Proxy param-forwarding allowlist and per-entity cache_salt injection (ncsa/lumen#36).

Covers two guarantees:
  1. A client can only forward allowlisted sampling params. Control params
     (`extra_body`/`extra_headers`), routing (`model`), and backend smuggling
     fields (`vllm_xargs`, `custom_logit_processor`, …) are dropped — closing the
     `extra_body` billing-bypass / model-swap vector.
  2. Every forwarded request carries a `cache_salt`: the client's value if given,
     otherwise a stable, unguessable per-entity salt, so prefix-cache reuse is
     isolated per user on shared backends.
"""
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest


def _expected_salt(app, entity_id):
    secret = app.config["SECRET_KEY"]
    return hmac.new(secret.encode(), f"entity:{entity_id}".encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# _forward_params — the allowlist + salt logic
# ---------------------------------------------------------------------------

def test_forward_passes_allowlisted_native_and_extension_params(app):
    from lumen.blueprints.api.routes import _forward_params
    data = {
        "model": "x", "messages": [], "stream": True,
        "temperature": 0.2, "reasoning_effort": "high",   # native
        "top_k": 40, "chat_template_kwargs": {"enable_thinking": True},  # extension
    }
    with app.app_context():
        out = _forward_params(data, 7)
    assert out["temperature"] == 0.2
    assert out["reasoning_effort"] == "high"
    assert out["extra_body"]["top_k"] == 40
    assert out["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}
    # model / messages / stream are never forwarded through the allowlist
    assert "model" not in out and "messages" not in out and "stream" not in out


def test_forward_drops_control_and_smuggling_params(app):
    from lumen.blueprints.api.routes import _forward_params
    data = {
        "temperature": 0.5,
        "extra_body": {"stream_options": {"include_usage": False}},  # billing-bypass vector
        "extra_headers": {"Authorization": "Bearer evil"},
        "stream_options": {"include_usage": False},
        "vllm_xargs": {"foo": "bar"},           # vLLM passthrough
        "custom_logit_processor": "os.system",  # SGLang arbitrary callable
        "lora_path": "/evil", "priority": 999, "input_ids": [1, 2, 3],
    }
    with app.app_context():
        out = _forward_params(data, 1)
    # Only temperature survives; extra_body holds nothing but the salt.
    assert out["temperature"] == 0.5
    assert set(out) == {"temperature", "extra_body"}
    assert set(out["extra_body"]) == {"cache_salt"}


def test_forward_honors_client_cache_salt(app):
    from lumen.blueprints.api.routes import _forward_params
    with app.app_context():
        out = _forward_params({"cache_salt": "team-shared-42"}, 1)
    assert out["extra_body"]["cache_salt"] == "team-shared-42"


@pytest.mark.parametrize("bad", [None, "", {"not": "a string"}, 123], ids=["absent", "empty", "dict", "int"])
def test_forward_derives_salt_when_client_salt_missing_or_invalid(app, bad):
    from lumen.blueprints.api.routes import _forward_params
    data = {} if bad is None else {"cache_salt": bad}
    with app.app_context():
        out = _forward_params(data, 55)
        assert out["extra_body"]["cache_salt"] == _expected_salt(app, 55)


def test_derived_salt_is_stable_and_entity_specific(app):
    from lumen.services.crypto import cache_salt_for_entity
    with app.app_context():
        assert cache_salt_for_entity(1) == cache_salt_for_entity(1)   # stable
        assert cache_salt_for_entity(1) != cache_salt_for_entity(2)   # per-entity


# ---------------------------------------------------------------------------
# API streaming path — the client cannot suppress usage or swap the model
# ---------------------------------------------------------------------------

class _ApiChunk:
    def __init__(self, usage=None):
        self.usage = usage

    def model_dump(self):
        return {"usage": None}


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    completion_tokens_details = None


@pytest.fixture
def billable_key(app, test_user, test_model_endpoint):
    """API key for test_user with an unlimited coin pool and a healthy endpoint."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.crypto import hash_api_key
        db.session.add(EntityLimit(entity_id=test_user["id"], max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.add(APIKey(entity_id=test_user["id"], name="k", key_hash=hash_api_key("lk_bill_tok"), active=True))
        db.session.commit()
    return "lk_bill_tok"


def test_client_extra_body_cannot_suppress_usage_or_swap_model(app, client, test_user, billable_key):
    from sqlalchemy import func, select
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = iter([_ApiChunk(usage=_Usage())])
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)

    with patch("lumen.blueprints.api.routes.openai.OpenAI", MagicMock(return_value=mock_client)):
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer lk_bill_tok"},
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                # Malicious: try to disable usage (dodge billing) and swap the model.
                "extra_body": {"stream_options": {"include_usage": False}, "model": "evil"},
                "vllm_xargs": {"foo": "bar"},
            },
        )
        resp.get_data()  # drain the SSE generator so billing runs

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    # The routed model is the endpoint's, not the smuggled one.
    assert kwargs["model"] == "dummy"
    # Usage stays on (server-forced); the client's extra_body was discarded.
    assert kwargs["stream_options"] == {"include_usage": True}
    assert set(kwargs["extra_body"]) == {"cache_salt"}
    with app.app_context():
        assert kwargs["extra_body"]["cache_salt"] == _expected_salt(app, test_user["id"])
        # Billing happened despite the suppression attempt.
        from lumen.models.model_stat import ModelStat
        from lumen.extensions import db
        total = db.session.scalar(select(func.sum(ModelStat.requests)).filter_by(entity_id=test_user["id"]))
    assert total == 1


# ---------------------------------------------------------------------------
# Web chat path — send_message_stream injects the per-entity salt
# ---------------------------------------------------------------------------

def _mock_openai_capture():
    """openai.OpenAI mock that records the create() kwargs and yields one usage chunk."""
    from tests.unit.test_llm_functions import _Chunk, _Usage as _U
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = iter([_Chunk(usage=_U())])
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=mock_client), mock_client


def test_chat_stream_injects_entity_cache_salt(app, test_user, test_model_endpoint):
    from lumen.services.llm import send_message_stream
    mock_cls, mock_client = _mock_openai_capture()
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(entity_id=test_user["id"], max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
        with patch("lumen.services.llm.openai.OpenAI", mock_cls):
            list(send_message_stream([], "test-model", entity_id=test_user["id"]))
        kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert kwargs["extra_body"] == {"cache_salt": _expected_salt(app, test_user["id"])}


def test_chat_stream_no_salt_without_entity(app, test_model_endpoint):
    from lumen.services.llm import send_message_stream
    mock_cls, mock_client = _mock_openai_capture()
    with app.app_context():
        with patch("lumen.services.llm.openai.OpenAI", mock_cls):
            list(send_message_stream([], "test-model"))
        # No entity → nothing to key a salt on → empty extra_body (no isolation claimed).
        assert mock_client.chat.completions.create.call_args.kwargs["extra_body"] == {}
