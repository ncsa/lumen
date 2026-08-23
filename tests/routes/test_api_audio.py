"""Tests for the /v1/audio/transcriptions and /v1/audio/translations endpoints."""
from http import HTTPStatus
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from tests.routes.test_api_auth import fresh_rate_limit

# Re-exported so pytest resolves the fixture by name in this module's tests.
__all__ = ["fresh_rate_limit"]


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
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.api_key import APIKey
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_stat import EntityStat
        from lumen.models.model_stat import ModelStat
        from lumen.models.request_log import RequestLog

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
        from sqlalchemy import select

        from lumen.extensions import db
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
        from sqlalchemy import select

        from lumen.extensions import db
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
        from datetime import datetime, timezone

        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
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
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.request_log import RequestLog
        log = db.session.execute(select(RequestLog)).scalar_one()
        assert log.audio_seconds == 0
        assert float(log.cost) == 0.0


# ---------------------------------------------------------------------------
# Request timing columns
# ---------------------------------------------------------------------------

_MAX_PLAUSIBLE_SPAN = 60 * 60  # seconds; a test request takes milliseconds
_QUEUE_WAIT = 0.05             # seconds of admission wait to stamp T0 behind


def _bridge_environ():
    """The environ keys ``asgi.py`` publishes; the test client bypasses it.

    ``lumen.queue_wait`` is left out on purpose — the real before_request hook
    derives it from the arrival mark.
    """
    import time
    from datetime import datetime, timezone

    from lumen.services.wsgi_disconnect import SendBlocked
    return {
        "lumen.t0_monotonic": time.monotonic() - _QUEUE_WAIT,
        "lumen.started_at": datetime.now(timezone.utc),
        "lumen.send_blocked": SendBlocked(),
    }


def test_transcription_records_timing_columns(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    """The audio path is the fourth billing site and the easiest one to forget.

    Its preflight is not database contention: it includes reading the whole
    multipart upload, which for a large file dominates.
    """
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"], coins=100)
        _set_audio_rate(app, test_model["id"], 0.6)

    payload = {"text": "hello", "usage": {"type": "duration", "seconds": 11}}
    with patch("lumen.blueprints.api.routes.openai.OpenAI", _mock_openai(payload)):
        resp = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {token}"},
            data=_audio_data(),
            content_type="multipart/form-data",
            environ_base=_bridge_environ(),
        )
    assert resp.status_code == HTTPStatus.OK

    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.request_log import RequestLog
        log = db.session.execute(select(RequestLog)).scalar_one()
    assert log.started_at is not None
    assert _QUEUE_WAIT <= log.queue_wait < _MAX_PLAUSIBLE_SPAN
    assert 0 <= log.preflight < _MAX_PLAUSIBLE_SPAN
    # Nothing streams, so the first chunk is the whole transcription.
    assert log.ttft == log.ttft_visible == log.duration
    assert log.send_blocked == 0.0  # a holder is present; the view never blocks
    assert log.outcome == "ok"


def test_transcription_without_the_bridge_records_nulls(
    app, client, test_user, test_model, test_model_endpoint, api_key,
):
    """Absent marks are NULL — "not measured", not "measured as zero"."""
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"], coins=100)
        _set_audio_rate(app, test_model["id"], 0.6)

    payload = {"text": "hello", "usage": {"type": "duration", "seconds": 11}}
    with patch("lumen.blueprints.api.routes.openai.OpenAI", _mock_openai(payload)):
        resp = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {token}"},
            data=_audio_data(),
            content_type="multipart/form-data",
        )
    assert resp.status_code == HTTPStatus.OK

    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.request_log import RequestLog
        log = db.session.execute(select(RequestLog)).scalar_one()
    assert log.started_at is None
    assert log.queue_wait is None
    assert log.preflight is None
    assert log.send_blocked is None
    assert log.outcome == "ok"


# ---------------------------------------------------------------------------
# Rejection taxonomy on the /v1 surface
# ---------------------------------------------------------------------------

def _recorder(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "lumen.blueprints.metrics.middleware.observe_rejection",
        lambda reason, source, model="": recorded.append((reason, source, model)),
    )
    return recorded


