"""Additional LLM service tests: groups, endpoints, coin functions, stats."""
from datetime import datetime
from sqlalchemy import func, select

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_group(db, name, active=True, model_access_default=None):
    from lumen.models.group import Group
    g = Group(name=name, active=active, model_access_default=model_access_default)
    db.session.add(g)
    db.session.flush()
    return g


def _add_member(db, group_id, entity_id):
    from lumen.models.group_member import GroupMember
    db.session.add(GroupMember(group_id=group_id, entity_id=entity_id))
    db.session.flush()


def _add_group_model_access(db, group_id, model_config_id, access_type):
    from lumen.models.group_model_access import GroupModelAccess
    db.session.add(GroupModelAccess(group_id=group_id, model_config_id=model_config_id, access_type=access_type))
    db.session.flush()


# ---------------------------------------------------------------------------
# Group model access tests
# ---------------------------------------------------------------------------

def test_group_blacklist_blocks(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import get_model_access_status
        g = _make_group(db, "blk-group")
        _add_member(db, g.id, entity_id)
        _add_group_model_access(db, g.id, model_id, "blacklist")
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"


def test_group_whitelist_allows(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import get_model_access_status
        g = _make_group(db, "wl-group")
        _add_member(db, g.id, entity_id)
        _add_group_model_access(db, g.id, model_id, "whitelist")
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "allowed"


def test_group_graylist(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import get_model_access_status
        g = _make_group(db, "gl-group")
        _add_member(db, g.id, entity_id)
        _add_group_model_access(db, g.id, model_id, "graylist")
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "graylist"


def test_group_blacklist_beats_group_whitelist(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import get_model_access_status
        g1 = _make_group(db, "blk2")
        g2 = _make_group(db, "wl2")
        _add_member(db, g1.id, entity_id)
        _add_member(db, g2.id, entity_id)
        _add_group_model_access(db, g1.id, model_id, "blacklist")
        _add_group_model_access(db, g2.id, model_id, "whitelist")
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"


def test_group_default_whitelist_allows(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import get_model_access_status
        g = _make_group(db, "def-wl", model_access_default="whitelist")
        _add_member(db, g.id, entity_id)
        db.session.commit()
        # No per-model rule; group default is whitelist
        assert get_model_access_status(entity_id, model_id) == "allowed"


def test_group_default_blacklist_blocks(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import get_model_access_status
        g = _make_group(db, "def-blk", model_access_default="blacklist")
        _add_member(db, g.id, entity_id)
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"


def test_group_default_graylist(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import get_model_access_status
        g = _make_group(db, "def-gl", model_access_default="graylist")
        _add_member(db, g.id, entity_id)
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "graylist"


def test_inactive_group_ignored(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import get_model_access_status
        g = _make_group(db, "inactive-g", active=False, model_access_default="blacklist")
        _add_member(db, g.id, entity_id)
        db.session.commit()
        # Inactive group is ignored; default whitelist applies
        assert get_model_access_status(entity_id, model_id) == "allowed"


# ---------------------------------------------------------------------------
# Endpoint selection
# ---------------------------------------------------------------------------

def test_get_next_endpoint_no_endpoints(app, test_model):
    with app.app_context():
        from lumen.services.llm import get_next_endpoint
        assert get_next_endpoint(test_model["id"]) is None


def test_get_next_endpoint_returns_healthy(app, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.services.llm import get_next_endpoint
        ep = ModelEndpoint(model_config_id=test_model["id"], url="http://x/v1", api_key="k", healthy=True)
        db.session.add(ep)
        db.session.commit()
        result = get_next_endpoint(test_model["id"])
        assert result is not None
        assert result.healthy is True


def test_get_next_endpoint_skips_unhealthy(app, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.services.llm import get_next_endpoint
        db.session.add(ModelEndpoint(model_config_id=test_model["id"], url="http://x/v1", api_key="k", healthy=False))
        db.session.commit()
        assert get_next_endpoint(test_model["id"]) is None


def test_get_next_endpoint_round_robin(app, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.services import llm as llm_mod
        from lumen.services.llm import get_next_endpoint
        ep1 = ModelEndpoint(model_config_id=test_model["id"], url="http://ep1/v1", api_key="k1", healthy=True)
        ep2 = ModelEndpoint(model_config_id=test_model["id"], url="http://ep2/v1", api_key="k2", healthy=True)
        db.session.add_all([ep1, ep2])
        db.session.commit()
        # Reset counter so test is deterministic
        llm_mod._rr_counters[test_model["id"]] = 0
        r1 = get_next_endpoint(test_model["id"])
        r2 = get_next_endpoint(test_model["id"])
        assert r1.id != r2.id  # alternates between the two


# ---------------------------------------------------------------------------
# Coin balance tests
# ---------------------------------------------------------------------------

def test_get_coin_balance_no_limit(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.services.llm import get_coin_balance
        # No limit → access is None → returns None
        assert get_coin_balance(entity_id, model_id) is None


def test_get_coin_balance_unlimited(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import get_coin_balance
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
        # Unlimited → returns None (no budget to track)
        assert get_coin_balance(entity_id, model_id) is None


def test_get_coin_balance_creates_balance(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import get_coin_balance
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=0, starting_coins=50))
        db.session.commit()
        balance = get_coin_balance(entity_id, model_id)
        assert balance == 50.0
        # Balance row should have been created
        row = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one_or_none()
        assert row is not None


def test_get_coin_balance_existing_balance(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import get_coin_balance
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=0, starting_coins=100))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=42))
        db.session.commit()
        assert get_coin_balance(entity_id, model_id) == 42.0


# ---------------------------------------------------------------------------
# check_coin_budget
# ---------------------------------------------------------------------------

def test_check_coin_budget_no_access(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        from lumen.services.llm import check_coin_budget
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="blacklist"))
        db.session.commit()
        ok, code, msg = check_coin_budget(entity_id, model_id)
        assert not ok
        assert code == 403


def test_check_coin_budget_unlimited(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import check_coin_budget
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
        ok, code, _ = check_coin_budget(entity_id, model_id)
        assert ok
        assert code is None


def test_check_coin_budget_exhausted(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import check_coin_budget
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=0, starting_coins=100))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=0))
        db.session.commit()
        ok, code, _ = check_coin_budget(entity_id, model_id)
        assert not ok
        assert code == 429


def test_check_coin_budget_ok(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import check_coin_budget
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=0, starting_coins=100))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=50))
        db.session.commit()
        ok, code, _ = check_coin_budget(entity_id, model_id)
        assert ok
        assert code is None


# ---------------------------------------------------------------------------
# update_stats
# ---------------------------------------------------------------------------

def test_update_stats_creates_stat_and_log(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_stat import ModelStat
        from lumen.models.request_log import RequestLog
        from lumen.services.llm import update_stats
        update_stats(entity_id, model_id, "chat", 100, 200, 0.0003)
        db.session.commit()
        stat = db.session.execute(select(ModelStat).filter_by(entity_id=entity_id, model_config_id=model_id, source="chat")).scalar_one_or_none()
        assert stat is not None
        assert stat.requests == 1
        assert stat.input_tokens == 100
        assert stat.output_tokens == 200
        log_count = db.session.scalar(select(func.count()).select_from(RequestLog).filter_by(entity_id=entity_id, model_config_id=model_id))
        assert log_count == 1


def test_update_stats_accumulates(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_stat import ModelStat
        from lumen.services.llm import update_stats
        import time
        update_stats(entity_id, model_id, "api", 50, 100, 0.0001)
        db.session.commit()
        time.sleep(0.001)  # ensure distinct timestamps for primary key
        update_stats(entity_id, model_id, "api", 50, 100, 0.0001)
        db.session.commit()
        stat = db.session.execute(select(ModelStat).filter_by(entity_id=entity_id, model_config_id=model_id, source="api")).scalar_one_or_none()
        assert stat.requests == 2
        assert stat.input_tokens == 100
        assert stat.output_tokens == 200


# ---------------------------------------------------------------------------
# get_pool_limit with groups
# ---------------------------------------------------------------------------

def test_get_pool_limit_group_limit(app, test_user, test_model):
    entity_id = test_user["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_limit import GroupLimit
        from lumen.services.llm import get_pool_limit
        g = _make_group(db, "limit-group")
        _add_member(db, g.id, entity_id)
        db.session.add(GroupLimit(group_id=g.id, max_coins=500, refresh_coins=50, starting_coins=500))
        db.session.commit()
        result = get_pool_limit(entity_id)
        assert result is not None
        assert result[0] == 500.0


def test_get_pool_limit_user_wins_over_group(app, test_user, test_model):
    entity_id = test_user["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.group_limit import GroupLimit
        from lumen.services.llm import get_pool_limit
        g = _make_group(db, "grp-limit2")
        _add_member(db, g.id, entity_id)
        db.session.add(GroupLimit(group_id=g.id, max_coins=100, refresh_coins=0, starting_coins=100))
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=200, refresh_coins=0, starting_coins=200))
        db.session.commit()
        result = get_pool_limit(entity_id)
        # Higher max_coins wins
        assert result[0] == 200.0
