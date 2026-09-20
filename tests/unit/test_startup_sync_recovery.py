"""A failed startup sync must not poison the metrics priming that follows.

create_app catches a sync_models_from_yaml failure and then primes the metrics
snapshot in the same app context. A sync that fails mid-flush leaves the
session in pending-rollback state, so without an explicit rollback the priming
dies with PendingRollbackError — logged with a misleading "run flask db
upgrade" hint that hides the real sync error.
"""

import yaml


def _write_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.dump({
        "version": 3,
        "app": {
            "secret_key": "s" * 32,
            "encryption_key": "e" * 32,
            "dev_user": "testuser@example.com",
            "debug": True,
            "database": {"url": "sqlite:///:memory:"},
        },
        "models": [{
            "name": "m",
            "input_cost_per_million": 0,
            "output_cost_per_million": 0,
        }],
    }))
    return str(cfg)


def test_startup_recovers_failed_sync_flush_before_metrics_priming(tmp_path, monkeypatch, capsys):
    """An injected sync flush failure followed by metrics initialization.

    The flush fails at the database level (NOT NULL violation), which is what
    leaves the session pending-rollback — the same shape as the production
    endpoint-deletion failure. Priming must still run afterwards.
    """
    import config as config_module
    monkeypatch.setattr(config_module.Config, "CONFIG_YAML", _write_config(tmp_path))

    from lumen import create_app
    from lumen.extensions import db
    from lumen.models.model_config import ModelConfig
    from lumen.services import metrics_snapshot

    def failing_sync(data):
        # Missing required cost fields: the flush raises IntegrityError.
        db.session.add(ModelConfig(model_name="startup-flush-failure"))
        db.session.flush()

    primed = []
    real_refresh = metrics_snapshot.refresh_snapshot

    def recording_refresh():
        primed.append(True)
        return real_refresh()

    monkeypatch.setattr("lumen.commands.sync_models_from_yaml", failing_sync)
    monkeypatch.setattr(metrics_snapshot, "refresh_snapshot", recording_refresh)
    # This throwaway app must not start a second background refresher thread.
    monkeypatch.setattr(metrics_snapshot, "start_snapshot_refresher", lambda app: None)

    create_app()

    err = capsys.readouterr().err
    assert "Could not sync models from yaml" in err
    # The failure is a constraint violation, not a schema gap, so the migration
    # hint must not be offered for it.
    assert "flask db upgrade" not in err
    # The priming that follows the failed sync ran on a recovered session
    # instead of dying with PendingRollbackError.
    assert primed == [True]
    assert "Could not prime the metrics snapshot" not in err


def test_startup_db_hint_matches_hint_to_error():
    from sqlalchemy.exc import IntegrityError, OperationalError

    from lumen import _startup_db_hint

    schema_error = OperationalError("SELECT 1", {}, Exception("no such table: model_configs"))
    hint = _startup_db_hint(schema_error)
    assert "flask db upgrade" in hint
    assert "no such table: model_configs" in hint

    data_error = IntegrityError("INSERT 1", {}, Exception("UNIQUE constraint failed"))
    hint = _startup_db_hint(data_error)
    assert "flask db upgrade" not in hint
    assert "UNIQUE constraint failed" in hint
