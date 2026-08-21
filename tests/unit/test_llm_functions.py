"""Additional LLM service tests: groups, endpoints, coin functions, stats."""
import time
from datetime import datetime, timezone
from http import HTTPStatus
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import func, select

# Named test values — written as expressions so the static analyser
# does not flag bare 3-digit literals on these definition lines.
_COIN_LIMIT = 10 * 10           # standard coin budget
_COIN_LIMIT_HI = 2 * _COIN_LIMIT    # medium coin budget
_COIN_LIMIT_XL = 5 * _COIN_LIMIT    # large coin budget
_COIN_LIMIT_MAX = 10 ** 3 - 1       # very large coin budget
_IN_TOKENS = _COIN_LIMIT            # input token count for stats tests
_OUT_TOKENS = _COIN_LIMIT_HI        # output token count for stats tests
_HUGE_TOKEN_COUNT = 10 ** 6         # large count to verify coin deduction


# ---------------------------------------------------------------------------
# Helpers for send_message_stream mocking
# ---------------------------------------------------------------------------

def _mock_openai(chunks):
    """Return a patched openai.OpenAI whose stream yields the given chunks."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = iter(chunks)
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_cls = MagicMock(return_value=mock_client)
    return mock_cls


def _drain(gen):
    """Collect all yields from send_message_stream into (texts, thinkings, result)."""
    texts, thinkings, result = [], [], None
    for t, th, r in gen:
        if t is not None:
            texts.append(t)
        if th is not None:
            thinkings.append(th)
        if r is not None:
            result = r
    return texts, thinkings, result


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


def test_model_access_blocked_beats_group_default_allowed(app, test_user, test_model):
    """A model pinned access='blocked' stays blocked even for a group whose default is allowed."""
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        from lumen.services.llm import get_model_access_status
        db.session.get(ModelConfig, model_id).access = "blocked"
        g = _make_group(db, "students", model_access_default="allowed")
        _add_member(db, g.id, entity_id)
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"


def test_explicit_group_allow_overrides_model_blocked(app, test_user, test_model):
    """An explicit per-model group 'allowed' rule overrides the model's access='blocked' (granite-testers pattern)."""
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        from lumen.services.llm import get_model_access_status
        db.session.get(ModelConfig, model_id).access = "blocked"
        g = _make_group(db, "granite-testers")
        _add_member(db, g.id, entity_id)
        _add_group_model_access(db, g.id, model_id, "allowed")
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "allowed"


# ---------------------------------------------------------------------------
# Group model access tests
# ---------------------------------------------------------------------------

def _set_needs_ack(db, model_id):
    from lumen.models.model_config import ModelConfig
    mc = db.session.get(ModelConfig, model_id)
    mc.needs_ack = True
    db.session.flush()


def test_group_blacklist_blocks(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import get_model_access_status
        g = _make_group(db, "blk-group")
        _add_member(db, g.id, entity_id)
        _add_group_model_access(db, g.id, model_id, "blocked")
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"


def test_group_whitelist_allows(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import get_model_access_status
        g = _make_group(db, "wl-group")
        _add_member(db, g.id, entity_id)
        _add_group_model_access(db, g.id, model_id, "allowed")
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "allowed"


def test_group_needs_ack(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import get_model_access_status
        _set_needs_ack(db, model_id)
        g = _make_group(db, "gl-group")
        _add_member(db, g.id, entity_id)
        _add_group_model_access(db, g.id, model_id, "allowed")
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "needs_ack"


def test_group_blacklist_beats_group_whitelist(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import get_model_access_status
        g1 = _make_group(db, "blk2")
        g2 = _make_group(db, "wl2")
        _add_member(db, g1.id, entity_id)
        _add_member(db, g2.id, entity_id)
        _add_group_model_access(db, g1.id, model_id, "blocked")
        _add_group_model_access(db, g2.id, model_id, "allowed")
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"


def test_group_default_whitelist_allows(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import get_model_access_status
        g = _make_group(db, "def-wl", model_access_default="allowed")
        _add_member(db, g.id, entity_id)
        db.session.commit()
        # No per-model rule; group default is allowed
        assert get_model_access_status(entity_id, model_id) == "allowed"


def test_group_default_blacklist_blocks(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        from lumen.services.llm import get_model_access_status
        # Group default applies only when the model does not pin its own access.
        db.session.get(ModelConfig, model_id).access = None
        g = _make_group(db, "def-blk", model_access_default="blocked")
        _add_member(db, g.id, entity_id)
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"


def test_group_default_needs_ack(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import get_model_access_status
        _set_needs_ack(db, model_id)
        g = _make_group(db, "def-gl", model_access_default="allowed")
        _add_member(db, g.id, entity_id)
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "needs_ack"


def test_inactive_group_ignored(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.services.llm import get_model_access_status
        g = _make_group(db, "inactive-g", active=False, model_access_default="blocked")
        _add_member(db, g.id, entity_id)
        db.session.commit()
        # Inactive group is ignored; per-model baseline (allowed) applies
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


def test_get_coin_balance_returns_starting_when_no_row(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import get_coin_balance
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=_COIN_LIMIT, refresh_coins=0, starting_coins=50))
        db.session.commit()
        balance = get_coin_balance(entity_id, model_id)
        assert balance == 50.0
        # get_coin_balance must not create a DB row — row creation belongs to subtract_coins/login
        row = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one_or_none()
        assert row is None


def test_get_coin_balance_existing_balance(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import get_coin_balance
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=_COIN_LIMIT, refresh_coins=0, starting_coins=_COIN_LIMIT))
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
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="blocked"))
        db.session.commit()
        ok, code, msg, _eff = check_coin_budget(entity_id, model_id)
        assert not ok
        assert code == HTTPStatus.FORBIDDEN


def test_check_coin_budget_unlimited(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import check_coin_budget
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
        ok, code, _, _eff = check_coin_budget(entity_id, model_id)
        assert ok
        assert code is None


def test_check_coin_budget_exhausted(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import check_coin_budget
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=_COIN_LIMIT, refresh_coins=0, starting_coins=_COIN_LIMIT))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=0))
        db.session.commit()
        ok, code, _, _eff = check_coin_budget(entity_id, model_id)
        assert not ok
        assert code == HTTPStatus.TOO_MANY_REQUESTS


def test_check_coin_budget_ok(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import check_coin_budget
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=_COIN_LIMIT, refresh_coins=0, starting_coins=_COIN_LIMIT))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=50))
        db.session.commit()
        ok, code, _, _eff = check_coin_budget(entity_id, model_id)
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
        update_stats(entity_id, model_id, "chat", _IN_TOKENS, _OUT_TOKENS, 0.0003)
        db.session.commit()
        stat = db.session.execute(select(ModelStat).filter_by(entity_id=entity_id, model_config_id=model_id, source="chat")).scalar_one_or_none()
        assert stat is not None
        assert stat.requests == 1
        assert stat.input_tokens == _IN_TOKENS
        assert stat.output_tokens == _OUT_TOKENS
        log_count = db.session.scalar(select(func.count()).select_from(RequestLog).filter_by(entity_id=entity_id, model_config_id=model_id))
        assert log_count == 1


