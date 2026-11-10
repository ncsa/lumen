"""Cached-input model pricing in YAML validation and database sync."""

import pytest
from sqlalchemy import select

from lumen.commands import sync_models_from_yaml
from lumen.extensions import db
from lumen.models.model_config import ModelConfig
from lumen.services.config_watcher import validate_config_structure


@pytest.mark.parametrize("cached_rate", [None, 0, 0.25])
def test_cached_input_rate_accepts_null_zero_and_positive(cached_rate):
    model = {"name": "cached-price", "input_cost_per_million": 1.0,
             "output_cost_per_million": 2.0, "cached_input_cost_per_million": cached_rate}
    assert validate_config_structure({"version": 3, "models": [model]}) == []
    model.pop("cached_input_cost_per_million")
    assert validate_config_structure({"version": 3, "models": [model]}) == []


@pytest.mark.parametrize("cached_rate", [True, "0.25", -0.1, float("nan"), float("inf")])
def test_cached_input_rate_rejects_invalid_values(cached_rate):
    model = {"name": "cached-price", "input_cost_per_million": 1.0,
             "output_cost_per_million": 2.0, "cached_input_cost_per_million": cached_rate}
    assert any("cached_input_cost_per_million" in error
               for error in validate_config_structure({"version": 3, "models": [model]}))


def test_cached_input_rate_syncs_and_clears_when_omitted(app):
    model = {"name": "cached-price", "input_cost_per_million": 1.0,
             "output_cost_per_million": 2.0, "cached_input_cost_per_million": 0}
    with app.app_context():
        sync_models_from_yaml({"models": [model]})
        config = db.session.execute(select(ModelConfig).filter_by(model_name="cached-price")).scalar_one()
        assert config.cached_input_cost_per_million == 0

        model["cached_input_cost_per_million"] = 0.25
        sync_models_from_yaml({"models": [model]})
        db.session.refresh(config)
        assert float(config.cached_input_cost_per_million) == 0.25

        model.pop("cached_input_cost_per_million")
        sync_models_from_yaml({"models": [model]})
        db.session.refresh(config)
        assert config.cached_input_cost_per_million is None