def _exhaust_budget(app, entity_id, refresh_coins, refilled_minutes_ago=30):
    from datetime import timedelta

    from lumen.extensions import db
    from lumen.models.entity_balance import EntityBalance
    from lumen.models.entity_limit import EntityLimit
    from lumen.timeutils import utcnow
    db.session.add(EntityLimit(
        entity_id=entity_id, max_coins=100, refresh_coins=refresh_coins, starting_coins=100,
    ))
    db.session.add(EntityBalance(
        entity_id=entity_id, coins_left=0,
        last_refill_at=utcnow() - timedelta(minutes=refilled_minutes_ago),
    ))
    db.session.commit()


def _transcribe(client, token):
    return client.post(
        "/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {token}"},
        data=_audio_data(),
        content_type="multipart/form-data",
    )


def test_coin_exhaustion_is_insufficient_quota_not_rate_limited(
    app, client, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit,
):
    """Both conditions answer 429 and they need opposite reactions.

    The limiter means "retry shortly"; an exhausted budget means "stop until it
    refills". Only the OpenAI type/code tells the two apart in the body, and the
    SDK branches on it.
    """
    token, _ = api_key
    with app.app_context():
        _exhaust_budget(app, test_user["id"], refresh_coins=10)

    resp = _transcribe(client, token)

    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    err = resp.get_json()["error"]
    assert err["type"] == "insufficient_quota"
    assert err["code"] == "insufficient_quota"
    # Distinguishable from the limiter's 429, which uses the other pair.
    assert err["code"] != "rate_limit_exceeded"


def test_coin_exhaustion_retry_after_is_derived_from_the_refill(
    app, client, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit,
):
    """The refiller credits an hour after the last refill, so the wait is knowable.

    It must shrink as that hour is used up — a constant would send every client
    back at the same instant, which is the storm Retry-After exists to prevent.
    """
    token, _ = api_key
    with app.app_context():
        _exhaust_budget(app, test_user["id"], refresh_coins=10, refilled_minutes_ago=30)

    half_way = int(_transcribe(client, token).headers["Retry-After"])
    assert 29 * 60 <= half_way <= 30 * 60

    from datetime import timedelta

    from sqlalchemy import select

    from lumen.extensions import db
    from lumen.models.entity_balance import EntityBalance
    from lumen.timeutils import utcnow
    with app.app_context():
        bal = db.session.execute(
            select(EntityBalance).filter_by(entity_id=test_user["id"])
        ).scalar_one()
        bal.last_refill_at = utcnow() - timedelta(minutes=10)
        db.session.commit()

    later = int(_transcribe(client, token).headers["Retry-After"])
    assert 49 * 60 <= later <= 50 * 60
    assert later > half_way


def test_no_retry_after_when_the_budget_never_refills(
    app, client, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit,
):
    """A pool with refresh_coins=0 has no next refill; a header would be a lie."""
    token, _ = api_key
    with app.app_context():
        _exhaust_budget(app, test_user["id"], refresh_coins=0)

    resp = _transcribe(client, token)
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert "Retry-After" not in resp.headers


def test_coin_exhaustion_is_counted_with_its_model(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit,
):
    """Unlike the limiter's rejection, this one knows which model was asked for."""
    token, _ = api_key
    with app.app_context():
        _exhaust_budget(app, test_user["id"], refresh_coins=10)
    recorded = _recorder(monkeypatch)

    assert _transcribe(client, token).status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert ("coin_budget", "api", test_model["model_name"]) in recorded


def test_no_healthy_endpoint_is_counted(
    app, client, monkeypatch, test_user, test_model, api_key, fresh_rate_limit,
):
    """Preflight passed and there was still nowhere to send it."""
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    recorded = _recorder(monkeypatch)

    resp = _transcribe(client, token)

    assert resp.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert ("no_healthy_endpoint", "api", test_model["model_name"]) in recorded


def test_a_broken_counter_never_turns_a_rejection_into_a_500(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit,
):
    """Instrumentation is not allowed to escalate a clean refusal."""
    def boom(*a, **kw):
        raise RuntimeError("prometheus is unhappy")

    monkeypatch.setattr("lumen.blueprints.metrics.middleware.observe_rejection", boom)
    token, _ = api_key
    with app.app_context():
        _exhaust_budget(app, test_user["id"], refresh_coins=10)

    resp = _transcribe(client, token)
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert resp.get_json()["error"]["code"] == "insufficient_quota"


