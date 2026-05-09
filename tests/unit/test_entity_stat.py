"""Tests for EntityStat maintenance in update_stats()."""
import pytest


def test_update_stats_creates_entity_stat(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_stat import EntityStat
        from lumen.services.llm import update_stats

        update_stats(entity_id, model_id, "chat", 100, 50, 0.01)
        db.session.commit()

        estat = db.session.get(EntityStat, entity_id)
        assert estat is not None
        assert estat.requests == 1
        assert estat.input_tokens == 100
        assert estat.output_tokens == 50
        assert float(estat.cost) == pytest.approx(0.01)
        assert estat.last_used_at is not None


def test_update_stats_increments_entity_stat(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_stat import EntityStat
        from lumen.services.llm import update_stats

        update_stats(entity_id, model_id, "chat", 100, 50, 0.01)
        db.session.commit()
        update_stats(entity_id, model_id, "api", 200, 75, 0.02)
        db.session.commit()

        estat = db.session.get(EntityStat, entity_id)
        assert estat.requests == 2
        assert estat.input_tokens == 300
        assert estat.output_tokens == 125
        assert float(estat.cost) == pytest.approx(0.03)


def test_update_stats_entity_stat_aggregates_across_models(app, test_user, test_model):
    """entity_stats sums across all models, unlike model_stats which is per-model."""
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_stat import EntityStat
        from lumen.models.model_config import ModelConfig
        from lumen.services.llm import update_stats

        m2 = ModelConfig(model_name="other-model", input_cost_per_million=1.0,
                         output_cost_per_million=2.0, active=True)
        db.session.add(m2)
        db.session.commit()

        update_stats(entity_id, model_id, "chat", 100, 0, 0.01)
        db.session.commit()
        update_stats(entity_id, m2.id, "chat", 200, 0, 0.02)
        db.session.commit()

        estat = db.session.get(EntityStat, entity_id)
        assert estat.requests == 2
        assert estat.input_tokens == 300
        assert float(estat.cost) == pytest.approx(0.03)


def test_update_stats_last_used_at_advances(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_stat import EntityStat
        from lumen.services.llm import update_stats

        update_stats(entity_id, model_id, "chat", 10, 5, 0.001)
        db.session.commit()
        t1 = db.session.get(EntityStat, entity_id).last_used_at

        update_stats(entity_id, model_id, "chat", 10, 5, 0.001)
        db.session.commit()
        t2 = db.session.get(EntityStat, entity_id).last_used_at

        assert t2 >= t1
