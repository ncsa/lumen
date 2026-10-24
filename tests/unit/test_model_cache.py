"""Tests for the per-process model snapshot and the hooks that refresh it."""
import os
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import event

from lumen.extensions import db
from lumen.models.model_alias import ModelAlias
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.services import health, model_cache
from lumen.timeutils import utcnow


def _add_model(name, **kwargs):
    mc = ModelConfig(model_name=name, input_cost_per_million=1.0, output_cost_per_million=2.0, **kwargs)
    db.session.add(mc)
    db.session.commit()
    return mc.id


def _add_endpoint(model_config_id, url, healthy=True, active=True):
    ep = ModelEndpoint(model_config_id=model_config_id, url=url, api_key="k", healthy=healthy, active=active)
    db.session.add(ep)
    db.session.commit()
    return ep.id


def _endpoint_ids(model_config_id):
    return [ep.id for ep in model_cache.endpoints(model_config_id)]


@contextmanager
def _statements():
    """Yield the list every SQL statement run on the engine lands in."""
    log = []

    def record(conn, cursor, statement, *args):
        log.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        yield log
    finally:
        event.remove(db.engine, "before_cursor_execute", record)


# ---------------------------------------------------------------------------
# Loading and reading
# ---------------------------------------------------------------------------

def test_max_age_is_twice_the_health_interval():
    assert model_cache.MAX_AGE == 2 * health._CHECK_INTERVAL


def test_first_load_is_lazy_and_runs_three_queries(app, test_model, test_model_endpoint):
    with app.app_context():
        assert model_cache._snapshot is None
        with _statements() as log:
            mc = model_cache.resolve("test-model")
            assert mc.id == test_model["id"]
            assert len(log) == 3
            assert _endpoint_ids(test_model["id"]) == [test_model_endpoint["id"]]
            assert model_cache.resolve("test-model") is mc
            assert len(log) == 3  # later reads never touch the database


def test_snapshot_holds_plain_values(app, test_model, test_model_endpoint):
    with app.app_context():
        mc = model_cache.resolve("test-model")
        ep = model_cache.next_endpoint(test_model["id"])
    # Read outside any app context: nothing is lazy-loaded or session-bound.
    assert isinstance(mc, model_cache.ModelSnapshot)
    assert (mc.model_name, float(mc.input_cost_per_million), float(mc.output_cost_per_million)) == ("test-model", 1.0, 2.0)
    assert (ep.url, ep.api_key, ep.model_name) == ("http://localhost:9999/v1", "test-api-key-123", "dummy")
    assert "test-api-key-123" not in repr(ep)
    with pytest.raises(AttributeError):
        mc.disabled = True  # frozen


def test_load_leaves_the_callers_session_alone(app, test_model):
    """The lazy load runs in its own context: its rollback must not discard
    the request's pending changes."""
    with app.app_context():
        db.session.get(ModelConfig, test_model["id"]).description = "pending"
        model_cache.resolve("test-model")
        db.session.commit()
    with app.app_context():
        assert db.session.get(ModelConfig, test_model["id"]).description == "pending"


def test_unknown_and_empty_names_resolve_to_none(app, test_model):
    with app.app_context():
        assert model_cache.resolve("") is None
        assert model_cache.resolve("nope") is None
        assert model_cache.endpoints(test_model["id"]) == ()
        assert model_cache.next_endpoint(test_model["id"]) is None


def test_alias_resolves_to_canonical(app, test_model):
    with app.app_context():
        db.session.add(ModelAlias(alias="old-name", model_config_id=test_model["id"]))
        db.session.commit()
        assert model_cache.resolve("old-name").model_name == "test-model"


def test_only_healthy_active_endpoints_of_active_models(app, test_model):
    with app.app_context():
        good = _add_endpoint(test_model["id"], "http://good")
        _add_endpoint(test_model["id"], "http://sick", healthy=False)
        _add_endpoint(test_model["id"], "http://retired", active=False)
        off = _add_model("off-model", disabled=True)
        _add_endpoint(off, "http://off")
        assert _endpoint_ids(test_model["id"]) == [good]
        assert model_cache.resolve("off-model") is None
        assert model_cache.endpoints(off) == ()


def test_end_date_passing_between_refreshes_hides_the_model(app):
    """``active`` is evaluated on read, not frozen at load time."""
    with app.app_context():
        _add_model("ending", end_date=utcnow() + timedelta(seconds=30))
        assert model_cache.resolve("ending") is not None
        with patch("lumen.services.model_cache.utcnow", return_value=utcnow() + timedelta(minutes=1)):
            assert model_cache.resolve("ending") is None


# ---------------------------------------------------------------------------
# Changes become visible after refresh
# ---------------------------------------------------------------------------

def test_disabled_model_visible_after_refresh(app, test_model):
    with app.app_context():
        assert model_cache.resolve("test-model") is not None
        db.session.get(ModelConfig, test_model["id"]).disabled = True
        db.session.commit()
        assert model_cache.resolve("test-model") is not None  # not yet refreshed
        model_cache.refresh(app)
        assert model_cache.resolve("test-model") is None


