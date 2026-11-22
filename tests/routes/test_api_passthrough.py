"""Backend values that don't match the OpenAI SDK types pass through without a
Pydantic serializer warning (ncsa/lumen#693).

Backends such as vLLM can return a chat ``metadata`` whose values are lists, where the
SDK declares strings, and a base64 embedding is a string where the SDK declares floats.
The same applies to audio transcriptions, whose fields (such as ``logprobs``) can also
arrive in a shape the SDK does not declare.
Lumen relays the response unchanged, so serializing it must not log
``PydanticSerializationUnexpectedValue``. The fixtures use ``model_construct`` so the
off-type values reach the route as a backend would deliver them, unvalidated.
"""
import io
import json
import warnings
from unittest.mock import MagicMock, patch

import pytest
from openai.types import CreateEmbeddingResponse, Embedding
from openai.types.audio import Transcription
from openai.types.audio.transcription import UsageDuration
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.completion_usage import CompletionUsage
from openai.types.create_embedding_response import Usage as EmbeddingUsage

_METADATA = {"segments": [{"version": "default", "start": 0, "end": 450}]}
_FINGERPRINT = ["a", "b"]


def _usage():
    return CompletionUsage.model_construct(prompt_tokens=10, completion_tokens=5, total_tokens=15)


@pytest.fixture
def api_key(app, test_user, test_model_endpoint):
    """API key for test_user with an unlimited coin pool and a healthy endpoint."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.crypto import hash_api_key
        db.session.add(EntityLimit(entity_id=test_user["id"], max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.add(APIKey(entity_id=test_user["id"], name="k", key_hash=hash_api_key("lk_passthrough")))
        db.session.commit()
    return "lk_passthrough"


def _mock_openai(return_value):
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = return_value
    mock_client.embeddings.create.return_value = return_value
    mock_client.audio.transcriptions.create.return_value = return_value
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    return patch("lumen.blueprints.api.routes.openai.OpenAI", MagicMock(return_value=mock_client))


def _serializer_warnings(recorded):
    return [w for w in recorded if "PydanticSerializationUnexpectedValue" in str(w.message)]


def test_chat_completion_metadata_list_passes_through_without_warning(client, api_key):
    response = ChatCompletion.model_construct(
        id="chatcmpl-1", object="chat.completion", created=0, model="backend-model",
        choices=[], usage=_usage(), metadata=_METADATA,
    )
    with _mock_openai(response), warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    assert resp.get_json()["metadata"] == _METADATA
    assert _serializer_warnings(recorded) == []


def test_chat_completion_chunk_off_type_value_passes_through_without_warning(client, api_key):
    chunk = ChatCompletionChunk.model_construct(
        id="chatcmpl-1", object="chat.completion.chunk", created=0, model="backend-model",
        choices=[], usage=_usage(), system_fingerprint=_FINGERPRINT,
    )
    with _mock_openai(iter([chunk])), warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
        body = resp.get_data(as_text=True)  # drain the SSE generator inside the recorder
    assert resp.status_code == 200
    events = [json.loads(line[len("data: "):]) for line in body.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    assert [e["system_fingerprint"] for e in events] == [_FINGERPRINT]
    assert _serializer_warnings(recorded) == []


@pytest.mark.parametrize("encoding_format, vector", [
    ("base64", "AAAAAAAAgD8AAABA"),  # a str where the SDK declares list[float]
    ("float", [0.0, 1.0, 2.0]),
], ids=["base64", "float"])
def test_embedding_vector_passes_through_without_warning(client, api_key, encoding_format, vector):
    response = CreateEmbeddingResponse.model_construct(
        object="list", model="backend-model",
        data=[Embedding.model_construct(object="embedding", index=0, embedding=vector)],
        usage=EmbeddingUsage.model_construct(prompt_tokens=3, total_tokens=3),
    )
    with _mock_openai(response), warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        resp = client.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "test-model", "input": "hi", "encoding_format": encoding_format},
        )
    assert resp.status_code == 200
    assert resp.get_json()["data"][0]["embedding"] == vector
    assert _serializer_warnings(recorded) == []


def test_anthropic_message_metadata_list_without_warning(client, api_key):
    # translate_response drops metadata, so only the absence of the warning is checked.
    response = ChatCompletion.model_construct(
        id="chatcmpl-1", object="chat.completion", created=0, model="backend-model",
        choices=[], usage=_usage(), metadata=_METADATA,
    )
    with _mock_openai(response), warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        resp = client.post(
            "/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            json={"model": "test-model", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    assert _serializer_warnings(recorded) == []


def test_audio_transcription_off_type_value_passes_through_without_warning(client, api_key, audio_model):
    response = Transcription.model_construct(
        text="hi", logprobs="not-a-list",  # a str where the SDK declares a list
        usage=UsageDuration.model_construct(type="duration", seconds=10),
    )
    with _mock_openai(response), warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        resp = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            data={"model": "test-model", "file": (io.BytesIO(b"audio"), "a.wav")},
            content_type="multipart/form-data",
        )
    assert resp.status_code == 200
    assert resp.get_json()["logprobs"] == "not-a-list"
    assert _serializer_warnings(recorded) == []