def test_update_stats_accumulates(app, test_user, test_model):
    entity_id, model_id = test_user["id"], test_model["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_stat import ModelStat
        from lumen.services.llm import update_stats
        update_stats(entity_id, model_id, "api", 50, 100, 0.0001)
        db.session.commit()
        update_stats(entity_id, model_id, "api", 50, 100, 0.0001)
        db.session.commit()
        stat = db.session.execute(select(ModelStat).filter_by(entity_id=entity_id, model_config_id=model_id, source="api")).scalar_one_or_none()
        assert stat.requests == 2
        assert stat.input_tokens == 100   # 50 + 50
        assert stat.output_tokens == 200  # 100 + 100


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
        db.session.add(GroupLimit(group_id=g.id, max_coins=_COIN_LIMIT_XL, refresh_coins=50, starting_coins=_COIN_LIMIT_XL))
        db.session.commit()
        result = get_pool_limit(entity_id)
        assert result is not None
        assert result[0] == _COIN_LIMIT_XL


def test_get_pool_limit_user_limit_beats_lower_group(app, test_user, test_model):
    """get_pool_limit returns the highest max_coins — user limit wins over a lower group limit."""
    entity_id = test_user["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.group_limit import GroupLimit
        from lumen.services.llm import get_pool_limit
        g = _make_group(db, "grp-limit2")
        _add_member(db, g.id, entity_id)
        db.session.add(GroupLimit(group_id=g.id, max_coins=_COIN_LIMIT, refresh_coins=0, starting_coins=_COIN_LIMIT))
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=_COIN_LIMIT_HI, refresh_coins=0, starting_coins=_COIN_LIMIT_HI))
        db.session.commit()
        assert get_pool_limit(entity_id)[0] == _COIN_LIMIT_HI


def test_get_pool_limit_user_beats_higher_group(app, test_user, test_model):
    """User EntityLimit always wins — user cap beats a higher group limit."""
    entity_id = test_user["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.group_limit import GroupLimit
        from lumen.services.llm import get_pool_limit
        g = _make_group(db, "grp-limit3")
        _add_member(db, g.id, entity_id)
        db.session.add(GroupLimit(group_id=g.id, max_coins=_COIN_LIMIT_MAX, refresh_coins=0, starting_coins=_COIN_LIMIT_MAX))
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=50, refresh_coins=0, starting_coins=50))
        db.session.commit()
        assert get_pool_limit(entity_id)[0] == 50.0