def test_retired_endpoint_visible_after_refresh(app, test_model, test_model_endpoint):
    with app.app_context():
        assert _endpoint_ids(test_model["id"]) == [test_model_endpoint["id"]]
        db.session.get(ModelEndpoint, test_model_endpoint["id"]).active = False
        db.session.commit()
        model_cache.refresh(app)
        assert model_cache.endpoints(test_model["id"]) == ()


def test_alias_retarget_visible_after_refresh(app, test_model):
    with app.app_context():
        other = _add_model("other-model")
        db.session.add(ModelAlias(alias="moving", model_config_id=test_model["id"]))
        db.session.commit()
        assert model_cache.resolve("moving").id == test_model["id"]
        alias = db.session.execute(db.select(ModelAlias).filter_by(alias="moving")).scalar_one()
        alias.model_config_id = other
        db.session.commit()
        model_cache.refresh(app)
        assert model_cache.resolve("moving").id == other


def test_failed_refresh_keeps_the_previous_snapshot(app, test_model, caplog):
    with app.app_context():
        before = model_cache.resolve("test-model")
        with patch("lumen.services.model_cache._load", side_effect=RuntimeError("db down")):
            model_cache.refresh(app)  # must not raise
        assert model_cache.resolve("test-model") is before
    assert "model snapshot refresh failed" in caplog.text


# ---------------------------------------------------------------------------
# Safety valve
# ---------------------------------------------------------------------------

def test_snapshot_older_than_max_age_reloads_on_next_read(app, test_model):
    with app.app_context():
        assert model_cache.resolve("test-model") is not None
        db.session.get(ModelConfig, test_model["id"]).disabled = True
        db.session.commit()
        loaded = model_cache._snapshot.loaded_at
        with patch("lumen.services.model_cache.time.monotonic", return_value=loaded + model_cache.MAX_AGE):
            assert model_cache.resolve("test-model") is not None  # at the limit: still fresh
        with patch("lumen.services.model_cache.time.monotonic", return_value=loaded + model_cache.MAX_AGE + 1):
            assert model_cache.resolve("test-model") is None


# ---------------------------------------------------------------------------
# Refresh hooks
# ---------------------------------------------------------------------------

@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    path = tmp_path / "health.lock"
    monkeypatch.setenv(health.LOCK_PATH_ENV, str(path))
    return path


def test_health_tick_refreshes_in_a_process_that_did_not_probe(app, test_model, test_model_endpoint, lock_path):
    """Another process holds the election and marked the endpoint down; this
    process skips the pass but still picks the change up on its tick."""
    import fcntl
    with app.app_context():
        assert _endpoint_ids(test_model["id"]) == [test_model_endpoint["id"]]
        db.session.get(ModelEndpoint, test_model_endpoint["id"]).healthy = False
        db.session.commit()

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with patch("lumen.services.health.check_all_endpoints") as probe:
            health._health_tick(app)
        probe.assert_not_called()
    finally:
        os.close(fd)

    with app.app_context():
        assert model_cache.endpoints(test_model["id"]) == ()


def test_health_tick_refreshes_even_when_the_pass_fails(app, test_model):
    with app.app_context():
        assert model_cache.resolve("test-model") is not None
        db.session.get(ModelConfig, test_model["id"]).disabled = True
        db.session.commit()
    with patch("lumen.services.health.run_health_pass", side_effect=RuntimeError("boom")):
        health._health_tick(app)
    with app.app_context():
        assert model_cache.resolve("test-model") is None


def test_check_all_endpoints_refreshes_after_commit(app, test_model, test_model_endpoint):
    with app.app_context():
        assert _endpoint_ids(test_model["id"]) == [test_model_endpoint["id"]]
        with patch("lumen.services.health._probe_endpoint", return_value=False):
            assert health.check_all_endpoints() == 1
        assert model_cache.endpoints(test_model["id"]) == ()


def test_config_watcher_refreshes_after_sync(app, test_model, tmp_path, restore_config):
    import yaml

    from lumen.services.config_watcher import _watcher

    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"version": 3, "app": {"name": "Reloaded"}}))

    with app.app_context():
        assert model_cache.resolve("test-model") is not None

    def fake_sync(data):
        db.session.get(ModelConfig, test_model["id"]).disabled = True
        db.session.commit()

    sleeps = 0

    def fake_sleep(n):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 3:
            raise SystemExit("stop")

    mtimes = iter([1.0, 2.0])
    with patch("lumen.services.config_watcher.time.sleep", side_effect=fake_sleep), \
         patch("lumen.services.config_watcher.os.path.getmtime", side_effect=lambda p: next(mtimes, 2.0)), \
         patch("lumen.services.config_watcher.sync_models_from_yaml", side_effect=fake_sync):
        with pytest.raises(SystemExit):
            _watcher(app, str(config_file))

    with app.app_context():
        assert model_cache.resolve("test-model") is None


