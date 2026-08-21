"""Tests for check_all_endpoints — the per-tick logic of the health checker."""
import logging
import os
import time
from unittest.mock import MagicMock, patch

import pytest


def _add_endpoint(db, model_config_id, url, api_key, model_name=None, healthy=True):
    from lumen.models.model_endpoint import ModelEndpoint
    ep = ModelEndpoint(
        model_config_id=model_config_id,
        url=url,
        api_key=api_key,
        model_name=model_name,
        healthy=healthy,
    )
    db.session.add(ep)
    db.session.flush()
    return ep


def _make_openai_mock(model_ids):
    """Return a context-manager mock that lists the given model IDs.

    Model objects carry all four required fields from the OpenAI API spec:
    id (str), created (int, Unix timestamp), object (Literal["model"]), owned_by (str).
    See: https://platform.openai.com/docs/api-reference/models/object
    """
    def model_obj(mid):
        m = MagicMock()
        m.id = mid
        m.created = 1677610602  # fixed Unix timestamp — arbitrary but spec-valid
        m.object = "model"
        m.owned_by = "test-org"
        return m

    client = MagicMock()
    client.models.list.return_value = MagicMock(data=[model_obj(m) for m in model_ids])
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=client)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_no_endpoints_returns_zero(app):
    with app.app_context():
        from lumen.services.health import check_all_endpoints
        assert check_all_endpoints() == 0


def test_endpoint_healthy_when_model_found(app, test_model, test_model_endpoint):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.services.health import check_all_endpoints

        ep = db.session.get(ModelEndpoint, test_model_endpoint["id"])
        ep.model_name = "dummy"
        ep.healthy = False
        db.session.commit()

        mock_cm = _make_openai_mock(["dummy", "other"])
        with patch("lumen.services.health.openai.OpenAI", return_value=mock_cm):
            assert check_all_endpoints() == 1

        db.session.refresh(ep)
        assert ep.healthy is True
        assert ep.last_checked_at is not None


def test_endpoint_unhealthy_when_model_missing(app, test_model, test_model_endpoint):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.services.health import check_all_endpoints

        ep = db.session.get(ModelEndpoint, test_model_endpoint["id"])
        ep.model_name = "expected-model"
        ep.healthy = True
        db.session.commit()

        mock_cm = _make_openai_mock(["some-other-model"])
        with patch("lumen.services.health.openai.OpenAI", return_value=mock_cm):
            check_all_endpoints()

        db.session.refresh(ep)
        assert ep.healthy is False


def test_endpoint_unhealthy_on_connection_error(app, test_model, test_model_endpoint):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.services.health import check_all_endpoints

        ep = db.session.get(ModelEndpoint, test_model_endpoint["id"])
        ep.healthy = True
        db.session.commit()

        failing_cm = MagicMock()
        failing_cm.__enter__ = MagicMock(side_effect=ConnectionError("refused"))
        failing_cm.__exit__ = MagicMock(return_value=False)

        with patch("lumen.services.health.openai.OpenAI", return_value=failing_cm):
            check_all_endpoints()

        db.session.refresh(ep)
        assert ep.healthy is False


def test_endpoint_uses_parent_model_name_when_no_override(app, test_model, test_model_endpoint):
    """When ep.model_name is None, the parent ModelConfig.model_name is used."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.services.health import check_all_endpoints

        ep = db.session.get(ModelEndpoint, test_model_endpoint["id"])
        ep.model_name = None  # fall back to parent model_name = "test-model"
        ep.healthy = False
        db.session.commit()

        mock_cm = _make_openai_mock(["test-model"])
        with patch("lumen.services.health.openai.OpenAI", return_value=mock_cm):
            check_all_endpoints()

        db.session.refresh(ep)
        assert ep.healthy is True


def test_last_checked_at_updated_on_failure(app, test_model, test_model_endpoint):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.services.health import check_all_endpoints

        ep = db.session.get(ModelEndpoint, test_model_endpoint["id"])
        ep.last_checked_at = None
        db.session.commit()

        failing_cm = MagicMock()
        failing_cm.__enter__ = MagicMock(side_effect=RuntimeError("down"))
        failing_cm.__exit__ = MagicMock(return_value=False)

        with patch("lumen.services.health.openai.OpenAI", return_value=failing_cm):
            check_all_endpoints()

        db.session.refresh(ep)
        assert ep.last_checked_at is not None


def test_logging_healthy_endpoint(app, test_model, test_model_endpoint):
    """LOG_MODEL_HEALTH=True logs 'found' when model is present (covers lines 26-27)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.services.health import check_all_endpoints

        ep = db.session.get(ModelEndpoint, test_model_endpoint["id"])
        ep.model_name = "dummy"
        db.session.commit()

        app.config["LOG_MODEL_HEALTH"] = True
        try:
            mock_cm = _make_openai_mock(["dummy"])
            with patch("lumen.services.health.openai.OpenAI", return_value=mock_cm):
                with patch.object(app.logger, "info") as mock_log:
                    check_all_endpoints()
            assert mock_log.called
            log_msg = mock_log.call_args[0][0]
            assert "endpoint UP" in log_msg
        finally:
            app.config["LOG_MODEL_HEALTH"] = False