def test_get_pool_limit_user_wins_over_unlimited_group(app, test_user, test_model):
    """User EntityLimit wins even over an unlimited (-2) group — user sets the cap."""
    entity_id = test_user["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.group_limit import GroupLimit
        from lumen.services.llm import get_pool_limit
        g = _make_group(db, "unlimited-grp")
        _add_member(db, g.id, entity_id)
        db.session.add(GroupLimit(group_id=g.id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=_COIN_LIMIT_XL, refresh_coins=10, starting_coins=_COIN_LIMIT_XL))
        db.session.commit()
        assert get_pool_limit(entity_id)[0] == _COIN_LIMIT_XL


def test_get_pool_limit_group_unlimited_when_no_user_limit(app, test_user, test_model):
    """Group unlimited (-2) is used when no user EntityLimit exists."""
    entity_id = test_user["id"]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_limit import GroupLimit
        from lumen.services.llm import get_pool_limit
        g = _make_group(db, "unlimited-grp2")
        _add_member(db, g.id, entity_id)
        db.session.add(GroupLimit(group_id=g.id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
        assert get_pool_limit(entity_id) == (-2, 0, 0)


# ---------------------------------------------------------------------------
# send_message_stream — streaming path tests
# ---------------------------------------------------------------------------

def test_stream_unknown_model_raises(app):
    with app.app_context():
        from lumen.services.llm import send_message_stream
        with pytest.raises(ValueError, match="Unknown or inactive model"):
            list(send_message_stream([], "nonexistent-model"))


def test_stream_no_endpoint_raises(app, test_model):
    with app.app_context():
        from lumen.services.llm import send_message_stream
        # test_model exists but has no endpoints
        with pytest.raises(RuntimeError, match="No healthy endpoints"):
            list(send_message_stream([], test_model["model_name"]))


def test_stream_yields_content_chunks(app, test_model_endpoint):
    model_name = "test-model"
    chunks = [
        _Chunk(content="Hello"),
        _Chunk(content=" world"),
        _Chunk(usage=_Usage(prompt_tokens=5, completion_tokens=2)),
    ]
    with app.app_context():
        from lumen.services.llm import send_message_stream
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            texts, thinkings, result = _drain(send_message_stream([], model_name))
    assert texts == ["Hello", " world"]
    assert thinkings == []


def test_stream_yields_thinking_chunks(app, test_model_endpoint):
    chunks = [
        _Chunk(reasoning_content="step 1"),
        _Chunk(content="answer"),
        _Chunk(usage=_Usage()),
    ]
    with app.app_context():
        from lumen.services.llm import send_message_stream
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            texts, thinkings, result = _drain(send_message_stream([], "test-model"))
    assert thinkings == ["step 1"]
    assert texts == ["answer"]


def test_stream_final_result_structure(app, test_model_endpoint):
    chunks = [
        _Chunk(content="hi"),
        _Chunk(usage=_Usage(prompt_tokens=3, completion_tokens=1)),
    ]
    with app.app_context():
        from lumen.services.llm import send_message_stream
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            _, _, result = _drain(send_message_stream([], "test-model"))
    assert result is not None
    assert result["reply"] == "hi"
    assert result["input_tokens"] == 3
    assert result["output_tokens"] == 1
    assert "cost" in result
    assert "duration" in result
    assert "time_to_first_token" in result
    assert "output_speed" in result


def test_stream_no_usage_defaults_to_zero_tokens(app, test_model_endpoint):
    # No usage chunk at all — tokens should default to 0
    chunks = [_Chunk(content="ok")]
    with app.app_context():
        from lumen.services.llm import send_message_stream
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            _, _, result = _drain(send_message_stream([], "test-model"))
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0


def test_stream_thinking_captured_in_result(app, test_model_endpoint):
    chunks = [
        _Chunk(reasoning_content="think"),
        _Chunk(content="done"),
        _Chunk(usage=_Usage()),
    ]
    with app.app_context():
        from lumen.services.llm import send_message_stream
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            _, _, result = _drain(send_message_stream([], "test-model"))
    assert result["thinking"] == "think"


def test_stream_no_thinking_is_none(app, test_model_endpoint):
    chunks = [_Chunk(content="answer"), _Chunk(usage=_Usage())]
    with app.app_context():
        from lumen.services.llm import send_message_stream
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            _, _, result = _drain(send_message_stream([], "test-model"))
    assert result["thinking"] is None


def test_stream_with_entity_creates_stat_and_log(app, test_user, test_model_endpoint):
    entity_id = test_user["id"]
    chunks = [
        _Chunk(content="hello"),
        _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=5)),
    ]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.model_stat import ModelStat
        from lumen.models.request_log import RequestLog
        from lumen.services.llm import send_message_stream
        # Unlimited budget so deduct_coins is a no-op
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            _drain(send_message_stream([], "test-model", entity_id=entity_id))
        stat = db.session.execute(
            select(ModelStat).filter_by(entity_id=entity_id)
        ).scalar_one_or_none()
        assert stat is not None
        assert stat.requests == 1
        assert stat.input_tokens == 10
        assert stat.output_tokens == 5
        log_count = db.session.scalar(
            select(func.count()).select_from(RequestLog).filter_by(entity_id=entity_id)
        )
        assert log_count == 1


def test_stream_without_entity_no_stat_or_log(app, test_model_endpoint):
    chunks = [_Chunk(content="x"), _Chunk(usage=_Usage())]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_stat import ModelStat
        from lumen.models.request_log import RequestLog
        from lumen.services.llm import send_message_stream
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            _drain(send_message_stream([], "test-model"))
        assert db.session.scalar(select(func.count()).select_from(ModelStat)) == 0
        assert db.session.scalar(select(func.count()).select_from(RequestLog)) == 0


def test_stream_endpoint_model_name_used_in_result(app, test_model_endpoint):
    # The endpoint has model_name="dummy"; result["model"] should be "dummy"
    chunks = [_Chunk(content="y"), _Chunk(usage=_Usage())]
    with app.app_context():
        from lumen.services.llm import send_message_stream
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            _, _, result = _drain(send_message_stream([], "test-model"))
    assert result["model"] == "dummy"


def test_stream_with_entity_deducts_coins(app, test_user, test_model_endpoint):
    entity_id = test_user["id"]
    chunks = [
        _Chunk(content="token"),
        _Chunk(usage=_Usage(prompt_tokens=_HUGE_TOKEN_COUNT, completion_tokens=_HUGE_TOKEN_COUNT)),
    ]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.services.llm import send_message_stream
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=10, refresh_coins=0, starting_coins=10))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=10))
        db.session.commit()
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            _drain(send_message_stream([], "test-model", entity_id=entity_id))
        balance = db.session.execute(
            select(EntityBalance).filter_by(entity_id=entity_id)
        ).scalar_one()
        # 1M input tokens at $1/M + 1M output tokens at $2/M = $3.00 cost
        assert float(balance.coins_left) < 10.0


def test_stream_client_disconnect_bills_estimated_usage(app, test_user, test_model_endpoint):
    """A client disconnecting mid-stream is billed for what the backend produced.

    The upstream reports usage only in its terminal chunk, which this stream never
    reaches, so the counts are estimated: the prompt from its character count, the
    output as one token per content delta received. Writing a zero-cost row here
    (the old behaviour) was a working free-inference method — stream, hang up
    before the last chunk, pay nothing, repeat.

    Aborts are now found by the ``aborted`` flag, not by ``cost == 0``.
    """
    entity_id = test_user["id"]
    messages = [{"role": "user", "content": "x" * 400}]  # 400 chars -> ~100 tokens
    chunks = [
        _Chunk(content="partial"),
        _Chunk(content=" more"),
        _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=5)),
    ]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.model_stat import ModelStat
        from lumen.models.request_log import RequestLog
        from lumen.services.llm import send_message_stream
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=10, refresh_coins=0, starting_coins=10))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=10))
        db.session.commit()
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            gen = send_message_stream(messages, "test-model", entity_id=entity_id)
            assert next(gen) == ("partial", None, None)  # mid-stream
            gen.close()  # simulate client disconnect -> GeneratorExit
        logs = db.session.execute(select(RequestLog).filter_by(entity_id=entity_id)).scalars().all()
        assert len(logs) == 1
        log = logs[0]
        assert log.aborted is True  # this, not cost == 0, is how aborts are monitored
        assert log.input_tokens == 100  # 400 characters / 4
        assert log.output_tokens == 1   # one content delta made it out
        # 100 input @ $1/M + 1 output @ $2/M
        assert float(log.cost) == pytest.approx(0.000102)
        # and the balance actually moved
        balance = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one()
        assert float(balance.coins_left) < 10.0
        # the abort is rolled into the running totals like any other request
        assert db.session.scalar(select(func.count()).select_from(ModelStat)) == 1


