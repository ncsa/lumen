"""Translation between the Anthropic Messages protocol and the OpenAI flow.

Route-level behavior (auth, billing, errors) lives in
tests/routes/test_api_anthropic.py; this file covers the pure translation.
"""
import json
from pathlib import Path

import pytest

from lumen.services.anthropic_protocol import (
    AnthropicError,
    StreamTranslator,
    check_headers,
    count_tokens,
    stop_reason,
    translate_request,
    translate_response,
)


@pytest.fixture
def claude_code_request():
    """Claude Code 2.1.280 with the documented gateway settings; text redacted."""
    with open(Path(__file__).parent.parent / "fixtures" / "claude_code_request.json") as fh:
        return json.load(fh)


def _events(raw):
    """Parse rendered SSE strings into (event name, data dict) pairs."""
    out = []
    for chunk in raw:
        lines = chunk.strip().split("\n")
        out.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return out


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------

def test_known_version_accepted():
    check_headers({"anthropic-version": "2023-06-01"})


def test_missing_version_accepted():
    check_headers({})


def test_unknown_version_rejected():
    with pytest.raises(AnthropicError):
        check_headers({"anthropic-version": "2099-01-01"})


def test_unknown_beta_is_refused():
    check_headers({"anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14"})
    with pytest.raises(AnthropicError, match="anthropic-beta"):
        check_headers({"anthropic-beta": "made-up-beta-2099-01-01"})


# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------

def _req(**overrides):
    base = {"model": "m", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
    base.update(overrides)
    return base


def test_minimal_request():
    model, messages, stream, kwargs, extra = translate_request(_req())
    assert model == "m"
    assert messages == [{"role": "user", "content": "hi"}]
    assert stream is False
    assert kwargs["max_tokens"] == 16
    assert extra == {}


def test_max_tokens_is_required():
    with pytest.raises(AnthropicError):
        translate_request({"model": "m", "messages": [{"role": "user", "content": "hi"}]})


def test_max_tokens_must_be_a_positive_int():
    for bad in (0, -1, "16", True):
        with pytest.raises(AnthropicError):
            translate_request(_req(max_tokens=bad))


def test_model_and_messages_are_required():
    with pytest.raises(AnthropicError):
        translate_request({"max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]})
    with pytest.raises(AnthropicError):
        translate_request({"model": "m", "max_tokens": 1, "messages": []})


def test_system_string_becomes_the_first_message():
    _, messages, _, _, _ = translate_request(_req(system="be brief"))
    assert messages[0] == {"role": "system", "content": "be brief"}


def test_system_blocks_are_joined():
    _, messages, _, _, _ = translate_request(_req(system=[
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b", "cache_control": {"type": "ephemeral"}},
    ]))
    assert messages[0]["content"] == "a\n\nb"


def test_system_role_inside_messages_stays_in_place_as_a_reminder():
    """Claude Code's mid-conversation-system beta puts system turns in messages.
    Qwen-family chat templates raise on a system message that is not first, and
    folding the turn into the leading system message would change that message
    every turn and defeat the backend's prefix cache."""
    _, messages, _, _, _ = translate_request(_req(system="base", messages=[
        {"role": "user", "content": "hi"},
        {"role": "system", "content": [{"type": "text", "text": "new rules"}]},
        {"role": "assistant", "content": "ok"},
    ]))
    # Appended to the user turn before it: some templates require roles to alternate.
    assert messages == [
        {"role": "system", "content": "base"},
        {"role": "user", "content": "hi\n\n<system-reminder>\nnew rules\n</system-reminder>"},
        {"role": "assistant", "content": "ok"},
    ]


def test_a_growing_conversation_keeps_its_prefix():
    """Each request must extend the previous one, or prefix caching never hits."""
    turn = [{"role": "user", "content": "hi"}, {"role": "system", "content": "budget 10"}]
    longer = turn + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "more"},
                     {"role": "system", "content": "budget 9"}]
    _, first, _, _, _ = translate_request(_req(system="base", messages=turn))
    _, second, _, _, _ = translate_request(_req(system="base", messages=longer))
    assert second[:len(first)] == first


def test_multi_turn_roles_are_preserved():
    _, messages, _, _, _ = translate_request(_req(messages=[
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]))
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]


