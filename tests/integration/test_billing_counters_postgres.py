"""Billing counters under real PostgreSQL concurrency.

SQLite serialises writers, so the first-use race the ON CONFLICT fallback
exists for only happens here: several requests for a fresh entity all find no
row with their UPDATE and all reach the INSERT. Each has to be counted once,
without an error. The steady-state check pins why the fallback is not a plain
upsert every time: Postgres draws the serial ``id`` before it detects the
conflict, so an upsert per request would burn a sequence value per request.
"""

import os
import threading
import uuid

import pytest
from sqlalchemy import select, text

from tests.conftest import no_snapshot_refresher
from tests.integration.conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres

THREADS = 8
STARTING = 100
EFFECTIVE = (float(STARTING), 0.0, float(STARTING))


@pytest.fixture(scope="module")
def pg_app(pg_migrated_isolated):
    """A Flask app bound to this module's migrated PostgreSQL database.

    ``config.Config`` reads ``DATABASE_URL`` at class-definition time and the root
    conftest's SQLite app has already imported it, so the attribute is patched too.
    """
    url = pg_migrated_isolated[0]
    previous_env = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    os.environ["CONFIG_YAML"] = TEST_CONFIG
    os.environ["BACKGROUND_WORKER"] = "false"
    from config import Config
    previous_uri = Config.SQLALCHEMY_DATABASE_URI
    Config.SQLALCHEMY_DATABASE_URI = url
    from lumen import create_app
    with pytest.MonkeyPatch.context() as mp:
        no_snapshot_refresher(mp)
        application = create_app()
    Config.SQLALCHEMY_DATABASE_URI = previous_uri
    application.config["TESTING"] = True
    with application.app_context():
        from lumen.extensions import db
        assert db.engine.dialect.name == "postgresql"
    yield application
    if previous_env is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous_env


@pytest.fixture
def fresh_entity(pg_app):
    """A new entity with a coin limit but no balance or stats rows yet, and a model."""
    from lumen.extensions import db
    from lumen.models.entity import Entity
    from lumen.models.entity_limit import EntityLimit
    from lumen.models.model_config import ModelConfig
    with pg_app.app_context():
        tag = uuid.uuid4().hex[:8]
        entity = Entity(entity_type="user", email=f"billing-{tag}@example.com",
                        name=f"Billing {tag}", initials="BI", active=True)
        model = ModelConfig(model_name=f"billing-{tag}", input_cost_per_million=1.0,
                            output_cost_per_million=1.0)
        db.session.add_all([entity, model])
        db.session.flush()
        db.session.add(EntityLimit(entity_id=entity.id, max_coins=STARTING, refresh_coins=0,
                                   starting_coins=STARTING))
        db.session.commit()
        return entity.id, model.id


def _bill(entity_id, model_id, cost):
    from lumen.extensions import db
    from lumen.services.llm import subtract_coins, update_stats
    update_stats(entity_id, model_id, "api", 10, 5, cost)
    subtract_coins(entity_id, model_id, cost, effective=EFFECTIVE)
    db.session.commit()


def _counters(entity_id, model_id):
    from lumen.extensions import db
    from lumen.models.entity_balance import EntityBalance
    from lumen.models.entity_stat import EntityStat
    from lumen.models.model_stat import ModelStat
    db.session.expire_all()
    ms = db.session.execute(select(ModelStat).filter_by(entity_id=entity_id, model_config_id=model_id)).scalar_one()
    es = db.session.get(EntityStat, entity_id)
    bal = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one()
    return ms, es, bal


def test_concurrent_first_requests_are_each_counted_once(pg_app, fresh_entity):
    entity_id, model_id = fresh_entity
    costs = [float(i) for i in range(1, THREADS + 1)]
    barrier = threading.Barrier(THREADS)
    errors = []

    def worker(cost):
        try:
            with pg_app.app_context():
                barrier.wait(timeout=30)
                _bill(entity_id, model_id, cost)
        except Exception as exc:  # collected and asserted on below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(c,)) for c in costs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    with pg_app.app_context():
        ms, es, bal = _counters(entity_id, model_id)
        assert ms.requests == THREADS
        assert es.requests == THREADS
        assert ms.input_tokens == es.input_tokens == 10 * THREADS
        assert float(ms.cost) == float(es.cost) == pytest.approx(sum(costs))
        assert float(bal.coins_left) == pytest.approx(STARTING - sum(costs))


def test_steady_state_billing_draws_no_sequence_values(pg_app, fresh_entity):
    entity_id, model_id = fresh_entity
    seq = text("SELECT last_value, is_called FROM model_stats_id_seq "
               "UNION ALL SELECT last_value, is_called FROM entity_balances_id_seq")
    with pg_app.app_context():
        from lumen.extensions import db
        _bill(entity_id, model_id, 1.0)  # first use creates the rows
        before = db.session.execute(seq).all()
        db.session.commit()

        for _ in range(10):
            _bill(entity_id, model_id, 1.0)

        assert db.session.execute(seq).all() == before
        ms, es, bal = _counters(entity_id, model_id)
        assert ms.requests == es.requests == 11
        assert float(bal.coins_left) == pytest.approx(STARTING - 11)