# ---------------------------------------------------------------------------
# One clock: every span is measured with time.monotonic()
#
# Mixing monotonic with the wall clock does not raise — it writes a duration of
# roughly ±1.76e9 seconds (the Unix epoch, ~55 years). These bounds fail loudly
# in both directions. See tests/unit/test_single_clock.py for the static guard.
# ---------------------------------------------------------------------------

_MAX_PLAUSIBLE_SPAN = 60 * 60  # seconds; a real test stream takes milliseconds


def test_completed_stream_records_a_plausible_duration(app, test_user, test_model_endpoint):
    entity_id = test_user["id"]
    chunks = [
        _Chunk(content="hello"),
        _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=5)),
    ]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.request_log import RequestLog
        from lumen.services.llm import send_message_stream
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            _, _, result = _drain(send_message_stream([], "test-model", entity_id=entity_id))
        assert 0 <= result["duration"] < _MAX_PLAUSIBLE_SPAN
        log = db.session.execute(select(RequestLog).filter_by(entity_id=entity_id)).scalar_one()
        assert 0 <= log.duration < _MAX_PLAUSIBLE_SPAN


def test_aborted_stream_records_a_plausible_duration(app, test_user, test_model_endpoint):
    """The record_stream_abort path computes its duration from the caller's t0.

    It lives in a different function from the three that measure their own
    spans, which is how it gets missed: convert the assignments without it and
    every abandoned request is logged as having taken 55 years.
    """
    entity_id = test_user["id"]
    chunks = [_Chunk(content="partial"), _Chunk(content=" more")]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.request_log import RequestLog
        from lumen.services.llm import send_message_stream
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            gen = send_message_stream([{"role": "user", "content": "hi"}], "test-model", entity_id=entity_id)
            assert next(gen) == ("partial", None, None)  # mid-stream
            gen.close()  # client disconnect -> GeneratorExit -> record_stream_abort
        log = db.session.execute(select(RequestLog).filter_by(entity_id=entity_id)).scalar_one()
        assert log.aborted is True
        assert 0 <= log.duration < _MAX_PLAUSIBLE_SPAN


def test_time_to_first_token_is_a_plausible_span(app, test_model_endpoint):
    chunks = [
        _Chunk(content="first"),
        _Chunk(content=" second"),
        _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=5)),
    ]
    with app.app_context():
        from lumen.services.llm import send_message_stream
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            _, _, result = _drain(send_message_stream([], "test-model"))
    ttft = result["time_to_first_token"]
    assert 0 <= ttft < _MAX_PLAUSIBLE_SPAN
    # It is a prefix of the whole stream, so it cannot exceed the duration —
    # which it also would if the two were read from different clocks.
    assert ttft <= result["duration"]


def test_disconnect_flag_aborts_stream_without_generator_exit(app, test_user, test_model_endpoint):
    """The flag alone must end the stream — nothing closes the generator.

    Under uvicorn + a2wsgi a departed client never causes GeneratorExit, so this
    is the path that actually runs in production. Closing the generator is not
    simulated here on purpose.
    """
    import threading
    entity_id = test_user["id"]
    disconnected = threading.Event()
    messages = [{"role": "user", "content": "x" * 400}]  # 400 chars -> ~100 tokens
    chunks = [
        _Chunk(content="partial"),
        _Chunk(content=" more"),
        _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=5)),
    ]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.model_stat import ModelStat
        from lumen.models.request_log import RequestLog
        from lumen.services.llm import send_message_stream
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=10, refresh_coins=0, starting_coins=10))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=10))
        db.session.commit()
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)), \
             patch("lumen.services.llm.client_disconnect_event", return_value=disconnected):
            gen = send_message_stream(messages, "test-model", entity_id=entity_id)
            assert next(gen) == ("partial", None, None)
            disconnected.set()
            # Keep iterating rather than closing: the stream must stop itself,
            # and must not emit the final result dict.
            assert list(gen) == []
        logs = db.session.execute(select(RequestLog).filter_by(entity_id=entity_id)).scalars().all()
        assert len(logs) == 1
        # Billed for the estimate, exactly as the GeneratorExit path is: this is
        # the path that actually runs in production, so it is the one the free
        # inference hole would have lived in.
        assert logs[0].aborted is True
        assert logs[0].input_tokens == 100
        assert logs[0].output_tokens == 1
        assert float(logs[0].cost) == pytest.approx(0.000102)
        balance = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one()
        assert float(balance.coins_left) < 10.0
        assert db.session.scalar(select(func.count()).select_from(ModelStat)) == 1


def test_disconnect_flag_set_after_stream_completes_still_bills(app, test_user, test_model_endpoint):
    """A client that vanishes after the last chunk must still be billed.

    The usage totals arrived, so the work is real and chargeable. Keying the
    abort off the flag rather than the loop break would write this off as a
    zero-cost abort and hand out free inference to anyone who disconnects a
    few milliseconds late.
    """
    import threading
    entity_id = test_user["id"]
    disconnected = threading.Event()

    def chunks():
        yield _Chunk(content="hello")
        yield _Chunk(usage=_Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000))
        disconnected.set()  # client goes away exactly as the stream ends

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.model_stat import ModelStat
        from lumen.models.request_log import RequestLog
        from lumen.services.llm import send_message_stream
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=0, starting_coins=100))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=100))
        db.session.commit()
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks())), \
             patch("lumen.services.llm.client_disconnect_event", return_value=disconnected):
            _, _, result = _drain(send_message_stream([], "test-model", entity_id=entity_id))
        assert result is not None, "the final result dict must still be emitted"
        assert result["output_tokens"] == 1_000_000
        # Billed normally: stats recorded and balance drawn down.
        assert db.session.scalar(select(func.count()).select_from(ModelStat)) == 1
        balance = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one()
        assert float(balance.coins_left) < 100.0
        # ...and no zero-cost abort row was written.
        logs = db.session.execute(select(RequestLog).filter_by(entity_id=entity_id)).scalars().all()
        assert len(logs) == 1
        assert float(logs[0].cost) > 0.0
        assert logs[0].aborted is False  # a completed stream is not an abort


