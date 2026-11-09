"""send_message_stream with ``resolved=``: the view already picked the model and
endpoint, so the generator streams on those values without a DB phase of its own.

The target here deliberately differs from what the database holds (URL, remote
name, costs), so each assertion shows the generator used what it was handed
rather than looking the model or endpoint up again.
"""
from unittest.mock import patch

import pytest
from sqlalchemy import event, select

from lumen.services.cost import calculate_cost
from lumen.services.llm import StreamTarget
from tests.unit.test_cache_salt_forwarding import _expected_salt
from tests.unit.test_llm_functions import _HUGE_TOKEN_COUNT, _Chunk, _drain, _mock_openai, _Usage

_IN_COST = 3.0
_OUT_COST = 4.0


def _target(test_model, test_model_endpoint):
    return StreamTarget(
        remote_model="resolved-remote",
        url="http://resolved.example/v1",
        api_key="resolved-key",
        endpoint_id=test_model_endpoint["id"],
        model_config_id=test_model["id"],
        input_cost=_IN_COST,
        output_cost=_OUT_COST,
    )


def _no_lookup(*args, **kwargs):
    raise AssertionError("the generator looked up a model or endpoint despite resolved=")


def _pool(db, entity_id):
    from lumen.models.entity_balance import EntityBalance
    from lumen.models.entity_limit import EntityLimit
    db.session.add(EntityLimit(entity_id=entity_id, max_coins=10, refresh_coins=0, starting_coins=10))
    db.session.add(EntityBalance(entity_id=entity_id, coins_left=10))
    db.session.commit()


def test_resolved_stream_does_no_db_work_before_the_first_token(app, test_user, test_model, test_model_endpoint):
    from lumen.extensions import db
    from lumen.services.llm import send_message_stream
    log = []

    def _statement(conn, cursor, statement, parameters, context, executemany):
        log.append(statement)

    def _checkout(dbapi_conn, record, proxy):
        log.append("CHECKOUT")

    mock_cls = _mock_openai([_Chunk(content="Hello"), _Chunk(usage=_Usage())])
    client = mock_cls.return_value
    chunks = client.chat.completions.create.return_value

    def _create(**kwargs):
        log.append("UPSTREAM")
        return chunks

    client.chat.completions.create.side_effect = _create
    with app.app_context():
        engine = db.engine
        db.session.remove()
        event.listen(engine, "before_cursor_execute", _statement)
        event.listen(engine.pool, "checkout", _checkout)
        try:
            with patch("lumen.services.llm.openai.OpenAI", mock_cls), \
                    patch("lumen.services.llm.get_next_endpoint", _no_lookup), \
                    patch("lumen.services.llm.resolve_model_config", _no_lookup):
                gen = send_message_stream([], "test-model", entity_id=test_user["id"],
                                          resolved=_target(test_model, test_model_endpoint))
                assert next(gen) == ("Hello", None, None)
                gen.close()
        finally:
            event.remove(engine, "before_cursor_execute", _statement)
            event.remove(engine.pool, "checkout", _checkout)
    assert log[0] == "UPSTREAM", f"DB work before the upstream call: {log[:log.index('UPSTREAM')]}"
    assert mock_cls.call_args.kwargs["base_url"] == "http://resolved.example/v1"
    assert mock_cls.call_args.kwargs["api_key"] == "resolved-key"
    assert client.chat.completions.create.call_args.kwargs["model"] == "resolved-remote"


def test_resolved_stream_bills_against_the_target(app, test_user, test_model, test_model_endpoint):
    from lumen.extensions import db
    from lumen.models.entity_balance import EntityBalance
    from lumen.models.request_log import RequestLog
    from lumen.services.llm import send_message_stream
    entity_id = test_user["id"]
    chunks = [_Chunk(content="token"),
              _Chunk(usage=_Usage(prompt_tokens=_HUGE_TOKEN_COUNT, completion_tokens=_HUGE_TOKEN_COUNT))]
    with app.app_context():
        _pool(db, entity_id)
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            texts, _, result = _drain(send_message_stream(
                [], "test-model", entity_id=entity_id, resolved=_target(test_model, test_model_endpoint)))
        expected = calculate_cost(_HUGE_TOKEN_COUNT, _HUGE_TOKEN_COUNT, _IN_COST, _OUT_COST)
        assert texts == ["token"]
        assert result["model"] == "resolved-remote"
        assert result["cost"] == pytest.approx(expected)
        log = db.session.execute(select(RequestLog).filter_by(entity_id=entity_id)).scalar_one()
        assert log.aborted is False
        assert log.model_config_id == test_model["id"]
        assert log.model_endpoint_id == test_model_endpoint["id"]
        assert float(log.cost) == pytest.approx(expected)
        balance = db.session.execute(select(EntityBalance).filter_by(entity_id=entity_id)).scalar_one()
        assert float(balance.coins_left) == pytest.approx(10 - expected)


def test_resolved_stream_abort_bills_against_the_target(app, test_user, test_model, test_model_endpoint):
    from lumen.extensions import db
    from lumen.models.request_log import RequestLog
    from lumen.services.llm import send_message_stream
    entity_id = test_user["id"]
    messages = [{"role": "user", "content": "x" * 400}]  # ~100 tokens
    chunks = [_Chunk(content="partial"), _Chunk(content=" more"), _Chunk(usage=_Usage())]
    with app.app_context():
        _pool(db, entity_id)
        with patch("lumen.services.llm.openai.OpenAI", _mock_openai(chunks)):
            gen = send_message_stream(messages, "test-model", entity_id=entity_id,
                                      resolved=_target(test_model, test_model_endpoint))
            assert next(gen) == ("partial", None, None)
            gen.close()  # client disconnect -> GeneratorExit
        log = db.session.execute(select(RequestLog).filter_by(entity_id=entity_id)).scalar_one()
        assert log.aborted is True
        assert log.model_config_id == test_model["id"]
        assert log.model_endpoint_id == test_model_endpoint["id"]
        assert log.input_tokens == 100
        assert log.output_tokens == 1
        assert float(log.cost) == pytest.approx(calculate_cost(100, 1, _IN_COST, _OUT_COST))


def test_resolved_stream_forwards_the_entity_cache_salt(app, test_user, test_model, test_model_endpoint):
    from lumen.extensions import db
    from lumen.services.llm import send_message_stream
    mock_cls = _mock_openai([_Chunk(usage=_Usage())])
    with app.app_context():
        _pool(db, test_user["id"])
        with patch("lumen.services.llm.openai.OpenAI", mock_cls):
            list(send_message_stream([], "test-model", entity_id=test_user["id"],
                                     resolved=_target(test_model, test_model_endpoint)))
    kwargs = mock_cls.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"] == {"cache_salt": _expected_salt(app, test_user["id"])}