def test_unknown_role_is_refused():
    with pytest.raises(AnthropicError):
        translate_request(_req(messages=[{"role": "tool", "content": "x"}]))


def test_text_blocks_collapse_to_a_string():
    _, messages, _, _, _ = translate_request(_req(messages=[
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    ]))
    assert messages[0]["content"] == "hi"


def test_image_block_becomes_a_data_url():
    _, messages, _, _, _ = translate_request(_req(messages=[
        {"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
        ]},
    ]))
    parts = messages[0]["content"]
    assert parts[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}


def test_image_url_source():
    _, messages, _, _, _ = translate_request(_req(messages=[
        {"role": "user", "content": [
            {"type": "text", "text": "x"},
            {"type": "image", "source": {"type": "url", "url": "https://example.test/a.png"}},
        ]},
    ]))
    assert messages[0]["content"][1]["image_url"]["url"] == "https://example.test/a.png"


def test_unsupported_image_source_is_refused():
    with pytest.raises(AnthropicError):
        translate_request(_req(messages=[
            {"role": "user", "content": [{"type": "image", "source": {"type": "file", "file_id": "f"}}]},
        ]))


def test_tool_use_and_tool_result_round_trip():
    _, messages, _, _, _ = translate_request(_req(messages=[
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "sure"},
            {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"cmd": "ls"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"},
        ]},
    ]))
    assistant = messages[1]
    assert assistant["content"] == "sure"
    assert assistant["tool_calls"] == [{
        "id": "toolu_1", "type": "function",
        "function": {"name": "bash", "arguments": json.dumps({"cmd": "ls"})},
    }]
    assert messages[2] == {"role": "tool", "tool_call_id": "toolu_1", "content": "a.txt"}


def test_tool_result_blocks_are_joined_and_precede_user_text():
    _, messages, _, _, _ = translate_request(_req(messages=[
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1",
             "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
            {"type": "text", "text": "now what"},
        ]},
    ]))
    assert messages[0] == {"role": "tool", "tool_call_id": "toolu_1", "content": "a\nb"}
    assert messages[1] == {"role": "user", "content": "now what"}


def test_image_inside_a_tool_result_moves_to_the_next_user_message():
    """OpenAI tool messages carry text only; the picture must still reach the model."""
    _, messages, _, _, _ = translate_request(_req(messages=[
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t",
             "content": [{"type": "text", "text": "read shapes.png"},
                         {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}]},
            {"type": "text", "text": "describe it"},
        ]},
    ]))
    assert messages[0]["role"] == "tool"
    assert messages[0]["content"].startswith("read shapes.png\n[1 image(s)")
    assert messages[1] == {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
        {"type": "text", "text": "describe it"},
    ]}


def test_unknown_block_inside_a_tool_result_is_refused():
    with pytest.raises(AnthropicError):
        translate_request(_req(messages=[
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "document", "source": {}}]},
            ]},
        ]))


def test_thinking_blocks_are_dropped():
    _, messages, _, _, _ = translate_request(_req(messages=[
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm", "signature": "sig"},
            {"type": "text", "text": "answer"},
        ]},
    ]))
    assert messages[0] == {"role": "assistant", "content": "answer"}


def test_unknown_content_block_is_refused():
    with pytest.raises(AnthropicError):
        translate_request(_req(messages=[
            {"role": "user", "content": [{"type": "document", "source": {}}]},
        ]))


def test_tools_translate_to_openai_functions():
    _, _, _, kwargs, _ = translate_request(_req(tools=[
        {"name": "bash", "description": "run", "input_schema": {"type": "object", "properties": {}}},
    ]))
    assert kwargs["tools"] == [{
        "type": "function",
        "function": {"name": "bash", "parameters": {"type": "object", "properties": {}}, "description": "run"},
    }]