def test_a_broken_counter_never_turns_a_403_into_a_500(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key, fresh_rate_limit,
):
    def boom(*a, **kw):
        raise RuntimeError("prometheus is unhappy")

    monkeypatch.setattr("lumen.blueprints.metrics.middleware.observe_rejection", boom)
    token, _ = api_key
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from tests.conftest import set_model_owner
        _grant_finite_pool(app, test_user["id"])
        owner = Entity(entity_type="user", email="audio-owner@example.com", name="Owner", active=True)
        db.session.add(owner)
        db.session.commit()
        set_model_owner(test_model["id"], owner.id)

    assert _transcribe(client, token).status_code == HTTPStatus.FORBIDDEN


def test_a_broken_counter_never_turns_a_503_into_a_500(
    app, client, monkeypatch, test_user, test_model, api_key, fresh_rate_limit,
):
    def boom(*a, **kw):
        raise RuntimeError("prometheus is unhappy")

    monkeypatch.setattr("lumen.blueprints.metrics.middleware.observe_rejection", boom)
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])

    assert _transcribe(client, token).status_code == HTTPStatus.SERVICE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Live state (in-flight accounting) for the three /v1 paths
#
# admit() belongs in the view, after _preflight has passed every rejection;
# release() in the streaming generator's finally, and in a finally around the
# whole request on the two non-streaming paths. Every exit path must leave the
# in-flight count at zero.
# ---------------------------------------------------------------------------

from tests.routes.test_api_auth import (  # noqa: E402 - grouped with the section it serves
    _allow_model,
    _capturing_openai,
    _chat_post,
    _fake_openai,
    _NonStreamResponse,
    _UsageChunk,
)
from tests.routes.test_chat_routes import _recording_live_state  # noqa: E402


class _FakeAudio:
    """Upstream transcription response with a billable duration."""

    def model_dump(self):
        return {"text": "hi", "usage": {"type": "duration", "seconds": 10}}


def _live_state(monkeypatch):
    from lumen.blueprints.api import routes
    return _recording_live_state(monkeypatch, routes)


# ── /v1/chat/completions, streaming ───────────────────────────────────────────