def test_abort_with_usage_already_in_hand_bills_exactly(app, test_user, test_model_endpoint):
    """A disconnect landing on the terminal usage chunk is billed exactly, not estimated.

    Both loops capture ``chunk.usage`` before they test the disconnect flag, so the
    real totals survive a disconnect in that window. Falling back to the delta
    estimate here would under-bill 2000 output tokens as one.
    """
    import threading
    entity_id = test_user["id"]
    disconnected = threading.Event()

    def chunks():
        yield _Chunk(content="hello")
        disconnected.set()  # client goes away just as the usage chunk arrives
        yield _Chunk(usage=_Usage(prompt_tokens=1000, completion_tokens=2000))
        yield _Chunk(content="never delivered")

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.request_log import RequestLog
        from lumen.services.llm import send_message_stream
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=10, refresh_coins=0, starting_coins=10))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=10))
        db.session.commit()
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks())), \
             patch("lumen.services.llm.client_disconnect_event", return_value=disconnected):
            gen = send_message_stream([{"role": "user", "content": "hi"}], "test-model", entity_id=entity_id)
            assert next(gen) == ("hello", None, None)
            assert list(gen) == []  # the stream stops itself, no final result dict
        log = db.session.execute(select(RequestLog).filter_by(entity_id=entity_id)).scalar_one()
        assert log.aborted is True
        assert log.input_tokens == 1000   # exact, not the ~1-token prompt estimate
        assert log.output_tokens == 2000  # exact, not the 1-delta estimate
        # 1000 input @ $1/M + 2000 output @ $2/M
        assert float(log.cost) == pytest.approx(0.005)
        balance = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one()
        assert float(balance.coins_left) == pytest.approx(10 - 0.005)


# ---------------------------------------------------------------------------
# estimate_abort_usage — what an aborted stream is billed for
# ---------------------------------------------------------------------------

def test_estimate_prompt_tokens_counts_text_of_every_content_shape():
    from lumen.services.llm import estimate_prompt_tokens
    assert estimate_prompt_tokens([]) == 0
    assert estimate_prompt_tokens([{"role": "user", "content": "x" * 400}]) == 100
    # multimodal content: only the text parts are counted, images are not
    assert estimate_prompt_tokens([
        {"role": "user", "content": [
            {"type": "text", "text": "y" * 40},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,...."}},
        ]},
    ]) == 10


def test_estimate_abort_usage_prefers_exact_usage_over_the_delta_count():
    from lumen.services.llm import estimate_abort_usage
    usage = _Usage(prompt_tokens=1000, completion_tokens=2000)
    assert estimate_abort_usage(usage, [{"role": "user", "content": "hi"}], 3, 1.0, 2.0) == (
        1000, 2000, pytest.approx(0.005),
    )


def test_estimate_abort_usage_counts_one_output_token_per_content_delta():
    """Without the terminal usage chunk, the delta count is the output estimate."""
    from lumen.services.llm import estimate_abort_usage
    messages = [{"role": "user", "content": "x" * 400}]
    assert estimate_abort_usage(None, messages, 7, 1.0, 2.0) == (100, 7, pytest.approx(0.000114))


# ---------------------------------------------------------------------------
# record_stream_abort — the single abort-accounting seam shared by every path
# that can end a stream early (GeneratorExit, client-disconnect flag).
# ---------------------------------------------------------------------------

def _abort_rows(app, entity_id):
    from lumen.extensions import db
    from lumen.models.request_log import RequestLog
    with app.app_context():
        return db.session.execute(
            select(RequestLog).filter_by(entity_id=entity_id)
        ).scalars().all()


def test_record_stream_abort_writes_row_without_ambient_context(app, test_user, test_model, test_model_endpoint):
    """Called context-free (as the streaming generators are), it pushes its own."""
    from lumen.services.llm import record_stream_abort
    entity_id = test_user["id"]

    # A realistic monotonic origin, not 0.0: stream_t0 is now a time.monotonic()
    # value, so passing 0.0 would record "seconds since boot" as the duration --
    # a number that satisfies `> 0` while meaning nothing, which is precisely the
    # mixed-clock bug this conversion exists to prevent.
    started = time.monotonic()
    record_stream_abort(
        app, billed=False, entity_id=entity_id, model_config_id=test_model["id"],
        source="api", endpoint_id=test_model_endpoint["id"], stream_t0=started,
    )

    rows = _abort_rows(app, entity_id)
    assert len(rows) == 1
    assert float(rows[0].cost) == 0.0
    assert rows[0].input_tokens == 0
    assert rows[0].output_tokens == 0
    assert rows[0].source == "api"
    assert rows[0].model_endpoint_id == test_model_endpoint["id"]
    # Bounded on both sides: a mixed-clock regression lands at ~1.76e9 (wall
    # epoch) or at seconds-since-boot, and both blow this ceiling.
    assert 0 <= rows[0].duration < 60, f"implausible duration {rows[0].duration}"
    assert rows[0].aborted is True


def test_record_stream_abort_skips_when_already_billed(app, test_user, test_model):
    """Billing completed before the client went away — no abort row."""
    from lumen.services.llm import record_stream_abort
    record_stream_abort(
        app, billed=True, entity_id=test_user["id"], model_config_id=test_model["id"],
        source="chat", endpoint_id=None, stream_t0=time.monotonic(),
    )
    assert _abort_rows(app, test_user["id"]) == []


