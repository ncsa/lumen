"""Tests for YAML sync functions in lumen/commands.py."""
from lumen.commands import sync_clients_from_yaml, sync_global_model_access_from_yaml, sync_groups_from_yaml, sync_models_from_yaml


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
        mc = ModelConfig.query.filter_by(model_name="synced-model").first()
        assert mc is not None
        assert mc.active is True
        assert float(mc.input_cost_per_million) == 1.0


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
        mc = ModelConfig.query.filter_by(model_name="update-model").first()
        assert float(mc.input_cost_per_million) == 5.0


def test_sync_models_deactivates_removed(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        yaml1 = {"models": [{"name": "will-be-removed", "active": True, "input_cost_per_million": 1.0, "output_cost_per_million": 1.0}]}
        sync_models_from_yaml(yaml1)
        # Sync with empty models list — should deactivate
        sync_models_from_yaml({"models": []})
        mc = ModelConfig.query.filter_by(model_name="will-be-removed").first()
        assert mc is not None
        assert mc.active is False


def test_sync_models_with_endpoints(app):
    with app.app_context():
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
        mc = ModelConfig.query.filter_by(model_name="ep-model").first()
        assert mc is not None
        eps = list(mc.endpoints)
        assert len(eps) == 1
        assert eps[0].url == "http://ep1/v1"


def test_sync_groups_creates_group(app):
    with app.app_context():
        from lumen.models.group import Group
        yaml_data = {"groups": {"test-group": {}}}
        sync_groups_from_yaml(yaml_data)
        g = Group.query.filter_by(name="test-group").first()
        assert g is not None


def test_sync_groups_creates_group_with_limit(app):
    with app.app_context():
        from lumen.models.group import Group
        from lumen.models.group_limit import GroupLimit
        yaml_data = {
            "groups": {
                "limited-group": {
                    "max": 100,
                    "refresh": 10,
                    "starting": 100,
                }
            }
        }
        sync_groups_from_yaml(yaml_data)
        g = Group.query.filter_by(name="limited-group").first()
        assert g is not None
        assert g.limit is not None
        assert float(g.limit.max_coins) == 100.0


def test_sync_clients_creates_entity_limit(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_limit import EntityLimit
        client = Entity(entity_type="service", name="test-svc", initials="TS", active=True)
        db.session.add(client)
        db.session.commit()
        yaml_data = {
            "clients": {
                "default": {"max": 50.0, "refresh": 0.5, "starting": 50.0},
            }
        }
        sync_clients_from_yaml(yaml_data)
        limit = EntityLimit.query.filter_by(entity_id=client.id).first()
        assert limit is not None
        assert float(limit.max_coins) == 50.0
        assert float(limit.refresh_coins) == 0.5
        assert limit.config_managed is True


def test_sync_clients_named_entry_overrides_default(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_limit import EntityLimit
        client = Entity(entity_type="service", name="named-svc", initials="NS", active=True)
        db.session.add(client)
        db.session.commit()
        yaml_data = {
            "clients": {
                "default": {"max": 10.0, "starting": 10.0},
                "named-svc": {"max": 999.0, "starting": 999.0},
            }
        }
        sync_clients_from_yaml(yaml_data)
        limit = EntityLimit.query.filter_by(entity_id=client.id).first()
        assert float(limit.max_coins) == 999.0


def test_sync_clients_model_access(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.entity_model_access import EntityModelAccess
        from lumen.models.model_config import ModelConfig
        mc = ModelConfig(model_name="svc-model", input_cost_per_million=1.0, output_cost_per_million=1.0, active=True)
        client = Entity(entity_type="service", name="access-svc", initials="AS", active=True)
        db.session.add_all([mc, client])
        db.session.commit()
        yaml_data = {
            "clients": {
                "access-svc": {
                    "model_access": {"default": "blacklist", "whitelist": ["svc-model"]},
                }
            }
        }
        sync_clients_from_yaml(yaml_data)
        db.session.refresh(client)
        assert client.model_access_default == "blacklist"
        rule = EntityModelAccess.query.filter_by(entity_id=client.id, model_config_id=mc.id).first()
        assert rule is not None
        assert rule.allowed is True


def test_sync_global_model_access_creates_rule(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.global_model_access import GlobalModelAccess
        from lumen.models.model_config import ModelConfig
        mc = ModelConfig(model_name="access-model", input_cost_per_million=1.0, output_cost_per_million=1.0, active=True)
        db.session.add(mc)
        db.session.commit()
        yaml_data = {"model_access": {"blacklist": ["access-model"]}}
        sync_global_model_access_from_yaml(yaml_data)
        rule = GlobalModelAccess.query.filter_by(model_config_id=mc.id).first()
        assert rule is not None
        assert rule.access_type == "blacklist"


def test_sync_global_model_access_graylist(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.global_model_access import GlobalModelAccess
        from lumen.models.model_config import ModelConfig
        mc = ModelConfig(model_name="gray-access-model", input_cost_per_million=1.0, output_cost_per_million=1.0, active=True)
        db.session.add(mc)
        db.session.commit()
        yaml_data = {"model_access": {"graylist": ["gray-access-model"]}}
        sync_global_model_access_from_yaml(yaml_data)
        rule = GlobalModelAccess.query.filter_by(model_config_id=mc.id).first()
        assert rule is not None
        assert rule.access_type == "graylist"
