"""Retirement keeps endpoint history while removing the endpoint from live use."""

import logging
import re
from datetime import timezone
from http import HTTPStatus
from unittest.mock import patch

import pytest
import yaml
from sqlalchemy import select, text

from lumen.blueprints.profile.routes import _fetch_model_context
from lumen.commands import sync_models_from_yaml
from lumen.extensions import db
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.request_log import RequestLog
from lumen.services.health import check_all_endpoints
from lumen.services.llm import get_model_status, get_next_endpoint
from lumen.services.metrics_snapshot import refresh_snapshot
from lumen.timeutils import utcnow


def _config(*urls):
    return {"models": [{
        "name": "retirement-model", "input_cost_per_million": 1,
        "output_cost_per_million": 1,
        "endpoints": [{"url": url, "api_key": "test-key"} for url in urls],
    }]}


@pytest.mark.parametrize("remove_model", [False, True])
def test_retirement_preserves_history_and_reactivation_reuses_id(app, remove_model):
    with app.app_context():
        sync_models_from_yaml(_config("https://retired.example/v1"))
        endpoint = db.session.execute(select(ModelEndpoint)).scalar_one()
        endpoint.healthy = True
        endpoint_id = endpoint.id
        log = RequestLog(time=utcnow().replace(tzinfo=timezone.utc), source="api",
                         model_config_id=endpoint.model_config_id, model_endpoint_id=endpoint_id)
        db.session.add(log)
        db.session.commit()
        log_id = log.id
        # Model the production constraint: retirement must never update history,
        # even on SQLite where a plain FK would not expose compression failures.
        db.session.execute(text("""
            CREATE TRIGGER forbid_history_update BEFORE UPDATE ON request_logs
            BEGIN SELECT RAISE(ABORT, 'request history must not change'); END
        """))
        db.session.commit()
        try:
            sync_models_from_yaml({"models": []} if remove_model else _config())
            retired = db.session.get(ModelEndpoint, endpoint_id)
            assert retired is not None
            assert retired.active is False
            assert db.session.get(RequestLog, log_id).model_endpoint_id == endpoint_id

            sync_models_from_yaml(_config("https://retired.example/v1"))
            endpoints = db.session.execute(select(ModelEndpoint)).scalars().all()
            assert [ep.id for ep in endpoints] == [endpoint_id]
            assert endpoints[0].active is True
            assert endpoints[0].healthy is False
            assert db.session.get(RequestLog, log_id).model_endpoint_id == endpoint_id
        finally:
            db.session.rollback()
            db.session.execute(text("DROP TRIGGER forbid_history_update"))
            db.session.commit()


def test_duplicate_url_removal_and_reactivation_preserve_rows(app):
    a, b = "https://a.example/v1", "https://b.example/v1"
    with app.app_context():
        sync_models_from_yaml(_config(a, b, b))
        original_ids = set(db.session.scalars(select(ModelEndpoint.id)))
        sync_models_from_yaml(_config(a, b))
        assert len(db.session.scalars(select(ModelEndpoint).filter_by(active=True)).all()) == 2
        sync_models_from_yaml(_config(a))
        rows = db.session.scalars(select(ModelEndpoint)).all()
        assert {ep.id for ep in rows} == original_ids
        assert [ep.url for ep in rows if ep.active] == [a]
        sync_models_from_yaml(_config(a, b, b))
        rows = db.session.scalars(select(ModelEndpoint)).all()
        assert {ep.id for ep in rows} == original_ids
        assert all(ep.active for ep in rows)


@pytest.mark.parametrize("active_healthy", [False, True])
def test_retired_endpoint_is_excluded_from_live_consumers(app, admin_client, admin_user, active_healthy):
    a, b = "https://active.example/v1", "https://retired.example/v1"
    with app.app_context():
        sync_models_from_yaml(_config(a, b))
        model = db.session.execute(select(ModelConfig).filter_by(model_name="retirement-model")).scalar_one()
        for ep in model.endpoints:
            ep.active = ep.url == a
            ep.healthy = active_healthy if ep.active else True
        db.session.commit()
        model_id = model.id
        assert get_model_status(model) == ("ok" if active_healthy else "down")
        for _ in range(3):
            selected = get_next_endpoint(model_id)
            assert (selected.url if selected else None) == (a if active_healthy else None)
        assert [ep.url for ep in _fetch_model_context(admin_user["id"])[1][model_id]] == [a]
        assert refresh_snapshot().endpoint_health == (("retirement-model", a, active_healthy),)

    detail = admin_client.get("/models/retirement-model")
    assert detail.status_code == HTTPStatus.OK
    assert a.encode() in detail.data
    assert b.encode() not in detail.data
    assert f"{int(active_healthy)}/1 healthy".encode() in detail.data
    listing = admin_client.get("/models")
    assert re.search(rf">\s*{int(active_healthy)}\s*</span>\s*/\s*1\b", listing.data.decode())

    with patch.dict(app.config, YAML_DATA={"api": {"monitoring": {"token": "review-monitor"}}}):
        headers = {"Authorization": "Bearer review-monitor"}
        listed = admin_client.get("/v1/models", headers=headers).get_json()["data"]
        single = admin_client.get("/v1/models/retirement-model", headers=headers).get_json()
    for model_data in (next(m for m in listed if m["id"] == "retirement-model"), single):
        assert model_data["instances"] == {"configured": 1, "healthy": int(active_healthy)}

    with app.app_context(), patch("lumen.services.health._probe_endpoint", return_value=True) as probe:
        assert check_all_endpoints() == 1
        assert probe.call_args.args[0] == a


