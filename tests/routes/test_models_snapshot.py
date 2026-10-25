"""``/v1/models`` and ``/v1/models/<id>`` served from the model snapshot.

The response-equality tests compare each response with the one the previous
implementation built from live queries; ``_legacy_list``/``_legacy_get`` are
that implementation, kept here verbatim apart from taking their inputs as
arguments instead of from the request.
"""
import json
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import selectinload

from lumen.blueprints.api.routes import _ack_fields, _model_dict
from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.models.entity import Entity
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.model_alias import ModelAlias
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.request_log import RequestLog
from lumen.services import health, model_cache, request_rates
from lumen.services.crypto import hash_api_key
from lumen.services.llm import (
    bulk_model_access_statuses,
    consent_rows,
    get_model_access_status,
    get_pool_limit,
)
from lumen.services.model_resolver import resolve_model_config

NOW = datetime(2026, 10, 20, 12, 0, 15, tzinfo=timezone.utc)
MONITOR = "monitor-snapshot-token"
USER_KEY = "lk_test_snapshot_key"
ACKED = datetime(2026, 9, 1, 8, 30)


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch):
    monkeypatch.setattr(request_rates, "_now", lambda: NOW)


@pytest.fixture
def monitor_token(app):
    app.config["YAML_DATA"] = {**app.config.get("YAML_DATA", {}),
                               "api": {"monitoring": {"token": MONITOR}}}
    yield MONITOR
    app.config["YAML_DATA"].pop("api", None)


def _model(name, **fields):
    m = ModelConfig(model_name=name, input_cost_per_million=1.5, output_cost_per_million=3.0, **fields)
    db.session.add(m)
    db.session.flush()
    return m


def _endpoint(model, healthy=True, active=True, model_name=None):
    db.session.add(ModelEndpoint(model_config_id=model.id, url=f"http://upstream/{model.model_name}",
                                 api_key="k", model_name=model_name, healthy=healthy, active=active))


def _log(model, ago, inputs=1, outputs=2):
    db.session.add(RequestLog(time=NOW - ago, model_config_id=model.id, source="api",
                              input_tokens=inputs, output_tokens=outputs))


@pytest.fixture
def catalog(app, test_user):
    """One model per case the listing distinguishes, with requests on each."""
    with app.app_context():
        other = Entity(entity_type="user", email="other@example.com", name="Other", initials="OT", active=True)
        db.session.add(other)
        db.session.flush()
        uid = test_user["id"]
        db.session.add(APIKey(entity_id=uid, name="snapshot-key", key_hash=hash_api_key(USER_KEY)))
        db.session.add(EntityLimit(entity_id=uid, max_coins=-2, refresh_coins=0, starting_coins=0))

        plain = _model(
            "plain", description="A plain model", context_window=8192, max_output_tokens=1024,
            supports_function_calling=True, supports_reasoning=False, knowledge_cutoff="2025-06",
            input_modalities=["text", "image"], output_modalities=["text"],
        )
        _endpoint(plain, model_name="upstream/plain")
        _endpoint(plain, active=False)  # retired: in no listing
        db.session.add_all([ModelAlias(alias="plain-b", model_config_id=plain.id),
                            ModelAlias(alias="plain-a", model_config_id=plain.id)])

        degraded = _model("degraded")
        _endpoint(degraded)
        _endpoint(degraded, healthy=False)

        down = _model("down")
        _endpoint(down, healthy=False)

        _model("no-endpoints")

        needs_ack = _model("needs-ack", needs_ack=True, ack_message="Read this first.")
        _endpoint(needs_ack)

        early = _model("early", early_access=True)
        _endpoint(early)
        db.session.add(EntityModelConsent(entity_id=uid, model_config_id=early.id, early_access_at=ACKED))

        # Requirements removed after the user acknowledged: acknowledged_at is still reported.
        removed = _model("requirements-removed")
        _endpoint(removed)
        db.session.add(EntityModelConsent(entity_id=uid, model_config_id=removed.id, consented_at=ACKED))

        private = _model("private", owner_entity_id=other.id)
        _endpoint(private)
        own = _model("own", owner_entity_id=uid)
        _endpoint(own)

        disabled = _model("disabled", disabled=True)
        _endpoint(disabled)

        for i, m in enumerate((plain, degraded, needs_ack, private)):
            _log(m, timedelta(seconds=30), inputs=i + 1, outputs=10 * (i + 1))
            _log(m, timedelta(hours=2), inputs=100, outputs=1000)
        db.session.commit()
    return ["plain", "plain-a", "plain-b", "degraded", "down", "no-endpoints", "needs-ack",
            "early", "requirements-removed", "private", "own", "disabled", "missing"]