def test_a_commonly_refused_beta_explains_what_happens_next():
    """The bare name leaves an operator guessing whether the client recovered."""
    from lumen.services.anthropic_protocol import check_headers
    with pytest.raises(AnthropicError) as exc:
        check_headers({"anthropic-beta": "structured-outputs-2025-12-15"})
    assert "structured-outputs-2025-12-15" in str(exc.value)
    assert "retries without it" in str(exc.value)


def test_server_side_tools_are_refused():
    with pytest.raises(AnthropicError):
        translate_request(_req(tools=[{"type": "web_search_20250305", "name": "web_search"}]))


@pytest.mark.parametrize("given,expected", [
    ({"type": "auto"}, "auto"),
    ({"type": "any"}, "required"),
    ({"type": "none"}, "none"),
    ({"type": "tool", "name": "bash"}, {"type": "function", "function": {"name": "bash"}}),
])
def test_tool_choice(given, expected):
    _, _, _, kwargs, _ = translate_request(_req(
        tools=[{"name": "bash", "input_schema": {"type": "object"}}], tool_choice=given))
    assert kwargs["tool_choice"] == expected


def test_an_empty_tool_list_is_dropped():
    """Claude Code's WebFetch summarisation sends `tools: []`, which Anthropic
    accepts and vLLM rejects with a 400."""
    _, _, _, kwargs, _ = translate_request(_req(tools=[]))
    assert "tools" not in kwargs


def test_tool_choice_without_tools_is_dropped_but_still_validated():
    _, _, _, kwargs, _ = translate_request(_req(tools=[], tool_choice={"type": "auto"}))
    assert "tool_choice" not in kwargs
    assert "parallel_tool_calls" not in kwargs
    with pytest.raises(AnthropicError):
        translate_request(_req(tools=[], tool_choice={"type": "magic"}))


def test_unknown_tool_choice_is_refused():
    with pytest.raises(AnthropicError):
        translate_request(_req(tool_choice={"type": "magic"}))


def test_sampling_params():
    _, _, _, kwargs, extra = translate_request(
        _req(temperature=0.5, top_p=0.9, top_k=40, stop_sequences=["END"]))
    assert kwargs["temperature"] == 0.5
    assert kwargs["top_p"] == 0.9
    assert kwargs["stop"] == ["END"]
    # top_k has no OpenAI field, so it rides in extra_body
    assert extra == {"top_k": 40}


def test_stream_flag():
    assert translate_request(_req(stream=True))[2] is True


def test_stream_must_be_a_boolean():
    """A string "false" is truthy; coercing it would stream a request that asked
    not to."""
    with pytest.raises(AnthropicError):
        translate_request(_req(stream="false"))


def test_disable_parallel_tool_use_is_forwarded():
    _, _, _, kwargs, _ = translate_request(_req(
        tools=[{"name": "bash", "input_schema": {"type": "object"}}],
        tool_choice={"type": "auto", "disable_parallel_tool_use": True}))
    assert kwargs["parallel_tool_calls"] is False


def test_a_turn_that_translates_to_nothing_is_refused():
    """An assistant turn of pure thinking, or an empty user turn, would leave the
    upstream with no messages at all; the caller gets a clear 400 instead of an
    opaque backend error."""
    with pytest.raises(AnthropicError):
        translate_request(_req(messages=[{"role": "user", "content": []}]))
    with pytest.raises(AnthropicError):
        translate_request(_req(messages=[
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "s"}]}]))


def test_count_tokens_omits_max_tokens():
    """The count_tokens body has no max_tokens field at all."""
    body = _req()
    body.pop("max_tokens")
    _, _, _, kwargs, _ = translate_request(body, require_max_tokens=False)
    assert "max_tokens" not in kwargs


def test_mcp_servers_are_refused():
    with pytest.raises(AnthropicError):
        translate_request(_req(mcp_servers=[{"type": "url", "url": "https://x", "name": "n"}]))


