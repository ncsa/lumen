"""Tests for model aliases: the resolver, config validation, and YAML sync.

Aliases let a client keep requesting an old model name after its canonical model
is upgraded (e.g. ``glm-5.2`` -> ``glm-5.3-flash``). They resolve to the target
canonical ModelConfig so every policy, billing, and metrics decision uses the
canonical identity.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from lumen.extensions import db


def _make_config(app, model_name, disabled=False):
    with app.app_context():
        from lumen.models.model_config import ModelConfig
        mc = ModelConfig(
            model_name=model_name,
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
            disabled=disabled,
        )
        db.session.add(mc)
        db.session.commit()
        db.session.refresh(mc)
        return mc.id


def _add_alias(app, alias, model_config_id):
    with app.app_context():
        from lumen.models.model_alias import ModelAlias
        db.session.add(ModelAlias(alias=alias, model_config_id=model_config_id))
        db.session.commit()


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def test_resolve_canonical_by_name(app):
    mc_id = _make_config(app, "glm-5.3-flash")
    with app.app_context():
        from lumen.services.model_resolver import resolve_model_config
        assert resolve_model_config("glm-5.3-flash").id == mc_id


def test_resolve_alias_to_canonical(app):
    mc_id = _make_config(app, "glm-5.3-flash")
    _add_alias(app, "glm-5.2", mc_id)
    with app.app_context():
        from lumen.services.model_resolver import resolve_model_config
        assert resolve_model_config("glm-5.2").id == mc_id


def test_resolve_unknown_returns_none(app):
    _make_config(app, "glm-5.3-flash")
    with app.app_context():
        from lumen.services.model_resolver import resolve_model_config
        assert resolve_model_config("does-not-exist") is None
        assert resolve_model_config("") is None
        assert resolve_model_config(None) is None


def test_resolve_disabled_target_not_resolved(app):
    """An alias must never fall back to a disabled/retired target."""
    mc_id = _make_config(app, "retired-model", disabled=True)
    _add_alias(app, "old-name", mc_id)
    with app.app_context():
        from lumen.services.model_resolver import resolve_model_config
        assert resolve_model_config("retired-model") is None
        assert resolve_model_config("old-name") is None


def test_resolve_target_past_end_date_not_resolved(app):
    """An alias must never fall back to a target whose end date has passed."""
    from lumen.timeutils import utcnow
    with app.app_context():
        from lumen.models.model_config import ModelConfig
        mc = ModelConfig(
            model_name="expired-model",
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
            end_date=utcnow() - timedelta(days=1),
        )
        db.session.add(mc)
        db.session.commit()
        db.session.refresh(mc)
        mc_id = mc.id
    _add_alias(app, "old-name", mc_id)
    with app.app_context():
        from lumen.services.model_resolver import resolve_model_config
        assert resolve_model_config("expired-model") is None
        assert resolve_model_config("old-name") is None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _valid_models():
    return [{
        "name": "glm-5.3-flash",
        "aliases": ["glm", "glm-5.2"],
        "input_cost_per_million": 1.0,
        "output_cost_per_million": 2.0,
    }]


def test_valid_aliases_pass(app):
    from lumen.commands import model_alias_config_errors
    assert model_alias_config_errors(_valid_models()) == []


def test_alias_must_be_nonempty_string(app):
    from lumen.commands import model_alias_config_errors
    errs = model_alias_config_errors([{**_valid_models()[0], "aliases": [""]}])
    assert any("non-empty string" in e for e in errs)
    errs = model_alias_config_errors([{**_valid_models()[0], "aliases": [123]}])
    assert any("non-empty string" in e for e in errs)
    # Whitespace-only aliases are not meaningful and must be rejected too.
    errs = model_alias_config_errors([{**_valid_models()[0], "aliases": ["   "]}])
    assert any("non-empty string" in e for e in errs)


def test_duplicate_alias_rejected(app):
    from lumen.commands import model_alias_config_errors
    models = _valid_models() + [{
        "name": "other-model",
        "aliases": ["glm-5.2"],
        "input_cost_per_million": 1.0,
        "output_cost_per_million": 2.0,
    }]
    errs = model_alias_config_errors(models)
    assert any("duplicate alias 'glm-5.2'" in e for e in errs)


def test_self_alias_rejected(app):
    from lumen.commands import model_alias_config_errors
    errs = model_alias_config_errors([{"name": "glm-5.3-flash", "aliases": ["glm-5.3-flash"],
                                       "input_cost_per_million": 1.0, "output_cost_per_million": 2.0}])
    assert any("cannot equal" in e for e in errs)


def test_alias_colliding_with_any_canonical_name_rejected(app):
    from lumen.commands import model_alias_config_errors
    models = _valid_models() + [{
        "name": "glm",
        "input_cost_per_million": 1.0,
        "output_cost_per_million": 2.0,
    }]
    errs = model_alias_config_errors(models)
    assert any("collides with model name 'glm'" in e for e in errs)


def test_alias_too_long_rejected(app):
    from lumen.commands import model_alias_config_errors
    errs = model_alias_config_errors([{**_valid_models()[0], "aliases": ["x" * 129]}])
    assert any("exceeds" in e for e in errs)


def test_alias_must_be_list(app):
    from lumen.commands import model_alias_config_errors
    errs = model_alias_config_errors([{**_valid_models()[0], "aliases": "glm"}])
    assert any("must be a list" in e for e in errs)


def test_validate_config_structure_rejects_bad_aliases(app):
    from lumen.services.config_watcher import validate_config_structure
    cfg = {"version": 3, "models": [{"name": "a", "aliases": ["a"],
                                     "input_cost_per_million": 1,
                                     "output_cost_per_million": 1}]}
    assert any("cannot equal" in e for e in validate_config_structure(cfg))


def test_validate_model_aliases_raises(app):
    from lumen.commands import validate_model_aliases
    with pytest.raises(ValueError):
        validate_model_aliases({"models": [{"name": "a", "aliases": ["a"]}]})
    validate_model_aliases({"models": _valid_models()})


def test_validate_model_aliases_without_models_key(app):
    """A config without a ``models`` key must validate cleanly, not raise."""
    from lumen.commands import model_alias_config_errors, validate_model_aliases
    validate_model_aliases({})
    validate_model_aliases({"version": 3})
    validate_model_aliases(None)
    assert model_alias_config_errors([]) == []


# ---------------------------------------------------------------------------
# YAML sync
# ---------------------------------------------------------------------------

def _sync(app, models):
    with app.app_context():
        from lumen.commands import sync_models_from_yaml
        sync_models_from_yaml({"models": models})


def test_sync_creates_aliases(app):
    _sync(app, [{
        "name": "glm-5.3-flash",
        "aliases": ["glm", "glm-5.2"],
        "input_cost_per_million": 1.0,
        "output_cost_per_million": 2.0,
        "endpoints": [{"url": "http://localhost:9999/v1", "api_key": "k"}],
    }])
    with app.app_context():
        from lumen.models.model_alias import ModelAlias
        from lumen.models.model_config import ModelConfig
        mc = db.session.execute(select(ModelConfig).filter_by(model_name="glm-5.3-flash")).scalar_one()
        aliases = db.session.execute(select(ModelAlias.alias).where(ModelAlias.model_config_id == mc.id)).scalars().all()
        assert sorted(aliases) == ["glm", "glm-5.2"]


def test_sync_removes_and_retargets_aliases(app):
    _sync(app, [{"name": "m1", "aliases": ["a", "b"],
                 "input_cost_per_million": 1.0, "output_cost_per_million": 2.0}])
    _sync(app, [{"name": "m1", "aliases": ["b"],
                 "input_cost_per_million": 1.0, "output_cost_per_million": 2.0}])
    with app.app_context():
        from lumen.models.model_alias import ModelAlias
        names = db.session.execute(select(ModelAlias.alias)).scalars().all()
        assert names == ["b"]

    # Retarget alias 'b' onto a new canonical model.
    _sync(app, [{"name": "m1", "input_cost_per_million": 1.0, "output_cost_per_million": 2.0},
                {"name": "m2", "aliases": ["b"],
                 "input_cost_per_million": 1.0, "output_cost_per_million": 2.0}])
    with app.app_context():
        from lumen.models.model_alias import ModelAlias
        row = db.session.execute(select(ModelAlias).filter_by(alias="b")).scalar_one()
        assert row.model_config.model_name == "m2"


def test_sync_keeps_retired_row_when_its_name_becomes_an_alias(app):
    """A retired canonical row whose name becomes an alias elsewhere is kept
    disabled and the alias resolves to the new target, not the retired row."""
    _sync(app, [{"name": "glm-5.2", "input_cost_per_million": 1.0, "output_cost_per_million": 2.0}])
    _sync(app, [{"name": "glm-5.3-flash", "aliases": ["glm-5.2"],
                 "input_cost_per_million": 1.0, "output_cost_per_million": 2.0}])
    with app.app_context():
        from lumen.models.model_config import ModelConfig
        from lumen.services.model_resolver import resolve_model_config
        retired = db.session.execute(select(ModelConfig).filter_by(model_name="glm-5.2")).scalar_one()
        assert retired.disabled is True
        resolved = resolve_model_config("glm-5.2")
        assert resolved is not None
        assert resolved.model_name == "glm-5.3-flash"
        assert retired.id != resolved.id


def test_sync_invalid_aliases_rejected_before_any_writes(app):
    """An invalid alias config aborts the whole sync: no model is written."""
    bad = [{"name": "good-model", "aliases": ["dup"],
            "input_cost_per_million": 1.0, "output_cost_per_million": 2.0},
           {"name": "other", "aliases": ["dup"],
            "input_cost_per_million": 1.0, "output_cost_per_million": 2.0}]
    with app.app_context():
        from lumen.commands import sync_models_from_yaml
        with pytest.raises(ValueError):
            sync_models_from_yaml({"models": bad})
    with app.app_context():
        from lumen.models.model_config import ModelConfig
        assert db.session.execute(select(ModelConfig)).scalars().all() == []


def test_sync_empty_config_is_compatible(app):
    """Syncing a config with no models is valid and disables every model."""
    _sync(app, [{"name": "m1", "aliases": ["a"],
                 "input_cost_per_million": 1.0, "output_cost_per_million": 2.0}])
    _sync(app, [])
    with app.app_context():
        from lumen.models.model_config import ModelConfig
        m1 = db.session.execute(select(ModelConfig).filter_by(model_name="m1")).scalar_one()
        assert m1.disabled is True