def _legacy_list(entity_id, tokens=False, include_unacked=False):
    """The previous ``list_models``, run against live queries."""
    configs = db.session.execute(
        select(ModelConfig).where(ModelConfig.active).options(selectinload(ModelConfig.aliases))
    ).scalars().all()
    eps_by_model: dict = {}
    for ep in db.session.execute(
        select(ModelEndpoint).where(ModelEndpoint.model_config_id.in_([c.id for c in configs]), ModelEndpoint.active.is_(True))
    ).scalars().all():
        eps_by_model.setdefault(ep.model_config_id, []).append(ep)
    rates = request_rates.rates()
    tokens = request_rates.token_windows() if tokens else None
    if entity_id is None:
        permitted = configs
    else:
        access_statuses = bulk_model_access_statuses(entity_id, [c.id for c in configs])
        pool = get_pool_limit(entity_id)
        permitted = [
            c for c in configs
            if pool is not None
            and access_statuses.get(c.id, "allowed") != "blocked"
        ]
        rows = consent_rows(entity_id, [c.id for c in permitted])
    data = []
    for c in permitted:
        ack = _ack_fields(c, rows.get(c.id)) if entity_id is not None else None
        open_tags = ack[0] if ack is not None else []
        if open_tags and not include_unacked:
            continue
        entry = _model_dict(c, rates, eps_by_model.get(c.id, []), ack, tokens)
        data.append(entry)
        for alias in sorted(a.alias for a in c.aliases):
            alias_entry = dict(entry)
            alias_entry["id"] = alias
            alias_entry["parent"] = c.model_name
            data.append(alias_entry)
    return HTTPStatus.OK, {"object": "list", "data": data}


def _legacy_get(model_id, entity_id, tokens=False):
    """The previous ``get_model``, run against live queries."""
    config = resolve_model_config(model_id)
    if not config:
        return HTTPStatus.NOT_FOUND, None
    if entity_id is not None:
        if get_model_access_status(entity_id, config.id) == "blocked" or get_pool_limit(entity_id) is None:
            return HTTPStatus.NOT_FOUND, None
    eps = db.session.execute(
        select(ModelEndpoint).where(ModelEndpoint.model_config_id == config.id, ModelEndpoint.active.is_(True))
    ).scalars().all()
    rates = request_rates.rates()
    ack = None
    if entity_id is not None:
        row = db.session.execute(select(EntityModelConsent).filter_by(entity_id=entity_id, model_config_id=config.id)).scalar_one_or_none()
        ack = _ack_fields(config, row)
    d = _model_dict(config, rates, list(eps), ack, request_rates.token_windows() if tokens else None)
    if model_id != config.model_name:
        d["id"] = model_id
        d["parent"] = config.model_name
    return HTTPStatus.OK, d


def _as_json(app, body):
    return json.loads(app.json.dumps(body))


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


CASES = {
    "user key": ("user", ""),
    "monitor": ("monitor", ""),
    "monitor tokens": ("monitor", "?tokens=true"),
    "user include_unacknowledged": ("user", "?include_unacknowledged=true"),
    "user tokens (ignored)": ("user", "?tokens=true"),
}