def test_effort_service_tier_and_metadata_are_accepted_and_dropped():
    _, messages, _, kwargs, _ = translate_request(_req(
        thinking={"type": "disabled"}, context_management={"edits": []},
        output_config={"effort": "high"}, service_tier="auto",
        metadata={"user_id": "someone"},
    ))
    assert messages == [{"role": "user", "content": "hi"}]
    assert not {"reasoning_effort", "metadata", "thinking"} & kwargs.keys()


def test_client_cannot_inject_extra_body():
    """Only translated fields reach upstream — no passthrough of raw body keys."""
    _, _, _, kwargs, extra = translate_request(_req(
        extra_body={"cache_salt": "someone-elses"}, vllm_xargs={"x": 1}, stream_options={}))
    assert "extra_body" not in kwargs
    assert extra == {}


def test_non_object_body_is_refused():
    with pytest.raises(AnthropicError):
        translate_request(["not", "a", "dict"])


# ---------------------------------------------------------------------------
# Response translation
# ---------------------------------------------------------------------------

def _completion(**choice):
    base = {"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
    base.update(choice)
    return {"id": "chatcmpl-1", "choices": [base],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}}


def test_response_shape():
    out = translate_response(_completion(), "public-model")
    assert out["id"].startswith("msg_")
    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["model"] == "public-model"
    assert out["content"] == [{"type": "text", "text": "hello"}]
    assert out["stop_reason"] == "end_turn"
    assert out["stop_sequence"] is None
    assert out["usage"] == {"input_tokens": 7, "output_tokens": 3}


def test_response_tool_calls_become_tool_use():
    out = translate_response(_completion(
        message={"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "bash", "arguments": '{"cmd":"ls"}'}},
        ]},
        finish_reason="tool_calls",
    ), "m")
    assert out["content"] == [{"type": "tool_use", "id": "call_1", "name": "bash", "input": {"cmd": "ls"}}]
    assert out["stop_reason"] == "tool_use"


def test_malformed_tool_arguments_do_not_crash():
    out = translate_response(_completion(
        message={"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "b", "arguments": "{oops"}},
        ]},
        finish_reason="tool_calls",
    ), "m")
    assert out["content"][0]["input"] == {"__raw_arguments": "{oops"}


def test_response_reasoning_becomes_a_thinking_block():
    out = translate_response(_completion(
        message={"role": "assistant", "content": "answer", "reasoning_content": "thought"}), "m")
    assert out["content"] == [
        {"type": "thinking", "thinking": "thought", "signature": ""},
        {"type": "text", "text": "answer"},
    ]


def test_missing_usage_is_zero():
    payload = _completion()
    payload.pop("usage")
    assert translate_response(payload, "m")["usage"] == {"input_tokens": 0, "output_tokens": 0}


@pytest.mark.parametrize("finish,expected", [
    ("stop", "end_turn"),
    ("length", "max_tokens"),
    ("tool_calls", "tool_use"),
    ("content_filter", "refusal"),
    ("something_new", "end_turn"),
])
def test_stop_reason_mapping(finish, expected):
    assert stop_reason(finish)[0] == expected


def test_stop_sequence_is_reported_when_the_backend_names_it():
    assert stop_reason("stop", stop_sequence="END") == ("stop_sequence", "END")


