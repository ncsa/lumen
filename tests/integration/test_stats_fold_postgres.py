"""Two concurrent fold passes on PostgreSQL count each request_logs row once.

SQLite serialises writers, so the compare-and-set on ``stats_fold_state`` only
meets a real competitor here. A third connection holds the fold-state row
until both passes have read the same watermark and are blocked on their
UPDATE; when it lets go, one pass advances the watermark and folds, and the
other finds the watermark moved and rolls back.
"""

import os
import threading
import time
import uuid
from datetime import timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text

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


def test_concurrent_folds_count_each_row_once(pg_app, pg_migrated_isolated):
    from lumen.extensions import db
    from lumen.models.entity import Entity
    from lumen.models.entity_stat import EntityStat
    from lumen.models.model_config import ModelConfig
    from lumen.models.model_stat import ModelStat
    from lumen.models.request_log import RequestLog
    from lumen.services.stats_fold import fold_lag, fold_once, get_fold_state
    from lumen.timeutils import utcnow

    url, engine = pg_migrated_isolated
    lo = utcnow() - timedelta(hours=1)
    hi = lo + timedelta(minutes=30)
    with pg_app.app_context():
        tag = uuid.uuid4().hex[:8]
        entity = Entity(entity_type="user", email=f"fold-{tag}@example.com", name=f"Fold {tag}",
                        initials="FO", active=True)
        model = ModelConfig(model_name=f"fold-{tag}", input_cost_per_million=1.0, output_cost_per_million=1.0)
        db.session.add_all([entity, model])
        db.session.flush()
        entity_id, model_id = entity.id, model.id
        for minute in range(1, 6):
            db.session.add(RequestLog(
                time=(lo + timedelta(minutes=minute)).replace(tzinfo=timezone.utc), entity_id=entity_id,
                model_config_id=model_id, source="api", input_tokens=10, output_tokens=5, audio_seconds=1,
                cost=Decimal("0.5"), duration=0.0,
            ))
        db.session.execute(text("UPDATE stats_fold_state SET cutover_at = :lo, folded_until = :lo WHERE id = 1"),
                           {"lo": lo})
        db.session.commit()

    results, errors = [], []

    def worker():
        try:
            results.append(fold_once(pg_app, now=hi + fold_lag(pg_app)))
        except Exception as exc:  # collected and asserted on below
            errors.append(exc)

    blocker = create_engine(url)
    try:
        with blocker.connect() as conn:
            conn.execute(text("SELECT 1 FROM stats_fold_state WHERE id = 1 FOR UPDATE"))
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            # Both passes have read the same watermark once both wait on the row lock.
            with engine.connect() as probe:
                deadline = time.monotonic() + 30
                while probe.execute(text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )).scalar() < 2:
                    assert time.monotonic() < deadline, "the two fold passes never both blocked"
                    time.sleep(0.05)
            conn.commit()
            for t in threads:
                t.join(timeout=60)
    finally:
        blocker.dispose()

    assert errors == []
    assert sorted(results, key=lambda r: r is not None) == [None, hi]
    with pg_app.app_context():
        es = db.session.get(EntityStat, entity_id)
        ms = db.session.execute(select(ModelStat).filter_by(entity_id=entity_id, model_config_id=model_id)).scalar_one()
        for stat in (es, ms):
            assert (stat.requests, stat.input_tokens, stat.output_tokens, stat.audio_seconds, stat.cost) == (
                5, 50, 25, 5, Decimal("2.5"))
            assert stat.last_used_at == lo + timedelta(minutes=5)
        assert get_fold_state().folded_until == hi