@pytest.mark.parametrize("case", CASES)
def test_responses_equal_the_live_query_responses(app, client, test_user, monitor_token, catalog, case):
    who, query = CASES[case]
    token = USER_KEY if who == "user" else monitor_token
    entity_id = test_user["id"] if who == "user" else None
    tokens = who == "monitor" and "tokens" in query
    include_unacked = "include_unacknowledged" in query

    with app.app_context():
        expected_list = _as_json(app, _legacy_list(entity_id, tokens, include_unacked)[1])
        expected = {name: _legacy_get(name, entity_id, tokens) for name in catalog}
        expected = {name: (status, _as_json(app, body)) for name, (status, body) in expected.items()}

    resp = client.get(f"/v1/models{query}", headers=_auth(token))
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json() == expected_list
    # The cases the list must cover are actually in it.
    ids = {m["id"] for m in expected_list["data"]}
    assert {"plain", "plain-a", "plain-b", "degraded", "down", "no-endpoints", "requirements-removed"} <= ids
    assert ("private" in ids) == (who == "monitor")
    assert ("needs-ack" in ids) == (who == "monitor" or include_unacked)

    for name, (status, body) in expected.items():
        resp = client.get(f"/v1/models/{name}{query}", headers=_auth(token))
        assert resp.status_code == status, name
        if status == HTTPStatus.OK:
            assert resp.get_json() == body, name


def test_listing_reports_the_cases_it_covers(client, monitor_token, catalog):
    """Guards the fixture: the equality test means little if the cases collapse."""
    listed = {m["id"]: m for m in client.get("/v1/models", headers=_auth(USER_KEY)).get_json()["data"]}
    assert listed["degraded"]["status"] == "degraded"
    assert listed["degraded"]["instances"] == {"configured": 2, "healthy": 1}
    assert listed["down"]["status"] == "down"
    assert listed["no-endpoints"]["instances"] == {"configured": 0, "healthy": 0}
    assert listed["plain"]["root"] == "upstream/plain"
    assert listed["plain"]["input_modalities"] == ["text", "image"]
    assert listed["plain"]["requests"] == {"last_minute": 1, "last_hour": 1, "last_day": 2}
    assert listed["plain-a"]["parent"] == "plain"
    assert listed["requirements-removed"]["acknowledged_at"] == ACKED.isoformat() + "Z"
    assert listed["early"]["acknowledged_at"] == ACKED.isoformat() + "Z"
    assert "private" not in listed and "disabled" not in listed

    monitor = {m["id"]: m for m in client.get("/v1/models?tokens=true", headers=_auth(monitor_token)).get_json()["data"]}
    assert monitor["degraded"]["input_tokens"]["last_minute"] == 2


# ── Query counts ─────────────────────────────────────────────────────────────

def _statements(app, fn):
    with app.app_context():
        engine = db.engine
    statements = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    return statements


def test_monitor_token_runs_no_queries_once_loaded(app, client, monitor_token, catalog):
    for path in ("/v1/models", "/v1/models?tokens=true", "/v1/models/plain-a?tokens=true"):
        client.get(path, headers=_auth(monitor_token))  # loads the snapshot and both counts

    def requests():
        for path in ("/v1/models", "/v1/models?tokens=true", "/v1/models/plain", "/v1/models/plain-a?tokens=true"):
            assert client.get(path, headers=_auth(monitor_token)).status_code == HTTPStatus.OK

    assert _statements(app, requests) == []


@pytest.mark.parametrize("path", ["/v1/models", "/v1/models?include_unacknowledged=true",
                                  "/v1/models/plain", "/v1/models/plain-a"])
def test_user_key_runs_three_queries(app, client, catalog, path):
    client.get("/v1/models", headers=_auth(USER_KEY))  # loads the snapshot and the request counts

    def request():
        assert client.get(path, headers=_auth(USER_KEY)).status_code == HTTPStatus.OK

    statements = _statements(app, request)
    assert len(statements) == 3, statements
    assert "api_keys" in statements[0] and "entities" in statements[0]
    assert "entity_limits" in statements[1] and "entity_balances" in statements[1]
    assert "entity_model_consents" in statements[2]


# ── Counts: freshness, laziness, failure isolation ──────────────────────────

def _requests(client, monitor_token, name="plain"):
    return client.get(f"/v1/models/{name}", headers=_auth(monitor_token)).get_json()["requests"]


def _add_request(app, name="plain"):
    with app.app_context():
        mc = db.session.execute(select(ModelConfig).filter_by(model_name=name)).scalar_one()
        _log(mc, timedelta(seconds=40))
        db.session.commit()


