#!/usr/bin/env python
"""OpenAI-compatible backend that calls a tool once, for the Anthropic smoke test.

The echo backend (`uv run dummy`) never emits tool calls, so it cannot exercise
the tool_use/tool_result round trip that agentic clients live on. This one does
the smallest thing that can: the first time it sees a request carrying tools and
no tool result yet, it calls the first tool whose name it recognises; once a tool
result comes back, it answers with text quoting it.

    uv run python scripts/anthropic_toolbot.py      # serves :9998

Add it to the dev config as the `dummy-tools` model:

    - name: dummy-tools
      input_cost_per_million: 0
      output_cost_per_million: 0
      endpoints:
      - url: http://localhost:9998/v1
        api_key: dummy
        model: dummy-tools
"""
import json
import time

from flask import Flask, Response, jsonify, request

app = Flask(__name__)

# What to call each tool with. Anything else is answered with text, so a client
# whose tools we do not know still gets a reply instead of an invalid call.
TOOL_ARGUMENTS = {
    "Bash": {"command": "echo hello-from-lumen"},
    "bash": {"command": "echo hello-from-lumen"},
}


def _tool_to_call(data):
    """The (name, arguments) to call, or None to answer with text."""
    messages = data.get("messages") or []
    if any(m.get("role") == "tool" for m in messages):
        return None  # the result is in hand; answer instead of calling again
    for tool in data.get("tools") or []:
        name = (tool.get("function") or {}).get("name")
        if name in TOOL_ARGUMENTS:
            return name, TOOL_ARGUMENTS[name]
    return None


def _text_reply(data):
    messages = data.get("messages") or []
    results = [m.get("content") for m in messages if m.get("role") == "tool"]
    if results:
        return f"The tool said: {results[-1]}"
    # Skip the <system-reminder> turns Lumen makes of Claude Code's system turns.
    last = next((m.get("content") for m in reversed(messages) if m.get("role") == "user"
                 and not str(m.get("content")).startswith("<system-reminder>")), "")
    if isinstance(last, list):
        last = " ".join(part.get("text", "") for part in last if isinstance(part, dict))
    return f"toolbot echo: {last}"


@app.get("/v1/models")
def list_models():
    return jsonify({"object": "list",
                    "data": [{"id": "dummy-tools", "object": "model", "created": 0, "owned_by": "local"}]})


@app.post("/v1/chat/completions")
def chat_completions():
    data = request.json or {}
    call = _tool_to_call(data)
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    created, cid = int(time.time()), "chatcmpl-toolbot"

    if data.get("stream"):
        def generate():
            def chunk(delta, finish=None, with_usage=False):
                body = {"id": cid, "object": "chat.completion.chunk", "created": created,
                        "model": "dummy-tools",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                if with_usage:
                    body["usage"] = usage
                return f"data: {json.dumps(body)}\n\n"

            if call:
                name, arguments = call
                encoded = json.dumps(arguments)
                yield chunk({"tool_calls": [{"index": 0, "id": "call_toolbot", "type": "function",
                                             "function": {"name": name, "arguments": ""}}]})
                # Split so the client has to reassemble the argument JSON, the
                # way a real backend streams it.
                half = len(encoded) // 2
                for piece in (encoded[:half], encoded[half:]):
                    yield chunk({"tool_calls": [{"index": 0, "function": {"arguments": piece}}]})
                    time.sleep(0.02)
                yield chunk({}, finish="tool_calls")
            else:
                for word in _text_reply(data).split(" "):
                    yield chunk({"content": word + " "})
                    time.sleep(0.02)
                yield chunk({}, finish="stop")
            yield chunk({}, with_usage=True)
            yield "data: [DONE]\n\n"

        return Response(generate(), content_type="text/event-stream")

    if call:
        name, arguments = call
        message = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_toolbot", "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments)}}]}
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": _text_reply(data)}
        finish = "stop"

    return jsonify({"id": cid, "object": "chat.completion", "created": created,
                    "model": "dummy-tools",
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": usage})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=9998)