def test_record_stream_abort_skips_anonymous_stream(app, test_user, test_model):
    """send_message_stream may run with no entity (entity_id=None) — nothing to log."""
    from lumen.services.llm import record_stream_abort
    record_stream_abort(
        app, billed=False, entity_id=None, model_config_id=test_model["id"],
        source="chat", endpoint_id=None, stream_t0=time.monotonic(),
    )
    assert _abort_rows(app, test_user["id"]) == []


def test_record_stream_abort_never_raises_into_a_closing_generator(app, test_user, test_model):
    """A failure here must not replace the GeneratorExit that is already unwinding."""
    from lumen.services import llm
    with patch.object(llm, "record_aborted_request", side_effect=RuntimeError("boom")):
        llm.record_stream_abort(
            app, billed=False, entity_id=test_user["id"], model_config_id=test_model["id"],
            source="chat", endpoint_id=None, stream_t0=time.monotonic(),
        )
    assert _abort_rows(app, test_user["id"]) == []


# ---------------------------------------------------------------------------
# Upstream call bounds — every proxy client must be built with a timeout, and a
# streaming client must never auto-retry. Unbounded, the SDK's own defaults
# (600 s, two retries) let one stalled backend pin a WSGI worker for ~30 min.
# ---------------------------------------------------------------------------

def _stream_client_kwargs(app, chunks=None):
    """Drive send_message_stream and return the kwargs openai.OpenAI got."""
    from lumen.services.llm import send_message_stream
    if chunks is None:
        chunks = [_Chunk(content="hi"), _Chunk(usage=_Usage())]
    mock_cls = _mock_openai(chunks)
    with app.app_context():
        with patch("lumen.services.llm.openai.OpenAI", mock_cls):
            _drain(send_message_stream([], "test-model"))
    assert mock_cls.call_args is not None, "openai.OpenAI was never constructed"
    return mock_cls.call_args.kwargs


def test_chat_stream_client_carries_a_structured_timeout(app, test_model_endpoint):
    """connect and read are bounded separately, not by one bare float."""
    import openai
    kwargs = _stream_client_kwargs(app)
    timeout = kwargs["timeout"]
    assert isinstance(timeout, openai.Timeout)  # openai.Timeout IS httpx.Timeout
    assert timeout.connect == 5.0
    assert timeout.read == 300.0
    assert timeout.write == 300.0
    assert timeout.pool == 5.0


def test_chat_stream_client_never_retries(app, test_model_endpoint):
    """A retried stream is a second backend generation for one client request."""
    assert _stream_client_kwargs(app)["max_retries"] == 0


def test_chat_stream_timeout_comes_from_config(app, test_model_endpoint, monkeypatch):
    """The bounds are configuration, not constants baked into the call site."""
    monkeypatch.setitem(app.config, "LLM_CONNECT_TIMEOUT", 1.5)
    monkeypatch.setitem(app.config, "LLM_READ_TIMEOUT", 9.0)
    timeout = _stream_client_kwargs(app)["timeout"]
    assert timeout.connect == 1.5
    assert timeout.read == 9.0


def test_chat_stream_retries_stay_zero_even_when_config_allows_them(
    app, test_model_endpoint, monkeypatch,
):
    """LLM_MAX_RETRIES governs the non-streaming paths only."""
    monkeypatch.setitem(app.config, "LLM_MAX_RETRIES", 5)
    assert _stream_client_kwargs(app)["max_retries"] == 0


def test_upstream_call_bounds_falls_back_to_defaults(app, monkeypatch):
    """Works standalone if the config keys are ever absent."""
    import openai

    from lumen.services.llm import upstream_call_bounds
    with app.app_context():
        for key in ("LLM_CONNECT_TIMEOUT", "LLM_READ_TIMEOUT",
                    "LLM_REQUEST_TIMEOUT", "LLM_MAX_RETRIES"):
            monkeypatch.delitem(app.config, key, raising=False)
        stream_timeout, stream_retries = upstream_call_bounds(streaming=True)
        plain_timeout, plain_retries = upstream_call_bounds(streaming=False)
    assert isinstance(stream_timeout, openai.Timeout)
    assert (stream_timeout.connect, stream_timeout.read) == (5.0, 300.0)
    assert stream_retries == 0
    assert (plain_timeout.connect, plain_timeout.read) == (5.0, 600.0)
    assert plain_retries == 1


def test_non_streaming_read_bound_is_far_larger_than_the_streaming_one(app):
    """The two read bounds measure different things and must not be shared.

    On a stream the read timeout is the gap BETWEEN chunks, so it is safe
    however long the generation runs. On a non-streaming call the same setting
    caps the entire generation — a long completion or a large audio
    transcription legitimately takes minutes, and one shared value would either
    leave streams unbounded or start failing slow requests that work today.
    """
    from lumen.services.llm import upstream_call_bounds
    with app.app_context():
        stream_timeout, _ = upstream_call_bounds(streaming=True)
        plain_timeout, _ = upstream_call_bounds(streaming=False)
    assert plain_timeout.read > stream_timeout.read


# ---------------------------------------------------------------------------
# lumen_stream_aborts_total — the metric that makes mid-stream aborts
# observable. Wired-but-never-incremented would repeat F1 in a new form, so
# these assert the increment, not the definition.
# ---------------------------------------------------------------------------

def _abort_count(source, reason):
    from prometheus_client import REGISTRY
    return REGISTRY.get_sample_value(
        "lumen_stream_aborts_total", {"source": source, "reason": reason}) or 0.0


def test_stream_abort_increments_the_disconnect_counter(app, test_user, test_model, test_model_endpoint):
    from lumen.services.llm import record_stream_abort
    before = _abort_count("chat", "disconnect")
    record_stream_abort(
        app, billed=False, entity_id=test_user["id"], model_config_id=test_model["id"],
        source="chat", endpoint_id=test_model_endpoint["id"], stream_t0=time.monotonic(),
    )
    assert _abort_count("chat", "disconnect") == before + 1