def test_counts_change_only_after_a_tick(app, client, monitor_token, catalog):
    before = _requests(client, monitor_token)
    _add_request(app)
    assert _requests(client, monitor_token) == before

    model_cache.refresh_counts(app)
    after = _requests(client, monitor_token)
    assert after["last_minute"] == before["last_minute"] + 1


def test_counts_change_after_the_safety_valve(app, client, monitor_token, catalog):
    before = _requests(client, monitor_token)
    _add_request(app)
    model_cache._counts["requests"].loaded_at -= model_cache.MAX_AGE + 1
    assert _requests(client, monitor_token)["last_minute"] == before["last_minute"] + 1


def test_health_tick_refreshes_the_counts(app, client, monitor_token, catalog, monkeypatch):
    monkeypatch.setattr(health, "run_health_pass", lambda: 0)
    before = _requests(client, monitor_token)
    _add_request(app)
    health._health_tick(app)
    assert _requests(client, monitor_token)["last_minute"] == before["last_minute"] + 1


def test_tick_runs_no_count_queries_in_a_process_that_never_listed(app, catalog):
    model_cache.resolve("plain")  # a process that routes but never serves /v1/models
    statements = _statements(app, lambda: model_cache.refresh_counts(app))
    assert statements == []


def test_unread_counts_are_dropped_on_the_next_tick(app, client, monitor_token, catalog):
    before = _requests(client, monitor_token)
    assert len(_statements(app, lambda: model_cache.refresh_counts(app))) == 1  # read since: reload
    assert _statements(app, lambda: model_cache.refresh_counts(app)) == []  # not read since: drop
    assert model_cache._counts["requests"].data is None
    assert _requests(client, monitor_token) == before  # loaded again on the next read


def test_tokens_are_only_loaded_when_asked_for(app, client, monitor_token, catalog):
    _requests(client, monitor_token)
    assert model_cache._counts["tokens"].data is None
    assert all("input_tokens" not in st for st in _statements(app, lambda: model_cache.refresh_counts(app)))


def test_failed_count_refresh_keeps_counts_and_routing_still_refreshes(app, client, monitor_token, catalog, monkeypatch):
    monkeypatch.setattr(health, "run_health_pass", lambda: 0)
    before = _requests(client, monitor_token)
    _add_request(app)
    with app.app_context():
        _model("added-later")
        db.session.commit()

    def broken(*args, **kwargs):
        raise RuntimeError("aggregate unavailable")

    monkeypatch.setattr(request_rates, "rates", broken)
    health._health_tick(app)

    assert _requests(client, monitor_token) == before
    with app.app_context():
        assert model_cache.resolve("added-later") is not None


def test_failed_reload_at_the_safety_valve_keeps_the_counts(app, client, monitor_token, catalog, monkeypatch):
    before = _requests(client, monitor_token)
    model_cache._counts["requests"].loaded_at -= model_cache.MAX_AGE + 1
    monkeypatch.setattr(request_rates, "rates", lambda *a, **k: 1 / 0)
    assert _requests(client, monitor_token) == before


def test_count_failure_never_fails_the_snapshot_load(app, catalog, monkeypatch):
    monkeypatch.setattr(request_rates, "rates", lambda *a, **k: 1 / 0)
    model_cache.refresh(app)
    with app.app_context():
        assert model_cache.resolve("plain") is not None


# ── Endpoint filtering ───────────────────────────────────────────────────────

def test_unhealthy_endpoint_is_listed_but_never_routed(app, client, monitor_token, catalog):
    entry = client.get("/v1/models/degraded", headers=_auth(monitor_token)).get_json()
    assert entry["instances"] == {"configured": 2, "healthy": 1}
    with app.app_context():
        mc = model_cache.resolve("degraded")
        listed = model_cache.listed_endpoints(mc.id)
        unhealthy = [ep.id for ep in listed if not ep.healthy]
        assert len(listed) == 2 and len(unhealthy) == 1
        picked = {model_cache.next_endpoint(mc.id).id for _ in range(6)}
        assert unhealthy[0] not in picked
        assert model_cache.next_endpoint(model_cache.resolve("down").id) is None
