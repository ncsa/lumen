"""The one-statement pool resolution and the narrowed consent read."""
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from lumen.extensions import db
from lumen.models.entity import Entity
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.group import Group
from lumen.models.group_limit import GroupLimit
from lumen.models.group_member import GroupMember
from lumen.models.model_config import ModelConfig
from lumen.services.llm import (
    PoolBalance,
    PoolLimit,
    _load_pool_state,
    best_group_pool_limit,
    bulk_model_access_info,
    get_pool_limit,
)
from lumen.timeutils import utcnow

# ---------------------------------------------------------------------------
# bulk_model_access_info reads consent rows only for models that need them
# ---------------------------------------------------------------------------


def _consent_queries(app, fn):
    statements = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        if "entity_model_consents" in statement:
            statements.append(tuple(parameters))

    engine = db.engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    return statements


def _models(*flags):
    ids = []
    for i, f in enumerate(flags):
        mc = ModelConfig(model_name=f"m{i}", input_cost_per_million=1, output_cost_per_million=1, **f)
        db.session.add(mc)
        db.session.flush()
        ids.append(mc.id)
    db.session.commit()
    return ids


def test_bulk_access_info_skips_consent_query_without_ack_models(app, test_user):
    with app.app_context():
        ids = _models({}, {})
        queries = _consent_queries(app, lambda: bulk_model_access_info(test_user["id"], ids))
    assert queries == []


def test_bulk_access_info_reads_consent_only_for_ack_models(app, test_user):
    with app.app_context():
        plain, ack, plain2 = _models({}, {"needs_ack": True}, {})
        result = {}
        queries = _consent_queries(
            app, lambda: result.update(statuses=bulk_model_access_info(test_user["id"], [plain, ack, plain2])[0]),
        )
    assert queries == [(test_user["id"], ack)]
    assert result["statuses"] == {plain: "allowed", ack: "needs_ack", plain2: "allowed"}


# ---------------------------------------------------------------------------
# _load_pool_state agrees with the three-query resolution it replaced
# ---------------------------------------------------------------------------


def _old_get_pool_limit(app, entity_id):
    """get_pool_limit as it was before _load_pool_state (one query per table)."""
    user_limit = db.session.execute(select(EntityLimit).filter_by(entity_id=entity_id)).scalar_one_or_none()
    if user_limit is not None:
        if float(user_limit.max_coins) == 0:
            return None
        return PoolLimit(float(user_limit.max_coins), float(user_limit.refresh_coins), float(user_limit.starting_coins))
    group_ids = [
        m.group_id for m in db.session.execute(
            select(GroupMember)
            .join(Group, Group.id == GroupMember.group_id)
            .where(GroupMember.entity_id == entity_id, Group.active == True)  # noqa: E712
        ).scalars().all()
    ]
    if group_ids:
        best = best_group_pool_limit(
            db.session.execute(select(GroupLimit).where(GroupLimit.group_id.in_(group_ids))).scalars().all()
        )
        if best is not None:
            return best
    td = app.config.get("TOKEN_DEFAULTS")
    if td and float(td["max"]) != 0:
        return PoolLimit(float(td["max"]), float(td["refresh"]), float(td["starting"]))
    return None


_OWN = {"none": None, "zero": 0, "positive": 40, "unlimited": -2}
# (max_coins, active) per group
_GROUPS = {
    "none": [],
    "zero": [(0, True)],
    "positive": [(30, True)],
    "unlimited": [(-2, True)],
    "mixed": [(0, True), (30, True), (70, True)],
    "inactive": [(-2, False), (20, True)],
}
_DEFAULTS = {"zero": 0, "positive": 25, "unlimited": -2}
_BALANCE = {"missing": None, "zero": 0, "positive": 12.5}


@pytest.mark.parametrize("balance", list(_BALANCE))
@pytest.mark.parametrize("defaults", list(_DEFAULTS))
@pytest.mark.parametrize("groups", list(_GROUPS))
@pytest.mark.parametrize("own", list(_OWN))
def test_pool_state_agrees_with_old_resolution(app, monkeypatch, own, groups, defaults, balance):
    monkeypatch.setitem(app.config, "TOKEN_DEFAULTS",
                        {"max": _DEFAULTS[defaults], "refresh": 5, "starting": 15})
    last_refill = utcnow() - timedelta(minutes=10)
    with app.app_context():
        entity = Entity(entity_type="user", email="pool@example.com", name="Pool", active=True)
        db.session.add(entity)
        db.session.flush()
        if _OWN[own] is not None:
            db.session.add(EntityLimit(entity_id=entity.id, max_coins=_OWN[own], refresh_coins=4, starting_coins=8))
        for i, (max_coins, active) in enumerate(_GROUPS[groups]):
            group = Group(name=f"g{i}", active=active)
            db.session.add(group)
            db.session.flush()
            db.session.add(GroupMember(entity_id=entity.id, group_id=group.id))
            db.session.add(GroupLimit(group_id=group.id, max_coins=max_coins, refresh_coins=i + 1, starting_coins=i + 2))
        if _BALANCE[balance] is not None:
            db.session.add(EntityBalance(entity_id=entity.id, coins_left=Decimal(str(_BALANCE[balance])),
                                         last_refill_at=last_refill))
        db.session.commit()

        expected = _old_get_pool_limit(app, entity.id)
        pool, bal = _load_pool_state(entity.id)
        assert pool == expected
        assert get_pool_limit(entity.id) == expected
        if _BALANCE[balance] is None:
            assert bal is None
        else:
            assert bal == PoolBalance(_BALANCE[balance], last_refill)


def test_pool_state_for_unknown_entity_falls_back_to_defaults(app, monkeypatch):
    monkeypatch.setitem(app.config, "TOKEN_DEFAULTS", {"max": 25, "refresh": 5, "starting": 15})
    with app.app_context():
        assert _load_pool_state(999999) == (PoolLimit(25.0, 5.0, 15.0), None)
