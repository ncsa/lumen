"""The coin refiller under real PostgreSQL row locking.

SQLite has no row locks, so the point of batching — a committed batch releases
its balance rows before the next batch starts — only shows here. Before
batching, the first due row stayed locked until the whole pass committed, and a
billing request for that entity waited behind every other refill in the pass.
"""

import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from tests.conftest import no_snapshot_refresher
from tests.integration.conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres


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


def _due_entities(pg_app, count, now):
    """``count`` new entities with a 100/10 limit and a balance of 0 refilled 2 h before ``now``."""
    from lumen.extensions import db
    from lumen.models.entity import Entity
    from lumen.models.entity_balance import EntityBalance
    from lumen.models.entity_limit import EntityLimit
    with pg_app.app_context():
        tag = uuid.uuid4().hex[:8]
        entities = [
            Entity(entity_type="user", email=f"refill-{tag}-{i}@example.com",
                   name=f"Refill {tag} {i}", initials="RF", active=True)
            for i in range(count)
        ]
        db.session.add_all(entities)
        db.session.flush()
        for e in entities:
            db.session.add(EntityLimit(entity_id=e.id, max_coins=100, refresh_coins=10, starting_coins=0))
            db.session.add(EntityBalance(entity_id=e.id, coins_left=0,
                                         last_refill_at=now - timedelta(hours=2)))
        db.session.commit()
        return sorted(e.id for e in entities)


def _coins(pg_app, ids):
    from lumen.extensions import db
    from lumen.models.entity_balance import EntityBalance
    with pg_app.app_context():
        rows = db.session.execute(
            select(EntityBalance.entity_id, EntityBalance.coins_left)
            .where(EntityBalance.entity_id.in_(ids))
        ).all()
        return {eid: float(coins) for eid, coins in rows}


def test_committed_batch_releases_its_rows(pg_app, pg_migrated_isolated, monkeypatch):
    """While batch 2 runs, the batch-1 balance row can already be locked by billing."""
    from lumen.services import token_refill
    from lumen.timeutils import utcnow

    engine = pg_migrated_isolated[1]
    now = utcnow()
    ids = _due_entities(pg_app, 3, now)

    monkeypatch.setattr(token_refill, "REFILL_BATCH_SIZE", 1)
    real_apply = token_refill._apply_batch
    probed = []

    def probe_then_apply(stmt, params):
        if params[0]["b_entity_id"] == ids[1]:
            # NOWAIT raises LockNotAvailable if batch 1 still holds its row.
            with engine.connect() as other:
                locked = other.execute(text(
                    "SELECT entity_id FROM entity_balances WHERE entity_id = :eid FOR UPDATE NOWAIT"
                ), {"eid": ids[0]}).scalar_one()
                other.rollback()
            probed.append(locked)
        return real_apply(stmt, params)

    monkeypatch.setattr(token_refill, "_apply_batch", probe_then_apply)
    with pg_app.app_context():
        assert token_refill.refill_coin_balances(now=now) == 3

    assert probed == [ids[0]]
    assert _coins(pg_app, ids) == {eid: 20.0 for eid in ids}


def test_batch_rowcount_counts_every_credited_row(pg_app):
    """A multi-row executemany batch reports every credited row, not just the last."""
    from lumen.services import token_refill
    from lumen.timeutils import utcnow

    now = utcnow() + timedelta(minutes=1)
    ids = _due_entities(pg_app, 5, now)

    with pg_app.app_context():
        assert token_refill.refill_coin_balances(now=now) == 5

    assert _coins(pg_app, ids) == {eid: 20.0 for eid in ids}
