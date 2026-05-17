"""Additional LLM service tests: groups, endpoints, coin functions, stats."""
from datetime import datetime
from http import HTTPStatus
from unittest.mock import MagicMock, patch
from sqlalchemy import func, select

import pytest

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
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=_COIN_LIMIT, refresh_coins=0, starting_coins=50))
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
        db.session.add(EntityModelAccess(entity_id=entity_id, model_config_id=model_id, access_type="blacklist"))
        db.session.commit()
        ok, code, msg = check_coin_budget(entity_id, model_id)
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
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=_COIN_LIMIT, refresh_coins=0, starting_coins=_COIN_LIMIT))
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=0))
        db.session.commit()
        ok, code, _ = check_coin_budget(entity_id, model_id)
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
        import time
        update_stats(entity_id, model_id, "api", 50, _IN_TOKENS, 0.0001)
        db.session.commit()
        time.sleep(0.001)  # ensure distinct timestamps for primary key
        update_stats(entity_id, model_id, "api", 50, _IN_TOKENS, 0.0001)
        db.session.commit()
        stat = db.session.execute(select(ModelStat).filter_by(entity_id=entity_id, model_config_id=model_id, source="api")).scalar_one_or_none()
        assert stat.requests == 2
        assert stat.input_tokens == _IN_TOKENS
        assert stat.output_tokens == _OUT_TOKENS


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
