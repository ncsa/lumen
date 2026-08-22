"""Tests for YAML sync functions in lumen/commands.py."""
from datetime import datetime

from sqlalchemy import select

from lumen.commands import backfill_aggregate_cmd, enable_retention_cmd, sync_models_from_yaml


def test_sync_models_creates_model_config(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        yaml_data = {
            "models": [
                {
                    "name": "synced-model",
                    "active": True,
                    "input_cost_per_million": 1.0,
                    "output_cost_per_million": 2.0,
                }
            ]
        }
        sync_models_from_yaml(yaml_data)
        mc = db.session.execute(select(ModelConfig).filter_by(model_name="synced-model")).scalar_one_or_none()
        assert mc is not None
        assert mc.active is True
        assert float(mc.input_cost_per_million) == 1.0


def test_sync_models_normalizes_bare_hf_repo_id(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        yaml_data = {
            "models": [
                {"name": "bare-id", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0,
                 "url": "meta-models/Muse-Glimmer-30B"},
                {"name": "full-url", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0,
                 "url": "https://example.com/docs/model"},
                {"name": "hf-com", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0,
                 "url": "https://huggingface.com/meta-models/Muse-Glimmer-30B"},
                {"name": "hf-www", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0,
                 "url": "https://www.huggingface.co/meta-models/Muse-Glimmer-30B"},
                {"name": "no-url", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0},
            ]
        }
        sync_models_from_yaml(yaml_data)
        by_name = {m.model_name: m for m in db.session.execute(select(ModelConfig)).scalars().all()}
        assert by_name["bare-id"].url == "https://huggingface.co/meta-models/Muse-Glimmer-30B"
        assert by_name["full-url"].url == "https://example.com/docs/model"
        assert by_name["hf-com"].url == "https://huggingface.co/meta-models/Muse-Glimmer-30B"
        assert by_name["hf-www"].url == "https://huggingface.co/meta-models/Muse-Glimmer-30B"
        assert by_name["no-url"].url is None


def test_sync_models_updates_existing(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        yaml_data = {
            "models": [
                {"name": "update-model", "active": True, "input_cost_per_million": 1.0, "output_cost_per_million": 1.0}
            ]
        }
        sync_models_from_yaml(yaml_data)
        yaml_data["models"][0]["input_cost_per_million"] = 5.0
        sync_models_from_yaml(yaml_data)
        mc = db.session.execute(select(ModelConfig).filter_by(model_name="update-model")).scalar_one_or_none()
        assert float(mc.input_cost_per_million) == 5.0


def test_sync_models_deactivates_removed(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        yaml1 = {"models": [{"name": "will-be-removed", "active": True, "input_cost_per_million": 1.0, "output_cost_per_million": 1.0}]}
        sync_models_from_yaml(yaml1)
        # Sync with empty models list — should deactivate
        sync_models_from_yaml({"models": []})
        mc = db.session.execute(select(ModelConfig).filter_by(model_name="will-be-removed")).scalar_one_or_none()
        assert mc is not None
        assert mc.active is False


def test_sync_models_with_endpoints(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        yaml_data = {
            "models": [
                {
                    "name": "ep-model",
                    "active": True,
                    "input_cost_per_million": 1.0,
                    "output_cost_per_million": 1.0,
                    "endpoints": [{"url": "http://ep1/v1", "api_key": "key1"}],
                }
            ]
        }
        sync_models_from_yaml(yaml_data)
        mc = db.session.execute(select(ModelConfig).filter_by(model_name="ep-model")).scalar_one_or_none()
        assert mc is not None
        eps = list(mc.endpoints)
        assert len(eps) == 1
        assert eps[0].url == "http://ep1/v1"


def test_no_runtime_group_rules_import():
    """Rules an admin deletes must stay deleted: config.yaml group_rules is not
    read at all (Lumen 2.0 removed the section — rules are database rows managed
    on each group's Rules tab). If this fails, someone reintroduced a
    startup/reload import — which resurrected deleted rules and re-enabled
    auto_join every restart."""
    from pathlib import Path

    import lumen.commands
    import lumen.services.config_watcher

    assert not hasattr(lumen.commands, "import_group_rules_from_yaml")
    root = Path(lumen.commands.__file__).resolve().parent
    for rel in ("__init__.py", "services/config_watcher.py", "commands.py"):
        source = (root / rel).read_text()
        assert "import_group_rules_from_yaml" not in source, rel


# ---------------------------------------------------------------------------
# _apply_model_fields / _apply_model_access — ack/early-access/disabled flags
# ---------------------------------------------------------------------------

def test_apply_model_fields_sets_ack_flags(app):
    from lumen.commands import _apply_model_fields
    from lumen.models.model_config import ModelConfig
    with app.app_context():
        mc = ModelConfig(model_name="m")
        _apply_model_fields(mc, {
            "name": "m",
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 2.0,
            "needs_ack": True,
            "ack_message": "ack me",
        })
        assert mc.needs_ack is True
        assert mc.disabled is False
        assert mc.ack_message == "ack me"


def test_apply_model_access_defaults(app):
    """With no flags in the definition, needs_ack and disabled default to False."""
    from lumen.commands import _apply_model_fields
    from lumen.models.model_config import ModelConfig
    with app.app_context():
        mc = ModelConfig(model_name="m2")
        _apply_model_fields(mc, {"name": "m2", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0})
        assert mc.needs_ack is False
        assert mc.disabled is False


def test_apply_model_access_legacy_active_false_maps_to_disabled(app, caplog):
    import logging

    from lumen.commands import _apply_model_fields, _warned
    from lumen.models.model_config import ModelConfig
    with app.app_context():
        _warned.clear()
        mc = ModelConfig(model_name="m3")
        with caplog.at_level(logging.WARNING):
            _apply_model_fields(mc, {
                "name": "m3", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0,
                "active": False,
            })
        assert mc.disabled is True
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "active: false" in msgs


def test_apply_model_access_removed_access_key_is_ignored(app):
    """The removed per-model 'access:' key is ignored; ownership is never touched by config sync."""
    from lumen.commands import _apply_model_fields
    from lumen.models.model_config import ModelConfig
    with app.app_context():
        mc = ModelConfig(model_name="m4")
        _apply_model_fields(mc, {
            "name": "m4", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0,
            "access": "allowed",
        })
        assert mc.owner_entity_id is None
        assert mc.disabled is False


def test_apply_model_access_explicit_disabled(app):
    from lumen.commands import _apply_model_fields
    from lumen.models.model_config import ModelConfig
    with app.app_context():
        mc = ModelConfig(model_name="m5")
        _apply_model_fields(mc, {
            "name": "m5", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0,
            "disabled": True,
        })
        assert mc.disabled is True


# ---------------------------------------------------------------------------
# _normalize_end_date / _normalize_knowledge_cutoff
# ---------------------------------------------------------------------------

def test_normalize_end_date_from_date(app):
    from datetime import date

    from lumen.commands import _normalize_end_date
    with app.app_context():
        assert _normalize_end_date(date(2026, 9, 1)) == datetime(2026, 9, 1)


def test_normalize_end_date_naive_datetime_passthrough(app):
    from lumen.commands import _normalize_end_date
    with app.app_context():
        dt = datetime(2026, 9, 1, 12, 30)
        assert _normalize_end_date(dt) == dt


def test_normalize_end_date_aware_datetime_to_naive_utc(app):
    from datetime import timedelta, timezone

    from lumen.commands import _normalize_end_date
    with app.app_context():
        aware = datetime(2026, 9, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
        assert _normalize_end_date(aware) == datetime(2026, 9, 1, 10, 0)


def test_normalize_end_date_from_iso_string(app):
    from lumen.commands import _normalize_end_date
    with app.app_context():
        assert _normalize_end_date("2026-09-01") == datetime(2026, 9, 1)
        assert _normalize_end_date("2026-09-01T08:15:00") == datetime(2026, 9, 1, 8, 15)


def test_normalize_end_date_garbage_is_none(app):
    from lumen.commands import _normalize_end_date
    with app.app_context():
        assert _normalize_end_date("not-a-date", "m") is None
    assert _normalize_end_date(None) is None
    assert _normalize_end_date("") is None


def test_normalize_knowledge_cutoff_truncates(app):
    from lumen.commands import _normalize_knowledge_cutoff
    with app.app_context():
        assert _normalize_knowledge_cutoff("2024-06-15") == "2024-06"
        assert _normalize_knowledge_cutoff("2024-06") == "2024-06"
        assert _normalize_knowledge_cutoff(None) is None
        assert _normalize_knowledge_cutoff("not-a-real-date", "m") is None


def test_apply_model_fields_sets_early_access_and_end_date(app):
    from datetime import date

    from lumen.commands import _apply_model_fields
    from lumen.models.model_config import ModelConfig
    with app.app_context():
        mc = ModelConfig(model_name="ea-model")
        _apply_model_fields(mc, {
            "name": "ea-model",
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 2.0,
            "early_access": True,
            "end_date": date(2026, 12, 31),
            "knowledge_cutoff": "2024-06-15",
        })
        assert mc.early_access is True
        assert mc.end_date == datetime(2026, 12, 31)
        assert mc.knowledge_cutoff == "2024-06"


def test_normalize_end_date_rfc822_from_config_editor(app):
    """The admin config editor round-trips YAML dates through JSON as RFC 822."""
    from lumen.commands import _normalize_end_date
    with app.app_context():
        assert _normalize_end_date("Thu, 31 Dec 2026 00:00:00 GMT") == datetime(2026, 12, 31)


def test_backfill_aggregate_is_a_clean_noop_on_sqlite(app):
    """SQLite has no continuous aggregates. Say so and exit 0 — this is not an error."""
    result = app.test_cli_runner().invoke(backfill_aggregate_cmd, [])
    assert result.exit_code == 0, result.output
    assert "requires PostgreSQL/TimescaleDB" in result.output
    assert "sqlite" in result.output


def test_enable_retention_is_a_clean_noop_on_sqlite(app):
    result = app.test_cli_runner().invoke(enable_retention_cmd, [])
    assert result.exit_code == 0, result.output
    assert "requires PostgreSQL/TimescaleDB" in result.output
    assert "sqlite" in result.output