def test_api_stream_admits_once_and_releases_once(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    state = _live_state(monkeypatch)
    _fake_openai(monkeypatch, routes, [_UsageChunk()])

    resp = _chat_post(client, token, test_model["model_name"], True)
    assert resp.status_code == HTTPStatus.OK
    assert resp.is_streamed
    events = iter(resp.response)
    try:
        next(events)  # suspended mid-stream: still in flight
        assert state.admits == 1
        assert state.inflight == 1
        for _ in events:
            pass
    finally:
        resp.close()

    assert state.releases == 1
    assert state.inflight == 0


def test_api_stream_releases_on_client_disconnect(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    import threading

    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    state = _live_state(monkeypatch)
    disconnected = threading.Event()

    def chunks():
        yield _UsageChunk()
        disconnected.set()  # client vanishes mid-stream
        yield _UsageChunk()

    _fake_openai(monkeypatch, routes, chunks())
    monkeypatch.setattr(routes, "client_disconnect_event", lambda: disconnected)

    resp = _chat_post(client, token, test_model["model_name"], True)
    b"".join(resp.response)
    resp.close()
    assert state.admits == 1
    assert state.releases == 1
    assert state.inflight == 0


def test_api_stream_releases_when_the_body_is_abandoned(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    """Closing the response mid-stream (GeneratorExit) releases the ticket."""
    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    state = _live_state(monkeypatch)
    _fake_openai(monkeypatch, routes, [_UsageChunk(), _UsageChunk(), _UsageChunk()])

    resp = _chat_post(client, token, test_model["model_name"], True)
    events = iter(resp.response)
    next(events)
    assert state.inflight == 1
    resp.close()
    assert state.releases == 1
    assert state.inflight == 0


def test_api_stream_releases_when_upstream_fails(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    from lumen.blueprints.api import routes

    def boom(**kwargs):
        raise RuntimeError("upstream is down")

    token, _ = api_key
    _allow_model(app, test_user, test_model)
    state = _live_state(monkeypatch)
    _capturing_openai(monkeypatch, routes, boom)

    resp = _chat_post(client, token, test_model["model_name"], True)
    body = b"".join(resp.response)
    resp.close()
    assert b'"error"' in body
    assert state.admits == 1
    assert state.releases == 1
    assert state.inflight == 0


# ── /v1/chat/completions, non-streaming ───────────────────────────────────────

def test_api_non_stream_admits_once_and_releases_once(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    """No generator here, so the request is live for the upstream call and the
    billing that follows it."""
    from lumen.blueprints.api import routes
    token, _ = api_key
    _allow_model(app, test_user, test_model)
    state = _live_state(monkeypatch)
    observed = []

    def create(**kwargs):
        observed.append(state.inflight)
        return _NonStreamResponse()

    _capturing_openai(monkeypatch, routes, create)

    resp = _chat_post(client, token, test_model["model_name"], False)
    assert resp.status_code == HTTPStatus.OK
    assert observed == [1], "the request was not in flight during the upstream call"
    assert (state.admits, state.releases) == (1, 1)
    assert state.inflight == 0


def test_api_non_stream_releases_when_upstream_fails(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    from lumen.blueprints.api import routes

    def boom(**kwargs):
        raise RuntimeError("upstream is down")

    token, _ = api_key
    _allow_model(app, test_user, test_model)
    state = _live_state(monkeypatch)
    _capturing_openai(monkeypatch, routes, boom)

    resp = _chat_post(client, token, test_model["model_name"], False)
    assert resp.status_code >= HTTPStatus.BAD_REQUEST
    assert (state.admits, state.releases) == (1, 1)
    assert state.inflight == 0


def test_api_non_stream_releases_when_billing_raises(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    """The finally covers the billing too, so a failure there still releases."""
    from lumen.blueprints.api import routes

    def boom(*a, **kw):
        raise RuntimeError("billing blew up")

    token, _ = api_key
    _allow_model(app, test_user, test_model)
    state = _live_state(monkeypatch)
    _capturing_openai(monkeypatch, routes, lambda **kwargs: _NonStreamResponse())
    monkeypatch.setattr(routes, "update_stats", boom)

    with pytest.raises(RuntimeError):
        _chat_post(client, token, test_model["model_name"], False)
    assert (state.admits, state.releases) == (1, 1)
    assert state.inflight == 0


def test_rejected_api_request_is_never_admitted(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    """An exhausted coin budget is refused inside _preflight, before admit."""
    token, _ = api_key
    with app.app_context():
        _exhaust_budget(app, test_user["id"], refresh_coins=10)
    state = _live_state(monkeypatch)

    resp = _chat_post(client, token, test_model["model_name"], False)
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert state.admits == 0
    assert state.inflight == 0


# ── /v1/audio/transcriptions ──────────────────────────────────────────────────

def test_audio_admits_once_and_releases_once(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    from lumen.blueprints.api import routes
    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
        _set_audio_rate(app, test_model["id"], 0.6)
    state = _live_state(monkeypatch)
    observed = []

    def create(**kwargs):
        observed.append(state.inflight)
        return _FakeAudio()

    _capturing_openai(monkeypatch, routes, create)

    resp = _transcribe(client, token)
    assert resp.status_code == HTTPStatus.OK
    assert observed == [1], "the request was not in flight during the upstream call"
    assert (state.admits, state.releases) == (1, 1)
    assert state.inflight == 0


def test_audio_releases_when_upstream_fails(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    from lumen.blueprints.api import routes

    def boom(**kwargs):
        raise RuntimeError("upstream is down")

    token, _ = api_key
    with app.app_context():
        _grant_finite_pool(app, test_user["id"])
    state = _live_state(monkeypatch)
    _capturing_openai(monkeypatch, routes, boom)

    resp = _transcribe(client, token)
    assert resp.status_code >= HTTPStatus.BAD_REQUEST
    assert (state.admits, state.releases) == (1, 1)
    assert state.inflight == 0


def test_rejected_audio_request_is_never_admitted(
    app, client, monkeypatch, test_user, test_model, test_model_endpoint, api_key,
    fresh_rate_limit,
):
    token, _ = api_key
    with app.app_context():
        _exhaust_budget(app, test_user["id"], refresh_coins=10)
    state = _live_state(monkeypatch)

    resp = _transcribe(client, token)
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert state.admits == 0
    assert state.inflight == 0