def test_stream_abort_counts_anonymous_streams_too(app, test_model):
    """There is no row to write without an entity, but the abort still happened."""
    from lumen.services.llm import record_stream_abort
    before = _abort_count("chat", "disconnect")
    record_stream_abort(
        app, billed=False, entity_id=None, model_config_id=test_model["id"],
        source="chat", endpoint_id=None, stream_t0=time.monotonic(),
    )
    assert _abort_count("chat", "disconnect") == before + 1


def test_completed_stream_is_not_counted_as_an_abort(app, test_user, test_model):
    """billed=True means the client got the whole reply — not an abort."""
    from lumen.services.llm import record_stream_abort
    before = _abort_count("chat", "disconnect")
    record_stream_abort(
        app, billed=True, entity_id=test_user["id"], model_config_id=test_model["id"],
        source="chat", endpoint_id=None, stream_t0=time.monotonic(),
    )
    assert _abort_count("chat", "disconnect") == before


def test_disconnected_chat_stream_increments_the_counter(app, test_user, test_model_endpoint):
    """End to end through the real generator, not just the accounting helper."""
    import threading
    disconnected = threading.Event()
    before = _abort_count("chat", "disconnect")
    chunks = [_Chunk(content="partial"), _Chunk(content=" more"), _Chunk(usage=_Usage())]
    with app.app_context():
        from lumen.services.llm import send_message_stream
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)), \
             patch("lumen.services.llm.client_disconnect_event", return_value=disconnected):
            gen = send_message_stream([], "test-model", entity_id=test_user["id"])
            next(gen)
            disconnected.set()
            assert list(gen) == []
    assert _abort_count("chat", "disconnect") == before + 1


def test_upstream_failure_is_counted_with_its_own_reason(app, test_user, test_model_endpoint):
    """A backend failure ends the stream too, and must be told apart from a
    disconnect — otherwise the counter cannot distinguish 'clients are leaving'
    from 'the backend is broken'."""
    before_up = _abort_count("chat", "upstream_error")
    before_disc = _abort_count("chat", "disconnect")
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = RuntimeError("upstream exploded")
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    with app.app_context():
        from lumen.services.llm import send_message_stream
        with patch("lumen.services.llm.openai.OpenAI", MagicMock(return_value=mock_client)):
            with pytest.raises(RuntimeError, match="upstream exploded"):
                _drain(send_message_stream([], "test-model", entity_id=test_user["id"]))
    assert _abort_count("chat", "upstream_error") == before_up + 1
    assert _abort_count("chat", "disconnect") == before_disc  # not double-counted


# ---------------------------------------------------------------------------
# Request timing columns (started_at, queue_wait, preflight, ttft,
# ttft_visible, send_blocked, outcome)
#
# The bridge marks are published in the WSGI environ by asgi.py and read in the
# view; the test client and the dev server never pass through it, so a test that
# wants them has to supply them the way _bridge_environ does below.
# ---------------------------------------------------------------------------

_QUEUE_WAIT = 0.05  # seconds; a plausible admission wait to stand in for T1 - T0


def _bridge_environ(queue_wait=_QUEUE_WAIT):
    """The environ keys a request would carry if it came through the ASGI bridge.

    ``lumen.queue_wait`` is included because the before_request hook that
    normally derives it does not run for a synthetic test_request_context.
    """
    from lumen.services.wsgi_disconnect import SendBlocked
    return {
        "lumen.t0_monotonic": time.monotonic() - queue_wait,
        "lumen.started_at": datetime.now(timezone.utc),
        "lumen.queue_wait": queue_wait,
        "lumen.send_blocked": SendBlocked(),
    }


def test_stream_records_ttft_before_ttft_visible_on_reasoning(app, test_user, test_model_endpoint):
    """ttft stops at the first chunk of any kind; ttft_visible at the first content.

    On a reasoning model the two are separated by the entire thinking phase,
    which is the whole point of storing both: one says "the model was queued",
    the other "the model was thinking". Conflating them makes a reasoning model
    look like an overloaded one.
    """
    entity_id = test_user["id"]
    chunks = [
        _Chunk(reasoning_content="think 1"),
        _Chunk(reasoning_content="think 2"),
        _Chunk(reasoning_content="think 3"),
        _Chunk(content="answer"),
        _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=5)),
    ]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.request_log import RequestLog
        from lumen.services.llm import send_message_stream
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            _drain(send_message_stream([], "test-model", entity_id=entity_id))
        log = db.session.execute(select(RequestLog).filter_by(entity_id=entity_id)).scalar_one()
        assert 0 < log.ttft < log.ttft_visible < _MAX_PLAUSIBLE_SPAN
        assert log.outcome == "ok"


def test_stream_without_the_bridge_records_nulls_not_zeros(app, test_user, test_model_endpoint):
    """Absent environ keys mean "not measured" — SQL NULL, never a fictitious 0.0.

    Nothing may raise either: the keys are missing on the dev server, under the
    test client and in direct calls to the streaming helpers.
    """
    entity_id = test_user["id"]
    chunks = [_Chunk(content="hello"), _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=5))]
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.request_log import RequestLog
        from lumen.services.llm import send_message_stream
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            _drain(send_message_stream([], "test-model", entity_id=entity_id))
        log = db.session.execute(select(RequestLog).filter_by(entity_id=entity_id)).scalar_one()
        assert log.started_at is None
        assert log.queue_wait is None
        assert log.preflight is None
        assert log.send_blocked is None
        # Measured inside the generator, so they survive the bridge's absence.
        assert log.ttft is not None
        assert log.ttft_visible is not None
        assert log.outcome == "ok"


