"""Two concurrent fold passes on PostgreSQL count each request_logs row once.

SQLite serialises writers, so the compare-and-set on ``stats_fold_state`` only
meets a real competitor here. A third connection holds the fold-state row
until both passes have read the same watermark and are blocked on their
UPDATE; when it lets go, one pass advances the watermark and folds, and the
other finds the watermark moved and rolls back.

The rolling-update case runs here too: pods of the previous release bump and
write unmarked rows next to new pods that mark theirs, on real PostgreSQL.
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
                cost=Decimal("0.5"), duration=0.0, stats_deferred=True,
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
                    " AND query LIKE 'UPDATE stats_fold_state %'"
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


def _old_pod_bill(entity_id, model_id, input_tokens, output_tokens, cost):
    """Bill the way the previous release does: an INSERT without ``stats_deferred``
    (so NULL), then both bumps, whatever the cutover says."""
    from lumen.extensions import db
    from lumen.models.entity_stat import EntityStat
    from lumen.models.model_stat import ModelStat
    from lumen.models.request_log import RequestLog
    from lumen.services.llm import _bump
    from lumen.timeutils import utcnow

    now = utcnow()
    db.session.add(RequestLog(
        time=now.replace(tzinfo=timezone.utc), entity_id=entity_id, model_config_id=model_id, source="api",
        input_tokens=input_tokens, output_tokens=output_tokens, cost=cost, duration=0.0,
    ))
    db.session.flush()
    for model, key in ((ModelStat, {"entity_id": entity_id, "model_config_id": model_id, "source": "api"}),
                       (EntityStat, {"entity_id": entity_id})):
        _bump(
            model, key,
            {"requests": model.requests + 1, "input_tokens": model.input_tokens + input_tokens,
             "output_tokens": model.output_tokens + output_tokens, "cost": model.cost + cost,
             "last_used_at": now},
            {"requests": 1, "input_tokens": input_tokens, "output_tokens": output_tokens, "cost": cost,
             "last_used_at": now},
        )
    db.session.commit()


def test_rolling_update_counts_every_request_once(pg_app):
    """Old and new pods bill side by side after the migration's automatic cutover.

    The old pods bump and write unmarked rows; the new pods (real ``bill_request``)
    mark theirs. After a fold the totals are every request exactly once.
    """
    from lumen.extensions import db
    from lumen.models.entity import Entity
    from lumen.models.entity_stat import EntityStat
    from lumen.models.model_config import ModelConfig
    from lumen.models.model_stat import ModelStat
    from lumen.models.request_log import RequestLog
    from lumen.services.llm import bill_request
    from lumen.services.stats_fold import fold_lag, fold_once, get_fold_state
    from lumen.timeutils import utcnow

    with pg_app.app_context():
        assert get_fold_state().cutover_at is not None  # set by the migration
        tag = uuid.uuid4().hex[:8]
        entity = Entity(entity_type="user", email=f"roll-{tag}@example.com", name=f"Roll {tag}",
                        initials="RO", active=True)
        model = ModelConfig(model_name=f"roll-{tag}", input_cost_per_million=1.0, output_cost_per_million=1.0)
        db.session.add_all([entity, model])
        db.session.commit()
        entity_id, model_id = entity.id, model.id

        for i in range(4):
            _old_pod_bill(entity_id, model_id, 10, 5, Decimal("0.5"))
            bill_request(entity_id, model_id, "api", 20, 7, 0.25, effective=None)
            if i % 2:
                _old_pod_bill(entity_id, model_id, 1, 1, Decimal("0.125"))
        markers = db.session.execute(
            select(RequestLog.stats_deferred).where(RequestLog.entity_id == entity_id)
        ).scalars().all()
        assert sorted(markers, key=lambda m: m is True) == [None] * 6 + [True] * 4

    assert fold_once(pg_app, now=utcnow() + fold_lag(pg_app) + timedelta(seconds=1)) is not None

    with pg_app.app_context():
        es = db.session.get(EntityStat, entity_id)
        ms = db.session.execute(select(ModelStat).filter_by(entity_id=entity_id, model_config_id=model_id)).scalar_one()
        for stat in (es, ms):
            assert stat.requests == 10
            assert stat.input_tokens == 4 * 10 + 4 * 20 + 2 * 1
            assert stat.output_tokens == 4 * 5 + 4 * 7 + 2 * 1
            assert stat.cost == 4 * Decimal("0.5") + 4 * Decimal("0.25") + 2 * Decimal("0.125")


def _entity_and_model(prefix):
    from lumen.extensions import db
    from lumen.models.entity import Entity
    from lumen.models.model_config import ModelConfig
    tag = uuid.uuid4().hex[:8]
    entity = Entity(entity_type="user", email=f"{prefix}-{tag}@example.com", name=f"{prefix} {tag}",
                    initials="FO", active=True)
    model = ModelConfig(model_name=f"{prefix}-{tag}", input_cost_per_million=1.0, output_cost_per_million=1.0)
    db.session.add_all([entity, model])
    db.session.flush()
    return entity.id, model.id


def _log(entity_id, model_id, at, source="api"):
    from lumen.extensions import db
    from lumen.models.request_log import RequestLog
    db.session.add(RequestLog(
        time=at.replace(tzinfo=timezone.utc), entity_id=entity_id, model_config_id=model_id, source=source,
        input_tokens=10, output_tokens=5, audio_seconds=1, cost=Decimal("0.5"), duration=0.0,
    ))


def _cut_over_at(lo):
    from lumen.extensions import db
    db.session.execute(text("UPDATE stats_fold_state SET cutover_at = :lo, folded_until = :lo WHERE id = 1"),
                       {"lo": lo})


def test_a_fold_waits_for_the_fold_state_lock(pg_app, pg_migrated_isolated):
    """reassign-model and check-stats hold the row; a pass blocks until they finish."""
    from lumen.extensions import db
    from lumen.services.stats_cutover import lock_fold_state
    from lumen.services.stats_fold import fold_lag, fold_once
    from lumen.timeutils import utcnow

    _, engine = pg_migrated_isolated
    lo = utcnow() - timedelta(hours=1)
    hi = lo + timedelta(minutes=30)
    with pg_app.app_context():
        _cut_over_at(lo)
        db.session.commit()

    results = []
    with pg_app.app_context():
        assert lock_fold_state().folded_until == lo
        thread = threading.Thread(target=lambda: results.append(fold_once(pg_app, now=hi + fold_lag(pg_app))))
        thread.start()
        with engine.connect() as probe:
            deadline = time.monotonic() + 30
            while not probe.execute(text(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                " AND query LIKE 'UPDATE stats_fold_state %'"
            )).scalar():
                # pg_stat_activity is a snapshot per transaction: end it to see the next one.
                probe.rollback()
                assert time.monotonic() < deadline, "the fold pass never waited on the lock"
                time.sleep(0.05)
        assert results == []
        db.session.commit()
    thread.join(timeout=60)
    assert results == [hi]


def test_check_stats_repairs_a_late_commit(pg_app):
    """A row committed inside an already-folded window is found and added."""
    from lumen.commands import check_stats_cmd
    from lumen.extensions import db
    from lumen.models.entity_stat import EntityStat
    from lumen.models.model_stat import ModelStat
    from lumen.services.stats_fold import fold_lag, fold_once
    from lumen.timeutils import utcnow

    lo = utcnow() - timedelta(hours=1)
    hi = lo + timedelta(minutes=30)
    with pg_app.app_context():
        # Rows earlier tests in this module folded count as drift for this
        # cutover, so they are cleared first.
        db.session.execute(text("DELETE FROM request_logs"))
        db.session.execute(text("DELETE FROM model_stats"))
        db.session.execute(text("DELETE FROM entity_stats"))
        entity_id, model_id = _entity_and_model("check")
        _cut_over_at(lo)
        _log(entity_id, model_id, lo)
        db.session.commit()
    fold_once(pg_app, now=hi + fold_lag(pg_app))
    with pg_app.app_context():
        _log(entity_id, model_id, hi - timedelta(seconds=1))
        db.session.commit()

    def check(*args):
        # Under an explicit app context: with_appcontext reuses whatever app
        # is current, and only this one is bound to PostgreSQL.
        with pg_app.app_context():
            return pg_app.test_cli_runner().invoke(check_stats_cmd, list(args))

    result = check()
    assert result.exit_code == 1, result.output
    assert "2 row(s) differ" in result.output
    result = check("--fix")
    assert result.exit_code == 0, result.output
    assert check().exit_code == 0
    with pg_app.app_context():
        model = db.session.execute(select(ModelStat).filter_by(entity_id=entity_id)).scalar_one()
        entity = db.session.get(EntityStat, entity_id)
        for stat in (model, entity):
            assert (stat.requests, stat.folded_requests, stat.cost, stat.folded_cost) == (
                2, 2, Decimal("1.0"), Decimal("1.0"))
            assert stat.last_used_at == hi - timedelta(seconds=1)


def test_usage_summary_last_active_includes_the_tail(pg_app):
    """The user's own /usage summary shows a request the fold has not reached."""
    from lumen.extensions import db
    from lumen.services.stats_fold import fold_lag, fold_once
    from lumen.timeutils import utcnow

    lo = utcnow() - timedelta(hours=1)
    with pg_app.app_context():
        entity_id, model_id = _entity_and_model("tail")
        _cut_over_at(lo)
        db.session.commit()
    fold_once(pg_app, now=utcnow() + fold_lag(pg_app))
    with pg_app.app_context():
        last = utcnow().replace(microsecond=0) + timedelta(seconds=5)
        _log(entity_id, model_id, last)
        db.session.commit()

    client = pg_app.test_client()
    with client.session_transaction() as sess:
        sess["entity_id"] = entity_id
    body = client.get("/api/usage/summary?period=all").get_json()
    assert body["last_active"] == last.isoformat() + "Z"
