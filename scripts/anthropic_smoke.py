#!/usr/bin/env python
"""End-to-end checks for the Anthropic Messages API against a running Lumen.

Unit and route tests cover the translation with a faked upstream; this drives the
real thing over HTTP with the clients people actually use, which is the only way
to catch what a client sends that no hand-written request does.

    # terminal 1
    uv run dummy
    # terminal 2
    uv run python scripts/anthropic_toolbot.py
    # terminal 3
    CONFIG_YAML=./dev.config.yaml uv run lumen
    # terminal 4
    LUMEN_API_KEY=sk_... uv run --with claude-agent-sdk python scripts/anthropic_smoke.py

Checks that need a client that is not installed are skipped, not failed. Exits
non-zero if any check fails.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import anthropic

BASE = os.environ.get("BASE_URL", "http://localhost:5001").rstrip("/")
KEY = os.environ.get("LUMEN_API_KEY")
MODEL = os.environ.get("MODEL", "dummy")
TOOL_MODEL = os.environ.get("TOOL_MODEL", "dummy-tools")

results = []


def check(name, fn):
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001 - every failure is a result, not a crash
        results.append(("FAIL", name, f"{type(exc).__name__}: {exc}"))
    else:
        results.append(("SKIP" if detail is None else "PASS", name, detail or "not installed"))


def client(**kwargs):
    return anthropic.Anthropic(base_url=BASE, api_key=KEY, **kwargs)


# ---------------------------------------------------------------------------
# Official Anthropic SDK
# ---------------------------------------------------------------------------

def _reply_text(message):
    # A reasoning model puts a thinking block first; the answer is the text blocks.
    return "".join(b.text for b in message.content if b.type == "text")


def sdk_non_streaming():
    message = client().messages.create(
        model=MODEL, max_tokens=2048, system="Be brief.",
        messages=[{"role": "user", "content": "hello"}])
    assert message.type == "message" and message.role == "assistant"
    assert _reply_text(message), "empty reply"
    assert message.stop_reason == "end_turn"
    assert message.usage.input_tokens > 0 and message.usage.output_tokens > 0
    return f"{message.usage.input_tokens} in / {message.usage.output_tokens} out"


def sdk_streaming():
    with client().messages.stream(
            model=MODEL, max_tokens=2048,
            messages=[{"role": "user", "content": "hello"}]) as stream:
        text = stream.get_final_text()
        final = stream.get_final_message()
    assert text, "empty stream"
    assert final.stop_reason == "end_turn"
    assert final.usage.output_tokens > 0
    return f"{len(text)} chars, {final.usage.output_tokens} output tokens"


def sdk_count_tokens():
    count = client().messages.count_tokens(
        model=MODEL, messages=[{"role": "user", "content": "hello world"}])
    assert count.input_tokens > 0
    return f"input_tokens={count.input_tokens} (estimate)"


def sdk_tool_round_trip():
    """tool_use out, tool_result back in, over two real requests."""
    tools = [{"name": "Bash", "description": "Run a shell command",
              "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
                               "required": ["command"]}}]
    messages = [{"role": "user", "content": "run echo hello-from-lumen"}]
    first = client().messages.create(model=TOOL_MODEL, max_tokens=2048,
                                     tools=tools, messages=messages)
    assert first.stop_reason == "tool_use", f"expected tool_use, got {first.stop_reason}"
    call = next(b for b in first.content if b.type == "tool_use")
    assert call.name == "Bash" and call.input.get("command"), f"bad tool input: {call.input}"

    messages += [
        {"role": "assistant", "content": [b.model_dump() for b in first.content]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call.id,
                                      "content": "hello-from-lumen"}]},
    ]
    second = client().messages.create(model=TOOL_MODEL, max_tokens=2048,
                                      tools=tools, messages=messages)
    assert "hello-from-lumen" in _reply_text(second), _reply_text(second)
    return f"called {call.name}({json.dumps(call.input)}), result fed back"


def sdk_tool_streaming():
    tools = [{"name": "Bash", "description": "Run a shell command",
              "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
                               "required": ["command"]}}]
    with client().messages.stream(model=TOOL_MODEL, max_tokens=2048, tools=tools,
                                  messages=[{"role": "user", "content": "run it"}]) as stream:
        final = stream.get_final_message()
    call = next(b for b in final.content if b.type == "tool_use")
    assert final.stop_reason == "tool_use"
    assert call.input.get("command"), "streamed tool input did not reassemble"
    return f"streamed tool_use {call.name}({json.dumps(call.input)})"


def sdk_errors():
    try:
        client(max_retries=0).messages.create(
            model="definitely-not-a-model", max_tokens=16,
            messages=[{"role": "user", "content": "hi"}])
    except anthropic.NotFoundError as exc:
        assert exc.body["error"]["type"] == "not_found_error", exc.body
    else:
        raise AssertionError("an unknown model did not raise")

    try:
        anthropic.Anthropic(base_url=BASE, api_key="not-a-key", max_retries=0).messages.create(
            model=MODEL, max_tokens=16, messages=[{"role": "user", "content": "hi"}])
    except anthropic.AuthenticationError as exc:
        assert exc.body["error"]["type"] == "authentication_error", exc.body
    else:
        raise AssertionError("a bad key did not raise")
    return "not_found_error and authentication_error surfaced"


# ---------------------------------------------------------------------------
# Claude Code (the CLI) and the Claude Agent SDK, which runs the same client
# ---------------------------------------------------------------------------

def _agent_env(model):
    return {
        **os.environ,
        "ANTHROPIC_BASE_URL": BASE,
        "ANTHROPIC_API_KEY": KEY,
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_SMALL_FAST_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "1",
        "MAX_THINKING_TOKENS": "0",
        # Lumen model names are not in Claude Code's catalog, so it cannot look
        # up their context window; without this it refuses to start.
        "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT": "1",
    }


def claude_code_text():
    if not shutil.which("claude"):
        return None
    with tempfile.TemporaryDirectory() as home:
        env = {**_agent_env(MODEL), "CLAUDE_CONFIG_DIR": home}
        out = subprocess.run(["claude", "-p", "say hi", "--model", MODEL],
                             env=env, capture_output=True, text=True, timeout=180,
                             stdin=subprocess.DEVNULL, cwd=home)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip(), "claude printed nothing"
    return f"{out.stdout.strip().splitlines()[0][:60]!r}"


def claude_code_tool_use():
    if not shutil.which("claude"):
        return None
    with tempfile.TemporaryDirectory() as home:
        env = {**_agent_env(TOOL_MODEL), "CLAUDE_CONFIG_DIR": home}
        out = subprocess.run(
            ["claude", "-p", "run the bash command: echo hello-from-lumen",
             "--model", TOOL_MODEL, "--allowedTools", "Bash", "--output-format", "json"],
            env=env, capture_output=True, text=True, timeout=180,
            stdin=subprocess.DEVNULL, cwd=home)
    assert out.returncode == 0, out.stderr[-500:]
    assert "hello-from-lumen" in out.stdout, out.stdout[-500:]
    return "Claude Code ran a tool through Lumen and read its result"


def agent_sdk():
    try:
        from claude_agent_sdk import ClaudeAgentOptions, query
    except ImportError:
        return None
    import anyio

    env = _agent_env(MODEL)
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
                "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT",
                "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
                "MAX_THINKING_TOKENS"):
        os.environ[key] = env[key]

    async def run():
        chunks = []
        async for message in query(prompt="say hi",
                                   options=ClaudeAgentOptions(model=MODEL, max_turns=1)):
            for block in getattr(message, "content", []) or []:
                text = getattr(block, "text", None)
                if text:
                    chunks.append(text)
        return "".join(chunks)

    text = anyio.run(run)
    assert text.strip(), "the agent produced no text"
    return f"{text.strip().splitlines()[0][:60]!r}"


CHECKS = [
    ("anthropic SDK: non-streaming", sdk_non_streaming),
    ("anthropic SDK: streaming", sdk_streaming),
    ("anthropic SDK: count_tokens", sdk_count_tokens),
    ("anthropic SDK: errors", sdk_errors),
    ("anthropic SDK: tool round trip", sdk_tool_round_trip),
    ("anthropic SDK: streamed tool call", sdk_tool_streaming),
    ("Claude Code: text reply", claude_code_text),
    ("Claude Code: tool use", claude_code_tool_use),
    ("Claude Agent SDK: text reply", agent_sdk),
]


def main():
    if not KEY:
        sys.exit("set LUMEN_API_KEY to a key from the Profile page")
    print(f"Lumen at {BASE}, models {MODEL!r} / {TOOL_MODEL!r}\n")
    for name, fn in CHECKS:
        check(name, fn)
        status, _, detail = results[-1]
        print(f"  {status:4}  {name}: {detail}")

    failed = [r for r in results if r[0] == "FAIL"]
    print(f"\n{len(results) - len(failed)} ok, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
