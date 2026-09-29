# Anthropic API

Lumen serves Anthropic's Messages API through translation: each request to `POST /v1/messages` is converted to the OpenAI format, sent through the same path as `/v1/chat/completions`, and the reply is converted back. The access rules, budgets and billing are the same as on the OpenAI endpoints. Anything in Anthropic's API that has no OpenAI counterpart is unavailable (see [Refused](#refused)).

The base URL has **no** `/v1` suffix; Anthropic clients add it themselves. Put the key in `x-api-key` (`Authorization: Bearer` also works). Model names come from `GET /v1/models`.

```bash
curl https://lumen.example.com/v1/messages \
  -H "x-api-key: sk_your_api_key_here" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model": "qwen3-coder", "max_tokens": 512,
       "messages": [{"role": "user", "content": "Hello"}]}'
```

This endpoint fits best when your own code calls the Anthropic SDK, because you control exactly what is sent. Claude Code also works, and so does the Claude Agent SDK, which runs Claude Code underneath, but both are built for Anthropic's service, and some of their features depend on it. What works, what does not, and the settings you need are below.

## What works through Lumen and what won't

What runs on your machine works; what needs Anthropic's servers does not.

The local tools (Read, Write, Edit, Bash, Glob, Grep, TodoWrite, subagents) work, and so does WebFetch: your machine fetches the page and your Lumen model summarises it. All permission modes work, though `auto` can fail when the model is slow (see [Auto mode may fail](#auto-mode-may-fail)).

What won't work:

- **WebSearch.** The search runs inside Anthropic's service. Claude Code carries on without it and may answer from memory, so treat those answers as unverified. To get search, add a search MCP server.
- **Extended thinking and prompt caching.** With the settings below, Claude Code never asks for either, so they are simply absent. A client that does ask for thinking gets a 400 (see [Refused](#refused)).
- **Features tied to an Anthropic account.** For example, `/cost` prices the session against Anthropic's model table, so its figures are meaningless here; Lumen's usage page is the real accounting. claude.ai connectors are turned off, because Claude Code disables them whenever an API key is set.

## Claude Code

A typical setup:

```bash
export ANTHROPIC_BASE_URL=https://lumen.example.com
export ANTHROPIC_API_KEY=sk_your_api_key_here
export ANTHROPIC_MODEL=qwen3-coder
export ANTHROPIC_DEFAULT_HAIKU_MODEL=qwen3-coder
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
export CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1
export MAX_THINKING_TOKENS=0
export CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1
claude
```

The `CLAUDE_CODE_*` and `MAX_THINKING_TOKENS` lines are required. Without them, Claude Code asks for thinking and beta features that Lumen refuses with a 400, retries a few times, and then fails. The Claude Agent SDK needs the same settings. Pick a model with reliable tool calling and a large context. Verified with Claude Code 2.1.284; pin the version you roll out, because a newer version may ask for something Lumen refuses.

## Auto mode may fail

Auto mode asks the model, through Lumen, to approve each action that your permission rules do not already allow, and each approval request carries the whole conversation. If the model takes too long to read a long conversation, the approval times out: Claude Code reports that the classifier is "temporarily unavailable (timed out)" and skips the action. In Lumen's log the approval requests look like ordinary successful requests, because the time runs out on Claude Code's side. Use a faster model, or switch to the `default` or `acceptEdits` mode.

## When requests fail

| What you see | Usual cause |
|--------------|-------------|
| Claude Code fails after several retries with a 400 that names an `anthropic-beta` | The `CLAUDE_CODE_*` / `MAX_THINKING_TOKENS` settings above are missing, or Claude Code was upgraded and now asks for a beta Lumen refuses |
| 404 on every request | `ANTHROPIC_BASE_URL` ends in `/v1` |
| 404 `not_found_error` for the model | Not a Lumen model id (`count_tokens` also answers 404 for a model your key cannot use) |
| 403 `permission_error` | The model needs its terms accepted in Lumen, or your account has no access |
| 429 `rate_limit_error` | Rate limit or exhausted budget; `Retry-After` says when to try again |
| 503 `overloaded_error` | No healthy backend for the model right now |

Lumen's server log records every request it rejects with a 400 as `anthropic request refused (<type>): <reason>`.

## Supported

- `model`, `messages` and `max_tokens` (all three required; `count_tokens` does not need `max_tokens`), plus the optional `system`, `stream`, `temperature`, `top_p`, `top_k`, `stop_sequences`, `tools` and `tool_choice`.
- Content blocks: `text`, `image`, `tool_use`, `tool_result`. A reasoning model's thinking comes back as a `thinking` block.
- `POST /v1/messages/count_tokens` returns an estimate that assumes about four characters per token, and is never billed.

## Refused

Requests that use any of the following get a 400 rather than being quietly dropped: extended thinking controls, context management, `output_config` fields other than `effort`, service tiers other than `auto`, server-side tools (web search, code execution, computer use), `mcp_servers`, `container`, unknown `anthropic-beta` values, and any other content block type. `output_config.effort` and `cache_control` are accepted and ignored.

## Error format

Errors use Anthropic's envelope, `{"type": "error", "error": {"type": ..., "message": ...}}`. Each type arrives with its usual status (the ones in the table above, plus `400 invalid_request_error`, `401 authentication_error` and `500 api_error`). A failure after streaming has started arrives as an SSE `error` event.