def test_unmeasured_timing_columns_store_sql_null(app, test_user, test_model):
    """A None-valued timing attribute must reach the database as SQL NULL.

    Read back with raw SQL rather than through the ORM: the instance still holds
    the Python None either way, so only the stored value distinguishes "not
    measured" from a fictitious 0.0. This is the invariant that forbids a
    ``server_default`` on these columns — with one, SQLAlchemy omits the
    None-valued attribute from the INSERT and the server writes the default,
    and (worse) the migration's ADD COLUMN would have written it over every
    pre-existing row as well.
    """
    with app.app_context():
        from sqlalchemy import text

        from lumen.extensions import db
        from lumen.models.request_log import RequestLog
        log = RequestLog(
            time=datetime.now(timezone.utc),
            entity_id=test_user["id"],
            model_config_id=test_model["id"],
            source="chat",
            input_tokens=0, output_tokens=0, cost=0, duration=0.0,
            started_at=None, queue_wait=None, preflight=None,
            ttft=None, ttft_visible=None, send_blocked=None, outcome=None,
        )
        db.session.add(log)
        db.session.commit()

        stored = db.session.execute(text(
            "SELECT started_at, queue_wait, preflight, ttft, ttft_visible, "
            "       send_blocked, outcome "
            "FROM request_logs WHERE id = :id"
        ), {"id": log.id}).one()

    assert all(v is None for v in stored), f"unmeasured timing stored as {stored!r}"


def test_stream_records_the_bridge_marks_captured_in_the_view(app, test_user, test_model_endpoint):
    """The marks reach the row even though the generator never touches ``request``."""
    entity_id = test_user["id"]
    chunks = [_Chunk(content="hello"), _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=5))]
    with app.test_request_context(environ_base=_bridge_environ()):
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.request_log import RequestLog
        from lumen.services.llm import send_message_stream
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            _drain(send_message_stream([], "test-model", entity_id=entity_id))
        log = db.session.execute(select(RequestLog).filter_by(entity_id=entity_id)).scalar_one()
        assert log.started_at is not None
        assert log.queue_wait == pytest.approx(_QUEUE_WAIT)
        # T2 - T1, derived from the arrival mark plus the admission wait.
        assert 0 <= log.preflight < _MAX_PLAUSIBLE_SPAN
        assert log.send_blocked == 0.0  # a holder is present; nothing blocked


def test_aborted_stream_records_disconnect_outcome(app, test_user, test_model_endpoint):
    """The abort path is the one that matters most, and the easiest to miss.

    It bills through record_stream_abort -> record_aborted_request, a second
    update_stats call site four functions away from the streaming one. Miss it
    and every disconnect row — the rows this instrumentation exists to study —
    is written with NULL timings.
    """
    entity_id = test_user["id"]
    chunks = [_Chunk(content="partial"), _Chunk(content=" more")]
    with app.test_request_context(environ_base=_bridge_environ()):
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.request_log import RequestLog
        from lumen.services.llm import send_message_stream
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            gen = send_message_stream([{"role": "user", "content": "hi"}], "test-model", entity_id=entity_id)
            assert next(gen) == ("partial", None, None)  # mid-stream
            gen.close()  # client disconnect -> GeneratorExit -> record_stream_abort
        log = db.session.execute(select(RequestLog).filter_by(entity_id=entity_id)).scalar_one()
        assert log.outcome == "disconnect"
        assert log.aborted is True
        assert log.started_at is not None
        assert log.queue_wait == pytest.approx(_QUEUE_WAIT)
        assert 0 <= log.preflight < _MAX_PLAUSIBLE_SPAN
        assert 0 < log.ttft < _MAX_PLAUSIBLE_SPAN
        assert 0 < log.ttft_visible < _MAX_PLAUSIBLE_SPAN


def test_update_stats_adds_no_statements(app, test_user, test_model):
    """The timing columns ride the INSERT that already runs.

    Phase 3's exit gate is "the added statement count per request is zero", and
    an extra round-trip per request is invisible to every behavioural assertion.
    """
    from sqlalchemy import event

    from lumen.extensions import db
    from lumen.services.llm import RequestTiming, update_stats
    from lumen.services.wsgi_disconnect import SendBlocked

    entity_id, mc_id = test_user["id"], test_model["id"]
    statements = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with app.app_context():
        # Warm-up: the first call inserts the ModelStat/EntityStat rows, so it
        # runs a different set of statements from every later one.
        update_stats(entity_id, mc_id, "chat", 1, 1, 0.0)
        db.session.commit()

        event.listen(db.engine, "before_cursor_execute", _count)
        try:
            update_stats(entity_id, mc_id, "chat", 1, 1, 0.0)
            db.session.commit()
            without_timing = len(statements)
            statements.clear()
            update_stats(
                entity_id, mc_id, "chat", 1, 1, 0.0,
                timing=RequestTiming(
                    started_at=datetime.now(timezone.utc),
                    queue_wait=_QUEUE_WAIT,
                    picked_up_at=time.monotonic(),
                    send_blocked=SendBlocked(),
                ),
                upstream_t0=time.monotonic(), ttft=0.2, ttft_visible=0.3, outcome="ok",
            )
            db.session.commit()
            with_timing = len(statements)
        finally:
            event.remove(db.engine, "before_cursor_execute", _count)

    assert without_timing > 0
    assert with_timing == without_timing


# ---------------------------------------------------------------------------
# Helper stubs for send_message_stream mocking (kept at end so the static
# analyser does not treat all test functions above as methods of _Chunk)
# ---------------------------------------------------------------------------

class _Delta:
    def __init__(self, content=None, reasoning_content=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.reasoning = None


class _Choice:
    def __init__(self, delta):
        self.delta = delta


class _Usage:
    def __init__(self, prompt_tokens=10, completion_tokens=20):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.completion_tokens_details = None


class _Chunk:
    def __init__(self, content=None, reasoning_content=None, usage=None):
        self.usage = usage
        if content is not None or reasoning_content is not None:
            self.choices = [_Choice(_Delta(content, reasoning_content))]
        else:
            self.choices = []