def test_config_watcher_does_not_refresh_after_failed_sync(app, test_model, tmp_path, restore_config):
    import yaml

    from lumen.services.config_watcher import _watcher

    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"version": 3, "app": {"name": "Reloaded"}}))

    sleeps = 0

    def fake_sleep(n):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 3:
            raise SystemExit("stop")

    mtimes = iter([1.0, 2.0])
    with patch("lumen.services.config_watcher.time.sleep", side_effect=fake_sleep), \
         patch("lumen.services.config_watcher.os.path.getmtime", side_effect=lambda p: next(mtimes, 2.0)), \
         patch("lumen.services.config_watcher.sync_models_from_yaml", side_effect=RuntimeError("db down")), \
         patch("lumen.services.config_watcher.model_cache.refresh") as refresh:
        with pytest.raises(SystemExit):
            _watcher(app, str(config_file))
    refresh.assert_not_called()


def test_admin_access_edit_refreshes_after_commit(app, admin_client, test_model, test_user):
    with app.app_context():
        assert model_cache.resolve("test-model").owner_entity_id is None
    resp = admin_client.patch(
        f"/admin/api/models/{test_model['id']}/access",
        json={"owner_email": "testuser@example.com"},
    )
    assert resp.status_code == 200
    with app.app_context():
        assert model_cache.resolve("test-model").owner_entity_id == test_user["id"]


def test_admin_access_edit_error_does_not_refresh(app, admin_client, test_model):
    with patch("lumen.blueprints.admin.routes.model_cache.refresh") as refresh:
        resp = admin_client.patch(
            f"/admin/api/models/{test_model['id']}/access",
            json={"owner_email": "nobody@example.com"},
        )
    assert resp.status_code == 400
    refresh.assert_not_called()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_readers_during_refresh_always_see_a_complete_snapshot(app):
    """A refresher flips the database between two states, each consistent
    across all three tables; every snapshot a reader sees matches one of them.

    State A: alias ``a`` -> m1, endpoint e1 on m1 healthy.
    State B: alias ``a`` -> m2, e1 retired, endpoint e2 on m2 healthy.
    """
    with app.app_context():
        m1 = _add_model("m1")
        m2 = _add_model("m2")
        e1 = _add_endpoint(m1, "http://e1")
        e2 = _add_endpoint(m2, "http://e2", healthy=False)
        db.session.add(ModelAlias(alias="a", model_config_id=m1))
        db.session.commit()
        model_cache.refresh(app)

    stop = threading.Event()
    seen = Counter()
    errors = []

    def reader():
        with app.app_context():
            while not stop.is_set():
                snap = model_cache._current()
                target = snap.models["a"].id if "a" in snap.models else None
                eps = {mc_id: [ep.id for ep in eps] for mc_id, eps in snap.endpoints.items()}
                if (target, eps) == (m1, {m1: [e1]}):
                    seen["A"] += 1
                elif (target, eps) == (m2, {m2: [e2]}):
                    seen["B"] += 1
                else:
                    errors.append((target, eps))
                time.sleep(0.0005)  # a spinning reader starves the refresher of the GIL

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        with app.app_context():
            for i in range(30):
                to_b = i % 2 == 0
                db.session.get(ModelAlias, db.session.execute(
                    db.select(ModelAlias.id).filter_by(alias="a")).scalar_one()
                ).model_config_id = m2 if to_b else m1
                db.session.get(ModelEndpoint, e1).active = not to_b
                db.session.get(ModelEndpoint, e2).healthy = to_b
                db.session.commit()
                model_cache.refresh(app)
    finally:
        stop.set()
        for t in threads:
            t.join()

    assert errors == []
    assert seen["A"] and seen["B"]


def test_round_robin_is_even_under_concurrency(app, test_model):
    with app.app_context():
        ids = [_add_endpoint(test_model["id"], f"http://ep{i}") for i in range(3)]
        model_cache.refresh(app)

    picks = Counter()
    lock = threading.Lock()
    start = threading.Barrier(8)

    def worker():
        local = Counter()
        with app.app_context():
            start.wait()
            for _ in range(300):
                local[model_cache.next_endpoint(test_model["id"]).id] += 1
        with lock:
            picks.update(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert picks == {i: 800 for i in ids}


def test_concurrent_stale_readers_load_once(app, test_model):
    """A burst of readers finding no snapshot runs one load, not one each."""
    loads = []
    real_load = model_cache._load

    def counting_load():
        loads.append(1)
        return real_load()

    start = threading.Barrier(6)
    resolved = []

    def reader():
        with app.app_context():
            start.wait()
            resolved.append(model_cache.resolve("test-model"))

    with patch("lumen.services.model_cache._load", side_effect=counting_load):
        threads = [threading.Thread(target=reader) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(loads) == 1
    assert len(resolved) == 6 and all(mc is not None for mc in resolved)
