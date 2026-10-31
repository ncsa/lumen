"""``flask reassign-model`` after the stats cutover.

The command merges the source model's ``model_stats`` rows into the target's and
moves its ``request_logs`` rows, holding the fold state meanwhile, so a fold
pass sees either the models before the move or after it, never half of each.
"""
from datetime import timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from lumen.commands import check_stats_cmd, reassign_model_cmd
from lumen.extensions import db
from lumen.models.entity import Entity
from lumen.models.entity_stat import EntityStat
from lumen.models.model_config import ModelConfig
from lumen.models.model_stat import ModelStat
from lumen.models.request_log import RequestLog
from lumen.services import stats_cutover
from lumen.timeutils import utcnow
from tests.conftest import fold


@pytest.fixture
def ids(app):
    with app.app_context():
        entity = Entity(entity_type="user", email="reassign@example.com", name="Reassign",
                        initials="RE", active=True)
        src = ModelConfig(model_name="reassign-src", input_cost_per_million=1.0, output_cost_per_million=1.0)
        dst = ModelConfig(model_name="reassign-dst", input_cost_per_million=1.0, output_cost_per_million=1.0)
        db.session.add_all([entity, src, dst])
        db.session.commit()
        return entity.id, src.id, dst.id


def _bill(app, entity_id, model_id, source="api", audio_seconds=0):
    """A billed request after the cutover: a request_logs row and nothing else.

    Stamped at the watermark, so the next fold pass is the one that counts it
    (``fold`` moves the watermark a second past now).
    """
    with app.app_context():
        at = max(utcnow(), stats_cutover.get_fold_state().folded_until)
        db.session.add(RequestLog(
            time=at.replace(tzinfo=timezone.utc), entity_id=entity_id, model_config_id=model_id,
            source=source, input_tokens=10, output_tokens=5, audio_seconds=audio_seconds,
            cost=Decimal("0.5"), duration=0.0, stats_deferred=True,
        ))
        db.session.commit()


def _model_stats(app):
    with app.app_context():
        return {
            (s.model_config_id, s.source): (s.requests, s.input_tokens, s.output_tokens, s.audio_seconds,
                                            s.cost, s.folded_requests, s.folded_audio_seconds)
            for s in db.session.execute(select(ModelStat)).scalars()
        }


def _reassign(app, src, dst):
    result = app.test_cli_runner().invoke(reassign_model_cmd, [str(src), str(dst)])
    assert result.exit_code == 0, result.output
    return result


def test_reassign_between_billing_and_a_fold(app, post_cutover, ids):
    entity_id, src, dst = ids
    # Folded already: both models have a row for the api source.
    _bill(app, entity_id, src, audio_seconds=3)
    _bill(app, entity_id, dst)
    fold(app)
    # Billed but not folded yet, then reassigned before the next pass.
    _bill(app, entity_id, src, audio_seconds=4)
    _bill(app, entity_id, src, source="chat")
    _reassign(app, src, dst)
    fold(app)

    assert _model_stats(app) == {
        (dst, "api"): (3, 30, 15, 7, Decimal("1.5"), 3, 7),
        (dst, "chat"): (1, 10, 5, 0, Decimal("0.5"), 1, 0),
    }
    with app.app_context():
        entity = db.session.get(EntityStat, entity_id)
        assert (entity.requests, entity.audio_seconds) == (4, 7)
    result = app.test_cli_runner().invoke(check_stats_cmd, [])
    assert result.exit_code == 0, result.output


def test_reassign_merges_audio_seconds(app, ids):
    """Before the cutover too: a merged row keeps both models' audio seconds."""
    entity_id, src, dst = ids
    with app.app_context():
        for model_id, seconds in ((src, 5), (dst, 6)):
            db.session.add(ModelStat(entity_id=entity_id, model_config_id=model_id, source="api", requests=1,
                                     input_tokens=1, output_tokens=1, audio_seconds=seconds, cost=Decimal("1")))
        db.session.commit()
    _reassign(app, src, dst)
    assert _model_stats(app) == {(dst, "api"): (2, 2, 2, 11, Decimal("2"), 0, 0)}


def test_reassign_holds_the_fold_state(app, post_cutover, ids, monkeypatch):
    """The fold state is locked first, before the command reads anything."""
    entity_id, src, dst = ids
    calls = []
    real = stats_cutover.lock_fold_state

    def lock():
        calls.append(db.session().in_transaction())
        return real()

    monkeypatch.setattr("lumen.commands.lock_fold_state", lock)
    _reassign(app, src, dst)
    assert calls == [False]


def test_a_fold_waits_for_reassign(app, post_cutover, ids):
    """On SQLite the lock is the database write lock: a fold during the command gives up."""
    from sqlalchemy.exc import OperationalError

    from lumen.services import stats_fold
    entity_id, src, dst = ids
    _bill(app, entity_id, src)
    with app.app_context():
        stats_cutover.lock_fold_state()
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(stats_fold, "_SQLITE_BUSY_TIMEOUT_MS", 50)
                with pytest.raises(OperationalError, match="database is locked"):
                    fold(app)
        finally:
            db.session.rollback()
    assert fold(app) is not None
    assert _model_stats(app)[(src, "api")][0] == 1


def test_lock_reads_the_state(app, post_cutover):
    with app.app_context():
        state = stats_cutover.lock_fold_state()
        db.session.rollback()
    assert state.cutover_at == state.folded_until
    assert state.cutover_at < utcnow() - timedelta(minutes=30)
