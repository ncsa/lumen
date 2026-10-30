"""``flask check-stats``: the folded totals against request_logs since the cutover.

The fold records its own part of every total in the ``folded_*`` columns, so
the check compares those with the ``request_logs`` sums of
``[cutover_at, folded_until)``, whatever history the totals carried before.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from lumen.commands import check_stats_cmd
from lumen.extensions import db
from lumen.models.entity import Entity
from lumen.models.entity_stat import EntityStat
from lumen.models.model_config import ModelConfig
from lumen.models.model_stat import ModelStat
from lumen.models.request_log import RequestLog
from lumen.models.stats_fold_state import StatsFoldState
from lumen.services.stats_fold import fold_lag, fold_once

CUTOVER = datetime(2026, 10, 1, 12, 0, 0)
FOLDED = CUTOVER + timedelta(minutes=10)


@pytest.fixture
def ids(app):
    with app.app_context():
        entity = Entity(entity_type="user", email="check@example.com", name="Check",
                        initials="CH", active=True)
        model = ModelConfig(model_name="check-model", input_cost_per_million=1.0, output_cost_per_million=1.0)
        db.session.add_all([entity, model])
        db.session.commit()
        return entity.id, model.id


def _log(app, t, entity_id, model_id, source="api", input_tokens=10, output_tokens=5, audio_seconds=0, cost="0.5"):
    with app.app_context():
        db.session.add(RequestLog(
            time=t.replace(tzinfo=timezone.utc), entity_id=entity_id, model_config_id=model_id, source=source,
            input_tokens=input_tokens, output_tokens=output_tokens, audio_seconds=audio_seconds,
            cost=Decimal(cost), duration=0.0,
        ))
        db.session.commit()


def _cut_over_with_history(app, entity_id, model_id):
    """A cutover at CUTOVER, totals carrying history request_logs does not have."""
    with app.app_context():
        db.session.add(StatsFoldState(id=1, cutover_at=CUTOVER, folded_until=CUTOVER))
        db.session.add(ModelStat(entity_id=entity_id, model_config_id=model_id, source="api", requests=100,
                                 input_tokens=1000, output_tokens=500, audio_seconds=0, cost=Decimal("50"),
                                 last_used_at=CUTOVER - timedelta(days=30)))
        db.session.add(EntityStat(entity_id=entity_id, requests=100, input_tokens=1000, output_tokens=500,
                                  audio_seconds=0, cost=Decimal("50"), conversations=0,
                                  last_used_at=CUTOVER - timedelta(days=30)))
        db.session.commit()


def _fold_to(app, hi):
    return fold_once(app, now=hi + fold_lag(app))


def _check(app, *args):
    return app.test_cli_runner().invoke(check_stats_cmd, list(args))


def _totals(app, entity_id):
    with app.app_context():
        model = db.session.execute(select(ModelStat).filter_by(entity_id=entity_id)).scalar_one()
        entity = db.session.get(EntityStat, entity_id)
        return [
            (s.requests, s.input_tokens, s.output_tokens, s.audio_seconds, s.cost, s.last_used_at,
             s.folded_requests, s.folded_input_tokens, s.folded_output_tokens, s.folded_audio_seconds, s.folded_cost)
            for s in (model, entity)
        ]


def test_does_nothing_before_the_cutover(app, ids):
    entity_id, model_id = ids
    _log(app, CUTOVER, entity_id, model_id)
    result = _check(app, "--fix")
    assert result.exit_code == 0, result.output
    assert "nothing to check" in result.output
    with app.app_context():
        assert db.session.execute(select(ModelStat)).first() is None
        assert db.session.execute(select(EntityStat)).first() is None


def test_fold_records_its_part_and_the_check_passes(app, ids):
    entity_id, model_id = ids
    _cut_over_with_history(app, entity_id, model_id)
    _log(app, CUTOVER, entity_id, model_id, audio_seconds=7)
    _log(app, CUTOVER + timedelta(minutes=5), entity_id, model_id, cost="0.25")
    _fold_to(app, FOLDED)

    model, entity = _totals(app, entity_id)
    assert model == entity
    assert model[:6] == (102, 1020, 510, 7, Decimal("50.75"), CUTOVER + timedelta(minutes=5))
    assert model[6:] == (2, 20, 10, 7, Decimal("0.75"))

    result = _check(app)
    assert result.exit_code == 0, result.output
    assert "match request_logs" in result.output


def test_detects_a_late_commit_and_fix_repairs_it(app, ids):
    """A row stamped inside an already-folded window, the drift a late commit leaves."""
    entity_id, model_id = ids
    _cut_over_with_history(app, entity_id, model_id)
    _log(app, CUTOVER, entity_id, model_id)
    _fold_to(app, FOLDED)
    folded = _totals(app, entity_id)
    late = FOLDED - timedelta(seconds=1)
    _log(app, late, entity_id, model_id, input_tokens=3, output_tokens=2, audio_seconds=4, cost="0.125")

    result = _check(app)
    assert result.exit_code == 1, result.output
    assert f"model_stats entity_id={entity_id}, model_config_id={model_id}, source=api:" in result.output
    assert f"entity_stats entity_id={entity_id}:" in result.output
    assert "requests +1, input_tokens +3, output_tokens +2, audio_seconds +4, cost +0.125" in result.output
    assert f"last_used_at -> {late}" in result.output
    assert "2 row(s) differ" in result.output
    assert _totals(app, entity_id) == folded, "the check without --fix changed the totals"

    result = _check(app, "--fix")
    assert result.exit_code == 0, result.output
    assert "Fixed 2 row(s)." in result.output
    model, entity = _totals(app, entity_id)
    assert model == entity
    assert model[:6] == (102, 1013, 507, 4, Decimal("50.625"), late)
    assert model[6:] == (2, 13, 7, 4, Decimal("0.625"))

    assert _check(app).exit_code == 0
    # The next pass starts at folded_until, so the repaired row is not counted again.
    _fold_to(app, FOLDED + timedelta(minutes=10))
    assert _totals(app, entity_id) == [model, entity]


def test_fix_creates_a_missing_row_and_removes_an_excess(app, ids):
    """A row the fold never saw (no stats row yet), and a total the fold overcounted."""
    entity_id, model_id = ids
    with app.app_context():
        db.session.add(StatsFoldState(id=1, cutover_at=CUTOVER, folded_until=CUTOVER))
        db.session.commit()
    _fold_to(app, FOLDED)
    _log(app, CUTOVER, entity_id, model_id, source="chat")
    with app.app_context():
        db.session.add(EntityStat(entity_id=entity_id, requests=3, input_tokens=0, output_tokens=0,
                                  audio_seconds=0, cost=Decimal("0"), conversations=2, folded_requests=3))
        db.session.commit()

    result = _check(app, "--fix")
    assert result.exit_code == 0, result.output
    with app.app_context():
        model = db.session.execute(select(ModelStat)).scalar_one()
        assert (model.entity_id, model.model_config_id, model.source) == (entity_id, model_id, "chat")
        assert (model.requests, model.folded_requests, model.last_used_at) == (1, 1, CUTOVER)
        entity = db.session.get(EntityStat, entity_id)
        assert (entity.requests, entity.folded_requests, entity.input_tokens) == (1, 1, 10)
        assert entity.conversations == 2
    assert _check(app).exit_code == 0


def test_only_the_folded_window_is_checked(app, ids):
    """Rows at or after folded_until are the next fold's, not drift."""
    entity_id, model_id = ids
    _cut_over_with_history(app, entity_id, model_id)
    _fold_to(app, FOLDED)
    _log(app, FOLDED, entity_id, model_id)
    _log(app, CUTOVER - timedelta(seconds=1), entity_id, model_id)  # before the cutover: billing bumped it
    result = _check(app)
    assert result.exit_code == 0, result.output


def test_ignores_float_noise_in_cost(app, ids):
    """SQLite sums Numeric as floats; one unit of the sixth decimal reads as equal."""
    entity_id, model_id = ids
    _cut_over_with_history(app, entity_id, model_id)
    _log(app, CUTOVER, entity_id, model_id, cost="0.1")
    _fold_to(app, FOLDED)
    with app.app_context():
        for model in (ModelStat, EntityStat):
            stat = db.session.execute(select(model).filter_by(entity_id=entity_id)).scalar_one()
            stat.folded_cost += Decimal("0.000001")
        db.session.commit()
    assert _check(app).exit_code == 0