def test_watcher_retries_failed_removal_sync_until_it_converges(
    app, admin_client, tmp_path, restore_config, caplog,
):
    """A transient sync failure while removing an endpoint is retried on later
    watcher polls without another file edit: nothing is applied and no success
    is logged until the sync commits, then the database rows, page counts,
    probes, and routing converge to the accepted config."""
    from lumen.services.config_watcher import _watcher

    a, b = "https://active.example/v1", "https://removed.example/v1"

    def watcher_config(*urls):
        # Keep the admins list so the admin-only per-endpoint rows render on
        # the detail page after the reload replaces YAML_DATA.
        return {"version": 3, "admins": ["admin@example.com"], **_config(*urls)}

    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(watcher_config(a, b)))

    sync_calls = []
    state_at_failure = {}

    def flaky_sync(data):
        reloaded_logged = sum(1 for r in caplog.records if r.getMessage() == "config.yaml reloaded")
        sync_calls.append({
            "endpoints": [ep["url"] for ep in data["models"][0]["endpoints"]],
            "reloaded_logged": reloaded_logged,
        })
        if len(sync_calls) == 2:
            # The removal reload: fail once, transiently, and prove nothing has
            # been applied — the removed endpoint is still live and the running
            # config still lists both URLs.
            removed = db.session.scalars(select(ModelEndpoint).filter_by(url=b)).all()
            state_at_failure["removed_still_active"] = [ep.active for ep in removed] == [True]
            state_at_failure["yaml_endpoints"] = [
                ep["url"] for ep in app.config["YAML_DATA"]["models"][0]["endpoints"]
            ]
            raise RuntimeError("simulated transient sync failure")
        return sync_models_from_yaml(data)

    sleep_count = 0

    def fake_sleep(n):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 3:
            # The endpoint is removed from the file after the initial reload;
            # no further edit follows, so the retry must fire on its own.
            config_file.write_text(yaml.dump(watcher_config(a)))
        if sleep_count >= 5:
            raise SystemExit("stop")

    mtime_values = [1.0, 2.0, 3.0]
    mtime_idx = 0

    def fake_getmtime(path):
        nonlocal mtime_idx
        v = mtime_values[mtime_idx] if mtime_idx < len(mtime_values) else mtime_values[-1]
        mtime_idx += 1
        return v

    with caplog.at_level(logging.INFO), \
         patch("lumen.services.config_watcher.sync_models_from_yaml", side_effect=flaky_sync), \
         patch("lumen.services.config_watcher.time.sleep", side_effect=fake_sleep), \
         patch("lumen.services.config_watcher.os.path.getmtime", side_effect=fake_getmtime):
        try:
            _watcher(app, str(config_file))
        except SystemExit:
            pass

    # One sync attempt per poll until success: initial load, the failed removal,
    # then the retry driven by the unchanged file — and nothing further.
    assert [c["endpoints"] for c in sync_calls] == [[a, b], [a], [a]]
    # The failed attempt applied nothing (the previous config stayed active and
    # the removed endpoint stayed live) and logged no success of its own.
    assert state_at_failure == {"removed_still_active": True, "yaml_endpoints": [a, b]}
    assert sync_calls[1]["reloaded_logged"] == sync_calls[2]["reloaded_logged"] == 1
    messages = [r.getMessage() for r in caplog.records]
    assert sum(1 for m in messages if m == "config.yaml reloaded") == 2
    assert any("sync_models_from_yaml failed" in m and "pending" in m for m in messages)

    # The health pass probes only the surviving endpoint; it then routes.
    with app.app_context(), patch("lumen.services.health._probe_endpoint", return_value=True) as probe:
        assert check_all_endpoints() == 1
        assert probe.call_args.args[0] == a

    with app.app_context():
        rows = db.session.scalars(select(ModelEndpoint).order_by(ModelEndpoint.id)).all()
        assert [(ep.url, ep.active, ep.healthy) for ep in rows] == [(a, True, True), (b, False, False)]
        model_id = db.session.scalar(select(ModelConfig.id).filter_by(model_name="retirement-model"))
    for _ in range(4):
        assert get_next_endpoint(model_id).url == a

    detail = admin_client.get("/models/retirement-model")
    assert detail.status_code == HTTPStatus.OK
    assert a.encode() in detail.data
    assert b.encode() not in detail.data
    assert b"1/1 healthy" in detail.data
    listing = admin_client.get("/models")
    assert re.search(r">\s*1\s*</span>\s*/\s*1\b", listing.data.decode())
    assert [ep["url"] for ep in app.config["YAML_DATA"]["models"][0]["endpoints"]] == [a]