def test_logging_exception_endpoint(app, test_model, test_model_endpoint):
    """LOG_MODEL_HEALTH=True logs 'endpoint DOWN' on exception (covers lines 33-34)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.services.health import check_all_endpoints

        ep = db.session.get(ModelEndpoint, test_model_endpoint["id"])
        ep.healthy = True
        db.session.commit()

        app.config["LOG_MODEL_HEALTH"] = True
        try:
            failing_cm = MagicMock()
            failing_cm.__enter__ = MagicMock(side_effect=ConnectionError("refused"))
            failing_cm.__exit__ = MagicMock(return_value=False)
            with patch("lumen.services.health.openai.OpenAI", return_value=failing_cm):
                with patch.object(app.logger, "info") as mock_log:
                    check_all_endpoints()
            assert mock_log.called
            log_msg = mock_log.call_args[0][0]
            assert "endpoint DOWN" in log_msg
        finally:
            app.config["LOG_MODEL_HEALTH"] = False


def test_hung_probe_does_not_block_other_endpoints(app, test_model, test_model_endpoint):
    """A probe stuck past _PROBE_TIMEOUT (e.g. a network path silently dropping
    packets, so the raw socket.connect() never returns) must be abandoned rather
    than block the rest of the pass — other endpoints still get checked."""
    import time

    from lumen.extensions import db
    from lumen.models.model_endpoint import ModelEndpoint

    with app.app_context():
        ep2 = _add_endpoint(db, test_model["id"], "http://second/v1", "key", model_name="dummy")
        db.session.commit()
        ep1_id, ep2_id = test_model_endpoint["id"], ep2.id

    with app.app_context():
        from lumen.services import health

        def hung_openai(*args, base_url=None, **kwargs):
            if base_url == "http://second/v1":
                return _make_openai_mock(["dummy"])
            cm = MagicMock()
            cm.__enter__ = MagicMock(side_effect=lambda: time.sleep(1))
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        with patch.object(health, "_PROBE_TIMEOUT", 0.05), \
             patch("lumen.services.health.openai.OpenAI", side_effect=hung_openai):
            assert health.check_all_endpoints() == 2

    with app.app_context():
        ep1 = db.session.get(ModelEndpoint, ep1_id)
        ep2 = db.session.get(ModelEndpoint, ep2_id)
        assert ep1.healthy is False  # abandoned after exceeding _PROBE_TIMEOUT
        assert ep2.healthy is True   # unaffected by the other endpoint hanging


def test_no_open_transaction_during_probe(app, test_model, test_model_endpoint):
    """Probes run on a separate thread pool with no Flask app context pushed, so
    they structurally cannot touch db.session/hold the app's DB connection open
    for the (slow, potentially hanging) duration of the network call."""
    with app.app_context():
        from flask import has_app_context

        from lumen.services.health import check_all_endpoints

        probe_had_app_context = []

        def record_then_list():
            probe_had_app_context.append(has_app_context())
            return MagicMock(data=[])

        client = MagicMock()
        client.models.list.side_effect = record_then_list
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=client)
        cm.__exit__ = MagicMock(return_value=False)

        with patch("lumen.services.health.openai.OpenAI", return_value=cm):
            check_all_endpoints()

        assert probe_had_app_context == [False]


# --- single-runner election -------------------------------------------------
#
# Every process of a pod runs the health checker, so without an election N
# processes × M endpoints probe the same backends every 60s. run_health_pass
# elects one runner with a non-blocking flock; the rest skip and read the DB
# result the holder wrote.
#
# flock is scoped to the *open file description*, not the process, so two
# separate os.open() calls conflict even inside one process — that is what lets
# these tests simulate concurrent passes without forking.


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    """Point the election at a per-test lock file, not the container default."""
    from lumen.services import health
    path = tmp_path / "health.lock"
    monkeypatch.setenv(health.LOCK_PATH_ENV, str(path))
    return path


def _hold_lock(path):
    """Acquire the lock the way another process would. Returns the fd."""
    import fcntl
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_only_one_of_two_concurrent_passes_probes(lock_path, caplog):
    """A second pass arriving while the first is mid-probe must skip, not probe.

    This is the whole point of the election: N processes per pod would otherwise
    each run the full probe set against the same backends every interval.
    """
    from lumen.services import health

    passes = []
    inner = {}

    def fake_pass(heartbeat=None):
        passes.append("first")
        # A second process reaches the election while this pass still holds it.
        inner["result"] = health.run_health_pass()
        return 1

    with patch.object(health, "check_all_endpoints", side_effect=fake_pass):
        assert health.run_health_pass() == 1

    assert passes == ["first"], "the second pass must not have probed"
    assert inner["result"] == 0, "the second pass must report having done nothing"


def test_pass_still_runs_when_flock_raises(lock_path):
    """No flock (platform, filesystem) degrades to the pre-election behaviour:
    probe rather than raise or silently stop checking health."""
    import errno
    import fcntl

    from lumen.services import health

    def boom(*args, **kwargs):
        raise OSError(errno.ENOLCK, "locks not supported here")

    passes = []
    with patch.object(fcntl, "flock", side_effect=boom), \
         patch.object(health, "check_all_endpoints", side_effect=lambda heartbeat=None: passes.append(1) or 1):
        assert health.run_health_pass() == 1
    assert passes == [1]


def test_pass_still_runs_without_fcntl(lock_path):
    """A platform with no fcntl module at all still gets health checks."""
    from lumen.services import health

    passes = []
    with patch.object(health, "fcntl", None), \
         patch.object(health, "check_all_endpoints", side_effect=lambda heartbeat=None: passes.append(1) or 1):
        assert health.run_health_pass() == 1
    assert passes == [1]


def test_heartbeat_refreshed_during_a_long_pass(lock_path):
    """check_all_endpoints must stamp the lock file as it works, so a pass that
    legitimately runs longer than _HEARTBEAT_STALE_AFTER is not taken over."""
    from lumen.services import health

    mtimes = []

    def fake_pass(heartbeat=None):
        stale = time.time() - (health._HEARTBEAT_STALE_AFTER + 60)
        os.utime(str(lock_path), (stale, stale))
        heartbeat()
        mtimes.append(os.stat(str(lock_path)).st_mtime)
        return 1

    with patch.object(health, "check_all_endpoints", side_effect=fake_pass):
        health.run_health_pass()

    assert mtimes and (time.time() - mtimes[0]) < health._HEARTBEAT_STALE_AFTER


def test_check_all_endpoints_calls_heartbeat_per_endpoint(app, test_model, test_model_endpoint):
    with app.app_context():
        from lumen.services.health import check_all_endpoints

        beats = []
        mock_cm = _make_openai_mock(["dummy"])
        with patch("lumen.services.health.openai.OpenAI", return_value=mock_cm):
            assert check_all_endpoints(heartbeat=lambda: beats.append(1)) == 1
        assert beats == [1]


def test_start_health_checker_starts_daemon_thread(app):
    """start_health_checker must start exactly one daemon thread (covers lines 45-55)."""
    import threading
    from unittest.mock import patch

    from lumen.services.health import start_health_checker

    captured = []

    original_thread = threading.Thread

    def fake_thread(*args, **kwargs):
        t = original_thread(*args, **kwargs)
        captured.append(t)
        return t

    with patch("lumen.services.health.threading.Thread", side_effect=fake_thread):
        start_health_checker(app)

    assert len(captured) == 1
    assert captured[0].daemon is True


def test_fresh_heartbeat_holder_is_skipped_quietly(lock_path):
    """A holder that is refreshing is working; non-holders skip without alarm."""
    from lumen.services import health

    fd = _hold_lock(lock_path)
    try:
        health._heartbeat(fd)
        with patch.object(health, "check_all_endpoints", side_effect=AssertionError("must not probe")):
            assert health.run_health_pass() == 0
    finally:
        os.close(fd)


def test_stale_heartbeat_does_not_take_over(lock_path, caplog):
    """A wedged holder is logged loudly and left alone — never raced.

    Takeover was removed deliberately. When the holder *dies* the kernel drops
    its flock and the next acquire simply succeeds, so no takeover is needed;
    when it is alive but wedged it still holds the flock, so seizing the pass
    would mean two passes hitting the same backends at once. The only step that
    does not heartbeat is the commit, so a stale heartbeat means the pool is
    exhausted — the worst possible moment to start a second pass.
    """
    from lumen.services import health

    fd = _hold_lock(lock_path)
    try:
        stale = time.time() - (health._HEARTBEAT_STALE_AFTER + 60)
        os.utime(str(lock_path), (stale, stale))

        with caplog.at_level(logging.WARNING, logger="lumen.services.health"):
            with patch.object(
                health, "check_all_endpoints", side_effect=AssertionError("must not probe")
            ):
                assert health.run_health_pass() == 0

        assert any("wedged" in r.message for r in caplog.records), (
            "a wedged holder must be reported, not silently skipped"
        )
    finally:
        os.close(fd)


def test_dead_holder_needs_no_takeover(lock_path):
    """The kernel releases flock on process exit, so holder death self-heals.

    This is why the takeover path was unnecessary as well as unsound: closing
    the fd is exactly what a dying process does.
    """
    from lumen.services import health

    fd = _hold_lock(lock_path)
    stale = time.time() - (health._HEARTBEAT_STALE_AFTER + 60)
    os.utime(str(lock_path), (stale, stale))
    os.close(fd)  # the holder "dies"

    passes = []
    with patch.object(
        health, "check_all_endpoints", side_effect=lambda heartbeat=None: passes.append(1) or 1
    ):
        assert health.run_health_pass() == 1
    assert passes == [1]


def test_lock_is_released_after_a_pass(lock_path):
    """Back-to-back passes must both run; the lock is per-pass, not per-process."""
    from lumen.services import health

    passes = []
    with patch.object(
        health, "check_all_endpoints", side_effect=lambda heartbeat=None: passes.append(1) or 1
    ):
        assert health.run_health_pass() == 1
        assert health.run_health_pass() == 1
    assert passes == [1, 1]


def test_lock_file_is_not_followed_through_a_symlink(tmp_path, monkeypatch):
    """A symlink planted at the lock path must not redirect the open or the heartbeat.

    The default path lives under /tmp. In a container that is private and this
    is theatre; outside one it is not, and the heartbeat is a utime() on
    whatever the fd points at.
    """
    from lumen.services import health

    target = tmp_path / "someone-elses-file"
    target.write_text("do not touch")
    link = tmp_path / "health.lock"
    link.symlink_to(target)
    monkeypatch.setenv(health.LOCK_PATH_ENV, str(link))

    fd, acquired = health._acquire(str(link))
    try:
        assert fd is None, "open() must refuse to follow the symlink"
        assert acquired is False
    finally:
        if fd is not None:
            os.close(fd)
    assert target.read_text() == "do not touch"


def test_unopenable_lock_still_runs_the_pass(tmp_path, monkeypatch):
    """A lock we cannot open degrades to probing, never to silence.

    Losing health checks entirely because of a lock-file problem would be a
    worse failure than the duplicated probing the election exists to remove.
    """
    from lumen.services import health

    unopenable = tmp_path / "nodir" / "health.lock"  # parent does not exist
    monkeypatch.setenv(health.LOCK_PATH_ENV, str(unopenable))

    passes = []
    with patch.object(
        health, "check_all_endpoints", side_effect=lambda heartbeat=None: passes.append(1) or 1
    ):
        assert health.run_health_pass() == 1
    assert passes == [1]
