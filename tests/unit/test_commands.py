"""Tests for YAML sync functions in lumen/commands.py."""
from datetime import datetime

from sqlalchemy import select

from lumen.commands import sync_group_rules_from_yaml, sync_models_from_yaml


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
        from lumen.models.model_endpoint import ModelEndpoint
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


def test_sync_group_rules_creates_missing_groups(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        sync_group_rules_from_yaml({"group_rules": {"staff": [{"field": "affiliation", "contains": "staff@x.edu"}],
                                                    "empty-rules": []}})
        staff = db.session.execute(select(Group).filter_by(name="staff")).scalar_one_or_none()
        empty = db.session.execute(select(Group).filter_by(name="empty-rules")).scalar_one_or_none()
        assert staff is not None and staff.config_managed is True
        assert empty is not None


def test_sync_group_rules_leaves_existing_groups_untouched(app):
    """Groups are DB-managed; the rules sync only ensures rows exist, never edits them."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_limit import GroupLimit
        g = Group(name="staff", active=True, config_managed=False, description="hand-made")
        db.session.add(g)
        db.session.flush()
        db.session.add(GroupLimit(group_id=g.id, max_coins=50, refresh_coins=1, starting_coins=50))
        db.session.commit()

        sync_group_rules_from_yaml({"group_rules": {"staff": [{"field": "idp", "equals": "x"}]}})

        db.session.expire_all()
        g = db.session.execute(select(Group).filter_by(name="staff")).scalar_one()
        assert g.config_managed is False
        assert g.description == "hand-made"
        assert g.limit is not None and float(g.limit.max_coins) == 50.0


def test_sync_group_rules_empty_section_is_noop(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        sync_group_rules_from_yaml({})
        assert db.session.execute(select(Group)).scalars().all() == []


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
    from datetime import date, datetime
    from lumen.commands import _normalize_end_date
    with app.app_context():
        assert _normalize_end_date(date(2026, 9, 1)) == datetime(2026, 9, 1)


def test_normalize_end_date_naive_datetime_passthrough(app):
    from datetime import datetime
    from lumen.commands import _normalize_end_date
    with app.app_context():
        dt = datetime(2026, 9, 1, 12, 30)
        assert _normalize_end_date(dt) == dt


def test_normalize_end_date_aware_datetime_to_naive_utc(app):
    from datetime import datetime, timezone, timedelta
    from lumen.commands import _normalize_end_date
    with app.app_context():
        aware = datetime(2026, 9, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
        assert _normalize_end_date(aware) == datetime(2026, 9, 1, 10, 0)


def test_normalize_end_date_from_iso_string(app):
    from datetime import datetime
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
    from datetime import date, datetime
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
    from datetime import datetime
    from lumen.commands import _normalize_end_date
    with app.app_context():
        assert _normalize_end_date("Thu, 31 Dec 2026 00:00:00 GMT") == datetime(2026, 12, 31)