def test_stop_reason_prefers_tool_use_when_the_turn_ended_in_a_tool_call():
    assert stop_reason("stop", has_tool_use=True)[0] == "tool_use"


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def _chunk(finish=None, **delta):
    return {"id": "chatcmpl-1", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _tool(index=0, **function):
    return _chunk(tool_calls=[{"index": index, **{k: v for k, v in function.items() if k == "id"},
                               "function": {k: v for k, v in function.items() if k != "id"}}])


def _run(*payloads):
    """Feed chunks through one translator and return the finished event list."""
    translator = StreamTranslator("m")
    raw = [event for payload in payloads for event in translator.chunk(payload)]
    return _events(raw + list(translator.finish()))


def test_stream_event_order_for_text():
    events = _run(
        _chunk(content="he"),
        _chunk(content="llo"),
        _chunk(finish="stop"),
        {"id": "chatcmpl-1", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    )

    assert [name for name, _ in events] == [
        "message_start", "content_block_start", "content_block_delta", "content_block_delta",
        "content_block_stop", "message_delta", "message_stop",
    ]
    assert events[0][1]["message"]["model"] == "m"
    assert [e[1]["delta"]["text"] for e in events[2:4]] == ["he", "llo"]
    assert events[5][1]["delta"] == {"stop_reason": "end_turn", "stop_sequence": None}
    assert events[5][1]["usage"] == {"input_tokens": 5, "output_tokens": 2}


def test_stream_tool_use_blocks():
    events = _run(
        _chunk(content="thinking out loud"),
        _tool(id="call_1", name="bash", arguments='{"cmd"'),
        _tool(arguments=':"ls"}'),
        _chunk(finish="tool_calls"),
    )

    assert [n for n, _ in events] == [
        "message_start", "content_block_start", "content_block_delta",
        "content_block_stop", "content_block_start", "content_block_delta",
        "content_block_stop", "message_delta", "message_stop",
    ]
    assert events[4][1]["content_block"] == {"type": "tool_use", "id": "call_1", "name": "bash", "input": {}}
    partial = "".join(e[1]["delta"]["partial_json"] for e in events[5:6])
    assert json.loads(partial) == {"cmd": "ls"}
    assert events[7][1]["delta"]["stop_reason"] == "tool_use"


def test_two_parallel_tool_calls_get_their_own_blocks():
    events = _run(_tool(id="a", name="one", arguments="{}"),
                  _tool(index=1, id="b", name="two", arguments="{}"))
    starts = [d for n, d in events if n == "content_block_start"]
    assert [s["index"] for s in starts] == [0, 1]
    assert [s["content_block"]["name"] for s in starts] == ["one", "two"]


def test_sequential_tool_calls_reusing_index_zero_get_their_own_blocks():
    """Several tool parsers number sequential calls 0 and 0. Keying on position
    alone merged them, so the second call's name and id were never sent and its
    arguments landed in the first call's input."""
    events = _run(_tool(id="a", name="one", arguments='{"x":1}'),
                  _tool(id="b", name="two", arguments='{"y":2}'))
    starts = [d for n, d in events if n == "content_block_start"]
    assert [s["content_block"]["id"] for s in starts] == ["a", "b"]
    assert [s["content_block"]["name"] for s in starts] == ["one", "two"]
    deltas = [d for n, d in events if n == "content_block_delta"]
    assert [d["index"] for d in deltas] == [0, 1]


def test_tool_argument_deltas_carry_no_id_and_continue_the_open_call():
    events = _run(_tool(id="a", name="one", arguments="{"), _tool(arguments='"x":1}'))
    assert len([d for n, d in events if n == "content_block_start"]) == 1
    partial = "".join(d["delta"]["partial_json"] for n, d in events if n == "content_block_delta")
    assert json.loads(partial) == {"x": 1}


def test_no_delta_is_ever_sent_for_a_closed_block():
    """Interleaving text must not split one tool's JSON across content blocks."""
    events = _run(_tool(id="a", name="one", arguments="{"),
                  _chunk(content="interrupting text"),
                  _tool(arguments='"x":1}'))

    open_block, closed = None, set()
    for name, data in events:
        if name == "content_block_start":
            open_block = data["index"]
        elif name == "content_block_stop":
            closed.add(data["index"])
            open_block = None
        elif name == "content_block_delta":
            assert data["index"] == open_block, f"delta for block {data['index']}, which is not open"
            assert data["index"] not in closed


def test_reasoning_content_becomes_a_thinking_block():
    """Reasoning models put the thinking phase in reasoning_content beside the
    answer, and it is billed either way — dropping it charges for text the
    caller never sees."""
    events = _run(_chunk(reasoning_content="let me see"), _chunk(content="the answer"))
    starts = [d["content_block"]["type"] for n, d in events if n == "content_block_start"]
    assert starts == ["thinking", "text"]
    kinds = [d["delta"]["type"] for n, d in events if n == "content_block_delta"]
    assert kinds == ["thinking_delta", "signature_delta", "text_delta"]


def test_stream_with_no_chunks_still_emits_a_complete_message():
    """An upstream that yields nothing must not produce a truncated event stream."""
    events = _events(list(StreamTranslator("m").finish()))
    assert [n for n, _ in events] == ["message_start", "message_delta", "message_stop"]


def test_stream_error_event():
    events = _events(list(StreamTranslator("m").error("boom", "api_error")))
    assert events == [("error", {"type": "error", "error": {"type": "api_error", "message": "boom"}})]


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

def test_count_tokens_estimates_from_characters():
    assert count_tokens([{"role": "user", "content": "x" * 40}]) == 10


def test_count_tokens_includes_the_tool_catalogue():
    """An agentic client sends its whole tool catalogue on every request, and for
    Claude Code it outweighs the conversation."""
    messages = [{"role": "user", "content": "hi"}]
    tools = [{"type": "function", "function": {"name": "Bash", "description": "d" * 400,
                                               "parameters": {"type": "object"}}}]
    assert count_tokens(messages, tools) > count_tokens(messages) + 50


# ---------------------------------------------------------------------------
# Contract: a real Claude Code request, captured from claude-cli 2.1.280
# ---------------------------------------------------------------------------

def test_a_real_claude_code_request_translates(claude_code_request):
    check_headers({k.lower(): v for k, v in claude_code_request["headers"].items()})
    model, messages, stream, kwargs, _ = translate_request(claude_code_request["body"])
    assert model and stream is True
    assert messages[0]["role"] == "system"
    assert kwargs["tools"] and all(t["type"] == "function" for t in kwargs["tools"])
    assert kwargs["max_tokens"] > 0


def test_tool_error_status_survives_translation():
    _, messages, _, _, _ = translate_request(_req(messages=[{
        "role": "user", "content": [{"type": "tool_result", "tool_use_id": "t",
                                      "content": "permission denied", "is_error": True}],
    }]))
    assert messages[0]["content"] == "[Tool error] permission denied"


def test_thinking_only_stream_closes_with_a_signature():
    events = _run(_chunk(reasoning_content="thought"), _chunk(finish="length"))
    assert [data["delta"]["type"] for name, data in events if name == "content_block_delta"] == [
        "thinking_delta", "signature_delta",
    ]
    assert events[-2][1]["delta"]["stop_reason"] == "max_tokens"


def test_stream_error_keeps_a_caller_error_type():
    """A backend 4xx (context too long, say) must not look like a server fault."""
    [caller] = _events(StreamTranslator("m").error("prompt is too long", "invalid_request_error"))
    [server] = _events(StreamTranslator("m").error("upstream is down", "api_error"))
    assert caller[1]["error"]["type"] == "invalid_request_error"
    assert server[1]["error"]["type"] == "api_error"


def test_streamed_malformed_tool_arguments_are_wrapped():
    """Same as the non-streaming path: the SDKs raise on an invalid input_json_delta."""
    events = _run(_tool(id="t1", name="Bash", arguments="{not json"), _chunk(finish="tool_calls"))
    [delta] = [data["delta"] for name, data in events
               if name == "content_block_delta" and data["delta"]["type"] == "input_json_delta"]
    assert json.loads(delta["partial_json"]) == {"__raw_arguments": "{not json"}


def test_system_turn_after_structured_user_content_is_appended_as_text():
    _, messages, _, _, _ = translate_request(_req(messages=[
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
        ]},
        {"role": "system", "content": "env"},
    ]))
    assert [m["role"] for m in messages] == ["user"]
    assert messages[0]["content"][-1] == {"type": "text", "text": "<system-reminder>\nenv\n</system-reminder>"}
