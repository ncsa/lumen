"""Tests for the /v1/audio/transcriptions and /v1/audio/translations endpoints."""
from http import HTTPStatus
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def api_key(app, test_user):
    """Create an active API key for test_user. Returns (token, key_id)."""
    token = "lk_test_audio_token"
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        from lumen.services.crypto import hash_api_key
        ak = APIKey(
            entity_id=test_user["id"],
            name="audio-key",
            key_hash=hash_api_key(token),
            active=True,
        )
        db.session.add(ak)
        db.session.commit()
        return token, ak.id


def _set_audio_rate(app, model_id, rate):
    from lumen.extensions import db
    from lumen.models.model_config import ModelConfig
    mc = db.session.get(ModelConfig, model_id)
    mc.audio_cost_per_hour = rate
    db.session.commit()


def _grant_finite_pool(app, entity_id, coins=100):
    """Grant a finite coin pool so subtraction is observable."""
    from lumen.extensions import db
    from lumen.models.entity_limit import EntityLimit
    db.session.add(EntityLimit(
        entity_id=entity_id, max_coins=coins, refresh_coins=0, starting_coins=coins,
    ))
    db.session.commit()


class _FakeResponse:
    """Stand-in for the OpenAI SDK Transcription/Translation object."""
    def __init__(self, payload):
        self._payload = payload

    def model_dump(self):
        return self._payload


def _mock_openai(payload):
    """Return a patched openai.OpenAI whose audio.*.create returns the payload."""
    mock_client = MagicMock()
    mock_client.audio.transcriptions.create = MagicMock(return_value=_FakeResponse(payload))
    mock_client.audio.translations.create = MagicMock(return_value=_FakeResponse(payload))
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=mock_client)


def _audio_data(model="test-model", **extra):
    data = {"model": model, "file": (BytesIO(b"fake-audio-bytes"), "sample.flac")}
    data.update(extra)
    return data


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_transcriptions_missing_auth_400(client):
    resp = client.post(
        "/v1/audio/transcriptions",
        data=_audio_data(),
        content_type="multipart/form-data",
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_transcriptions_invalid_token_401(client):
    resp = client.post(
        "/v1/audio/transcriptions",
        headers={"Authorization": "Bearer not-real"},
        data=_audio_data(),
        content_type="multipart/form-data",
    )
    assert resp.status_code == HTTPStatus.UNAUTHORIZED


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_transcriptions_missing_file_400(client, api_key):
    token, _ = api_key
    resp = client.post(
        "/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {token}"},
        data={"model": "test-model"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_transcriptions_missing_model_400(client, api_key):
    token, _ = api_key
    resp = client.post(
        "/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {token}"},
        data={"file": (BytesIO(b"x"), "a.flac")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_transcription_duration_billing(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    token, ak_id = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"], coins=100)
        _set_audio_rate(app, test_model["id"], 0.6)  # $0.6/min

    payload = {
        "task": "transcribe", "duration": 10.44, "text": "hello world",
        "segments": [], "usage": {"type": "duration", "seconds": 11},
    }
    with patch("lumen.blueprints.api.routes.openai.OpenAI", _mock_openai(payload)):
        resp = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {token}"},
            data=_audio_data(),
            content_type="multipart/form-data",
        )
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["text"] == "hello world"

    expected_cost = round(11 / 3600 * 0.6, 6)
    with app.app_context():
        from lumen.extensions import db
        from sqlalchemy import select
        from lumen.models.request_log import RequestLog
        from lumen.models.model_stat import ModelStat
        from lumen.models.entity_stat import EntityStat
        from lumen.models.api_key import APIKey
        from lumen.models.entity_balance import EntityBalance

        log = db.session.execute(select(RequestLog)).scalar_one()
        assert log.audio_seconds == 11
        assert log.input_tokens == 0
        assert log.output_tokens == 0
        assert float(log.cost) == expected_cost
        assert log.source == "api"

        ms = db.session.execute(select(ModelStat)).scalar_one()
        assert ms.audio_seconds == 11
        es = db.session.execute(select(EntityStat)).scalar_one()
        assert es.audio_seconds == 11

        ak = db.session.get(APIKey, ak_id)
        assert ak.audio_seconds == 11
        assert float(ak.cost) == expected_cost

        bal = db.session.execute(select(EntityBalance)).scalar_one()
        assert float(bal.coins_left) == round(100 - expected_cost, 6)


def test_transcription_token_billing(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"], coins=100)
        _set_audio_rate(app, test_model["id"], 0.6)

    payload = {
        "text": "hi",
        "usage": {"type": "tokens", "prompt_tokens": 1000, "completion_tokens": 500},
    }
    with patch("lumen.blueprints.api.routes.openai.OpenAI", _mock_openai(payload)):
        resp = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {token}"},
            data=_audio_data(),
            content_type="multipart/form-data",
        )
    assert resp.status_code == HTTPStatus.OK

    # input_cost=1.0/M, output_cost=2.0/M (from test_model fixture)
    expected_cost = round(1000 * 1.0 / 1_000_000 + 500 * 2.0 / 1_000_000, 6)
    with app.app_context():
        from lumen.extensions import db
        from sqlalchemy import select
        from lumen.models.request_log import RequestLog
        log = db.session.execute(select(RequestLog)).scalar_one()
        assert log.audio_seconds == 0
        assert log.input_tokens == 1000
        assert log.output_tokens == 500
        assert float(log.cost) == expected_cost


def test_translation_duration_billing(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"], coins=100)
        _set_audio_rate(app, test_model["id"], 1.2)

    payload = {
        "task": "translate", "duration": 5.0, "text": "translated",
        "usage": {"type": "duration", "seconds": 30},
    }
    with patch("lumen.blueprints.api.routes.openai.OpenAI", _mock_openai(payload)):
        resp = client.post(
            "/v1/audio/translations",
            headers={"Authorization": f"Bearer {token}"},
            data=_audio_data(),
            content_type="multipart/form-data",
        )
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["text"] == "translated"

    expected_cost = round(30 / 3600 * 1.2, 6)
    with app.app_context():
        from lumen.extensions import db
        from sqlalchemy import select
        from lumen.models.request_log import RequestLog
        log = db.session.execute(select(RequestLog)).scalar_one()
        assert log.audio_seconds == 30
        assert float(log.cost) == expected_cost


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_transcription_coin_budget_exhausted_429(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.entity_balance import EntityBalance
        from datetime import datetime, timezone
        db.session.add(EntityLimit(
            entity_id=test_user["id"], max_coins=100, refresh_coins=0, starting_coins=100,
        ))
        db.session.add(EntityBalance(
            entity_id=test_user["id"], coins_left=0,
            last_refill_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.session.commit()

    resp = client.post(
        "/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {token}"},
        data=_audio_data(),
        content_type="multipart/form-data",
    )
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS


def test_transcription_no_usage_zero_cost(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"], coins=100)
        _set_audio_rate(app, test_model["id"], 0.6)

    payload = {"text": "no usage here"}  # no usage object
    with patch("lumen.blueprints.api.routes.openai.OpenAI", _mock_openai(payload)):
        resp = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {token}"},
            data=_audio_data(),
            content_type="multipart/form-data",
        )
    assert resp.status_code == HTTPStatus.OK

    with app.app_context():
        from lumen.extensions import db
        from sqlalchemy import select
        from lumen.models.request_log import RequestLog
        log = db.session.execute(select(RequestLog)).scalar_one()
        assert log.audio_seconds == 0
        assert float(log.cost) == 0.0
