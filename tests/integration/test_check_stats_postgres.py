"""``flask check-stats`` on PostgreSQL, where it shares an advisory lock with
``reassign-model`` and ``erase-entity``.

While either of those walks request_logs chunk by chunk, the rows already moved
count for their target before its folded part is merged, so a check then reports
drift that is not there and ``--fix`` counts it twice. A check that finds the lock
held skips: it prints ``Skipped`` and exits 0, without touching the fold state, so a
scheduled run simply waits for the next one. The SQLite behaviour is covered by
``tests/unit/test_check_stats_cmd.py``.
"""

import os
import uuid
from datetime import timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select, text

from lumen import commands
from lumen.commands import check_stats_cmd
from tests.conftest import no_snapshot_refresher

from .conftest import TEST_CONFIG, holding_stats_walk_lock, stats_walk_lock_is_free

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def pg_app(pg_migrated_isolated):
    """A Flask app bound to this module's migrated PostgreSQL database."""
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
def engine(pg_app):
    with pg_app.app_context():
        from lumen.extensions import db

        yield db.engine


def _late_row(pg_app):
    """A deferred request inside an already-folded window that no fold counted:
    the drift a billing transaction committing after stats.fold_lag leaves."""
    from lumen.extensions import db
    from lumen.models.entity import Entity
    from lumen.models.model_config import ModelConfig
    from lumen.models.request_log import RequestLog
    from lumen.timeutils import utcnow

    lo = utcnow() - timedelta(hours=1)
    with pg_app.app_context():
        tag = uuid.uuid4().hex[:8]
        entity = Entity(entity_type="user", email=f"check-{tag}@example.com", name=f"Check {tag}",
                        initials="CH", active=True)
        model = ModelConfig(model_name=f"check-{tag}", input_cost_per_million=1.0, output_cost_per_million=1.0)
        db.session.add_all([entity, model])
        db.session.flush()
        db.session.add(RequestLog(
            time=(lo + timedelta(minutes=5)).replace(tzinfo=timezone.utc), entity_id=entity.id,
            model_config_id=model.id, source="api", input_tokens=10, output_tokens=5, audio_seconds=0,
            cost=Decimal("0.5"), duration=0.0, stats_deferred=True,
        ))
        db.session.execute(text("UPDATE stats_fold_state SET cutover_at = :lo, folded_until = :hi WHERE id = 1"),
                           {"lo": lo, "hi": lo + timedelta(minutes=30)})
        db.session.commit()
        return entity.id, model.id


def _stats(pg_app, entity_id):
    from lumen.extensions import db
    from lumen.models.entity_stat import EntityStat
    from lumen.models.model_stat import ModelStat

    with pg_app.app_context():
        model = db.session.execute(select(ModelStat).filter_by(entity_id=entity_id)).scalars().all()
        entity = db.session.get(EntityStat, entity_id)
        return ([(s.model_config_id, s.requests, s.folded_requests) for s in model],
                None if entity is None else (entity.requests, entity.folded_requests))


def _check(pg_app, *args):
    return pg_app.test_cli_runner().invoke(check_stats_cmd, list(args))


def test_skips_without_the_fold_lock_while_the_lock_is_held(pg_app, engine):
    entity_id, _ = _late_row(pg_app)
    with holding_stats_walk_lock(engine), \
            patch.object(commands, "lock_fold_state", side_effect=AssertionError("took the fold lock")) as fold:
        result = _check(pg_app, "--fix")
    assert result.exit_code == 0, result.output
    assert result.output.startswith("Skipped: a reassign-model or erase-entity (or another check-stats) is running")
    assert not fold.called
    assert _stats(pg_app, entity_id) == ([], None), "the skipped run changed the stats"
    assert stats_walk_lock_is_free(engine)


def test_fixes_a_late_row_and_releases_the_lock(pg_app, engine):
    entity_id, model_id = _late_row(pg_app)

    result = _check(pg_app)
    assert result.exit_code == 1, result.output
    assert f"entity_stats entity_id={entity_id}: requests +1" in result.output
    assert stats_walk_lock_is_free(engine), "the failed check kept the advisory lock"

    result = _check(pg_app, "--fix")
    assert result.exit_code == 0, result.output
    assert "Skipped" not in result.output
    assert "Fixed" in result.output
    assert _stats(pg_app, entity_id) == ([(model_id, 1, 1)], (1, 1))
    assert stats_walk_lock_is_free(engine), "the successful check kept the advisory lock"

    result = _check(pg_app)
    assert result.exit_code == 0, result.output
    assert "match request_logs" in result.output
