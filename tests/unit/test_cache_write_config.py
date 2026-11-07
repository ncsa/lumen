"""Cache-write model pricing in YAML validation and database sync."""

import pytest
from sqlalchemy import select

from lumen.commands import sync_models_from_yaml
from lumen.extensions import db
from lumen.models.model_config import ModelConfig
from lumen.services.config_watcher import validate_config_structure


@pytest.mark.parametrize("write_rate", [None, 0, 0.25])
def test_cache_write_rate_accepts_null_zero_and_positive(write_rate):
    model = {"name": "write-price", "input_cost_per_million": 1.0,
             "output_cost_per_million": 2.0, "cache_write_cost_per_million": write_rate}
    assert validate_config_structure({"version": 3, "models": [model]}) == []
    model.pop("cache_write_cost_per_million")
    assert validate_config_structure({"version": 3, "models": [model]}) == []


@pytest.mark.parametrize("write_rate", [True, "0.25", -0.1, float("nan"), float("inf")])
def test_cache_write_rate_rejects_invalid_values(write_rate):
    model = {"name": "write-price", "input_cost_per_million": 1.0,
             "output_cost_per_million": 2.0, "cache_write_cost_per_million": write_rate}
    assert any("cache_write_cost_per_million" in error
               for error in validate_config_structure({"version": 3, "models": [model]}))


def test_cache_write_rate_syncs_and_clears_when_omitted(app):
    model = {"name": "write-price", "input_cost_per_million": 1.0,
             "output_cost_per_million": 2.0, "cache_write_cost_per_million": 0}
    with app.app_context():
        sync_models_from_yaml({"models": [model]})
        config = db.session.execute(select(ModelConfig).filter_by(model_name="write-price")).scalar_one()
        assert config.cache_write_cost_per_million == 0

        model["cache_write_cost_per_million"] = 0.25
        sync_models_from_yaml({"models": [model]})
        db.session.refresh(config)
        assert float(config.cache_write_cost_per_million) == 0.25

        model.pop("cache_write_cost_per_million")
        sync_models_from_yaml({"models": [model]})
        db.session.refresh(config)
        assert config.cache_write_cost_per_million is None
