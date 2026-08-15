"""Tests for YAML sync functions in lumen/commands.py."""
from datetime import datetime

from sqlalchemy import select

from lumen.commands import sync_groups_from_yaml, sync_models_from_yaml, sync_user_groups_from_yaml


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


def test_sync_groups_creates_group(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        yaml_data = {"groups": {"test-group": {}}}
        sync_groups_from_yaml(yaml_data)
        g = db.session.execute(select(Group).filter_by(name="test-group")).scalar_one_or_none()
        assert g is not None


def test_sync_groups_creates_group_with_limit(app):
    with app.app_context():
        from lumen.extensions import db
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
        g = db.session.execute(select(Group).filter_by(name="limited-group")).scalar_one_or_none()
        assert g is not None
        assert g.limit is not None
        assert float(g.limit.max_coins) == 100.0


def test_sync_groups_removes_limit_when_max_removed(app):
    """sync_groups_from_yaml deletes an existing GroupLimit when 'max' key is absent (else branch)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_limit import GroupLimit
        # First sync: create group with a token limit.
        sync_groups_from_yaml({"groups": {"rm-grp": {"max": 100, "refresh": 10, "starting": 100}}})
        g = db.session.execute(select(Group).filter_by(name="rm-grp")).scalar_one_or_none()
        assert db.session.execute(select(GroupLimit).filter_by(group_id=g.id)).scalar_one_or_none() is not None
        # Second sync: same group, no 'max' key — the existing GroupLimit should be deleted.
        sync_groups_from_yaml({"groups": {"rm-grp": {}}})
        db.session.expire_all()
        g = db.session.execute(select(Group).filter_by(name="rm-grp")).scalar_one_or_none()
        assert db.session.execute(select(GroupLimit).filter_by(group_id=g.id)).scalar_one_or_none() is None


def test_sync_groups_skips_unknown_model_in_access(app):
    """sync_groups_from_yaml logs a warning and skips model names not in the DB."""
    with app.app_context():
        yaml_data = {
            "groups": {
                "grp": {
                    "model_access": {"whitelist": ["no-such-model"]}
                }
            }
        }
        # Must not raise; the unknown model is silently skipped.
        sync_groups_from_yaml(yaml_data)


def test_sync_groups_legacy_graylist_sets_needs_ack(app):
    """A legacy scope graylist list sets needs_ack on the model (consent preserved on v1 load)."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.commands import sync_models_from_yaml, sync_groups_from_yaml
        from lumen.models.model_config import ModelConfig
        from sqlalchemy import select
        sync_models_from_yaml({"models": [
            {"name": "gl-model", "input_cost_per_million": 0, "output_cost_per_million": 0},
        ]})
        sync_groups_from_yaml({"groups": {"g": {"model_access": {"graylist": ["gl-model"]}}}})
        mc = db.session.execute(select(ModelConfig).filter_by(model_name="gl-model")).scalar_one()
        assert mc.needs_ack is True


# ---------------------------------------------------------------------------
# _normalize_access — config access vocabulary mapping (new + legacy)
# ---------------------------------------------------------------------------

def test_normalize_access_new_terms(app):
    from lumen.commands import _normalize_access
    with app.app_context():
        assert _normalize_access("allowed") == "allowed"
        assert _normalize_access("blocked") == "blocked"


def test_normalize_access_legacy_terms_map_and_warn(app, caplog):
    import logging
    from lumen.commands import _normalize_access, _warned
    with app.app_context():
        _warned.clear()
        with caplog.at_level(logging.WARNING):
            assert _normalize_access("whitelist", context="t1") == "allowed"
            assert _normalize_access("blacklist", context="t2") == "blocked"
            # legacy graylist at a scope only grants allowed; ack is a model property
            assert _normalize_access("graylist", context="t3") == "allowed"
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "deprecated access term" in msgs
        assert "needs_ack" in msgs  # graylist warning points to needs_ack


def test_normalize_access_unknown_returns_none(app):
    from lumen.commands import _normalize_access, _warned
    with app.app_context():
        _warned.clear()
        assert _normalize_access("bogus") is None
        assert _normalize_access(None) is None


def test_parse_scope_access_clean_v2_does_not_warn(app, caplog):
    """A fully-migrated allowed/blocked block must not emit legacy deprecation warnings."""
    import logging
    from lumen.commands import _parse_scope_access, _warned
    with app.app_context():
        _warned.clear()
        with caplog.at_level(logging.WARNING):
            pairs, default, ack = _parse_scope_access(
                {"default": "blocked", "allowed": ["m1"], "blocked": ["m2"]}, context="group 'g'")
        assert default == "blocked"
        assert set(pairs) == {("m1", "allowed"), ("m2", "blocked")}
        assert ack == []
        assert "deprecated access term" not in " ".join(r.getMessage() for r in caplog.records)


def test_parse_scope_access_legacy_keys_still_warn(app, caplog):
    """Legacy keys that are actually present still warn and map correctly."""
    import logging
    from lumen.commands import _parse_scope_access, _warned
    with app.app_context():
        _warned.clear()
        with caplog.at_level(logging.WARNING):
            pairs, _, ack = _parse_scope_access({"whitelist": ["m1"], "graylist": ["m2"]}, context="group 'g'")
        assert set(pairs) == {("m1", "allowed"), ("m2", "allowed")}
        assert ack == ["m2"]  # graylisted model surfaced for needs_ack backfill
        assert "deprecated access term" in " ".join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# _apply_model_fields / _apply_model_access — orthogonal access on the model
# ---------------------------------------------------------------------------

def test_apply_model_fields_sets_orthogonal_access(app):
    from lumen.commands import _apply_model_fields
    from lumen.models.model_config import ModelConfig
    with app.app_context():
        mc = ModelConfig(model_name="m")
        _apply_model_fields(mc, {
            "name": "m",
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 2.0,
            "access": "allowed",
            "needs_ack": True,
            "ack_message": "ack me",
        })
        assert mc.access == "allowed"
        assert mc.needs_ack is True
        assert mc.disabled is False
        assert mc.ack_message == "ack me"


def test_apply_model_access_omitted_is_none_inherit(app):
    """access omitted -> None (inherit group/global defaults at resolution time)."""
    from lumen.commands import _apply_model_fields
    from lumen.models.model_config import ModelConfig
    with app.app_context():
        mc = ModelConfig(model_name="m2")
        _apply_model_fields(mc, {"name": "m2", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0})
        assert mc.access is None
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


def test_apply_model_access_invalid_value_inherits(app):
    """An invalid access value is ignored -> None (inherit defaults)."""
    from lumen.commands import _apply_model_fields
    from lumen.models.model_config import ModelConfig
    with app.app_context():
        mc = ModelConfig(model_name="m4")
        _apply_model_fields(mc, {
            "name": "m4", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0,
            "access": "nonsense",
        })
        assert mc.access is None


def test_apply_model_access_explicit_disabled(app):
    from lumen.commands import _apply_model_fields
    from lumen.models.model_config import ModelConfig
    with app.app_context():
        mc = ModelConfig(model_name="m5")
        _apply_model_fields(mc, {
            "name": "m5", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0,
            "access": "allowed", "disabled": True,
        })
        assert mc.disabled is True
        assert mc.access == "allowed"


def _make_user(db, email):
    from lumen.models.entity import Entity
    e = Entity(entity_type="user", email=email, name=email, initials="U", active=True)
    db.session.add(e)
    db.session.commit()
    return e.id


def test_sync_user_groups_adds_explicit_group(app):
    """A user listed in users.<email>.groups gets the membership on reload, no login needed."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        db.session.add_all([
            Group(name="default", active=True, config_managed=True),
            Group(name="dev", active=True, config_managed=True),
        ])
        db.session.commit()
        uid = _make_user(db, "alice@example.com")

        yaml_data = {
            "users": {"alice@example.com": {"groups": ["dev"]}},
            "groups": {"default": {}, "dev": {}},
        }
        sync_user_groups_from_yaml(yaml_data)

        dev = db.session.execute(select(Group).filter_by(name="dev")).scalar_one()
        member = db.session.execute(
            select(GroupMember).filter_by(entity_id=uid, group_id=dev.id)
        ).scalar_one_or_none()
        assert member is not None
        assert member.config_managed is True


def test_sync_user_groups_removes_dropped_group(app):
    """Dropping a user from an explicit group removes the membership on reload."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        default = Group(name="default", active=True, config_managed=True)
        dev = Group(name="dev", active=True, config_managed=True)
        db.session.add_all([default, dev])
        db.session.commit()
        uid = _make_user(db, "bob@example.com")
        db.session.add(GroupMember(group_id=dev.id, entity_id=uid, config_managed=True))
        db.session.commit()

        # dev still defined as a group, but bob is no longer assigned to it.
        sync_user_groups_from_yaml({"users": {"bob@example.com": {}}, "groups": {"default": {}, "dev": {}}})

        member = db.session.execute(
            select(GroupMember).filter_by(entity_id=uid, group_id=dev.id)
        ).scalar_one_or_none()
        assert member is None


def test_sync_user_groups_leaves_auto_membership_untouched(app):
    """A rule-based (auto) group membership must survive a userinfo-less reload reconcile."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        default = Group(name="default", active=True, config_managed=True)
        auto = Group(name="uiuc", active=True, config_managed=True)
        db.session.add_all([default, auto])
        db.session.commit()
        uid = _make_user(db, "carol@example.com")
        # Membership added at a prior login by the rule-based path.
        db.session.add(GroupMember(group_id=auto.id, entity_id=uid, config_managed=True))
        db.session.commit()

        yaml_data = {
            "users": {"carol@example.com": {}},
            "groups": {"default": {}, "uiuc": {"rules": [{"field": "eppn", "contains": "@illinois.edu"}]}},
        }
        sync_user_groups_from_yaml(yaml_data)

        member = db.session.execute(
            select(GroupMember).filter_by(entity_id=uid, group_id=auto.id)
        ).scalar_one_or_none()
        assert member is not None


def test_sync_user_groups_default_groups_apply_to_all(app):
    """users.default.groups is applied to every user."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        db.session.add_all([
            Group(name="default", active=True, config_managed=True),
            Group(name="everyone", active=True, config_managed=True),
        ])
        db.session.commit()
        uid = _make_user(db, "dave@example.com")

        yaml_data = {
            "users": {"default": {"groups": ["everyone"]}},
            "groups": {"default": {}, "everyone": {}},
        }
        sync_user_groups_from_yaml(yaml_data)

        everyone = db.session.execute(select(Group).filter_by(name="everyone")).scalar_one()
        member = db.session.execute(
            select(GroupMember).filter_by(entity_id=uid, group_id=everyone.id)
        ).scalar_one_or_none()
        assert member is not None


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
