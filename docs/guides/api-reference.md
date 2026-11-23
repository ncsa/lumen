# API Reference

> **Developer note:** This guide is for people writing code or integrating with Lumen programmatically. If you just want to use the chat interface, you don't need this page.

Lumen exposes an **OpenAI-compatible REST API** at `/v1/`. Any tool or library that works with OpenAI can be pointed at your Lumen instance with minimal changes.

## Base URL and Authentication

Replace `https://lumen.example.com` with your institution's Lumen URL. All requests require an `Authorization` header (`/v1/messages` also accepts `x-api-key`; see [Anthropic Messages](anthropic.md)):

```
Authorization: Bearer sk_your_api_key_here
```

See [Profile → API Keys](../guides/profile.md#api-keys) to create a key.

### Key types and access

Admins can also create admin (`sk_admin_…`) and monitoring (`sk_monitor_…`) keys (see [Key Types](profile.md#key-types)). The key's type, not its prefix, decides what it may do:

| Action | Admin key | Normal key | Monitoring key | `api.monitoring.token` (deprecated) |
|--------|-----------|------------|----------------|--------------------------------------|
| Chat, completions, embeddings, audio, Anthropic `/v1/messages` | 403 | ✅ (billed) | 403 | 403 |
| `POST /v1/models/<id>/acknowledge` (acknowledging a model) | 403 | ✅ (not billed) | 403 | 403 |
| `GET /v1/usage` (own key usage) | 403 | ✅ | 403 | 403 |
| `GET /v1/models`, `GET /v1/models/<id>` | ✅ every active model | ✅ only models you can use | ✅ every active model | ✅ every active model |
| `?tokens=true` token metrics on the models endpoints | ✅ | ignored: request counts only, never token metrics | ✅ | ✅ |
| [Admin API](#admin-api) `/v1/organization/*` | ✅ | 403 | 403 | 403 |

Admin keys **cannot chat**: they are for monitoring and the [admin API](#admin-api). A normal key sees request counts on the models endpoints but never `?tokens=true` token metrics. Admin and monitoring requests are never billed. A missing or malformed `Authorization` header gets `400`. An unknown, revoked or expired key, or an admin or monitoring key whose owner is no longer an admin, gets `401`.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/models` | List available models |
| `GET` | `/v1/models/<id>` | Retrieve details for a single model |
| `POST` | `/v1/models/<id>/acknowledge` | Acknowledge a model that requires consent |
| `GET` | `/v1/usage` | Get cumulative usage for the bearer API key |
| `POST` | `/v1/chat/completions` | Send a chat message and receive a reply |
| `POST` | `/v1/completions` | Legacy text-completion endpoint (prefer `/v1/chat/completions`) |
| `POST` | `/v1/audio/transcriptions` | Transcribe audio to text (speech-to-text) |
| `POST` | `/v1/audio/translations` | Translate audio into English text |
| `POST` | `/v1/embeddings` | Turn text or token ids into embedding vectors |
| `POST` | `/v1/messages` | Anthropic Messages API, see [Anthropic API](anthropic.md) |
| `POST` | `/v1/messages/count_tokens` | Estimate the prompt tokens of a Messages request |
| `GET` | `/v1/organization/users` | Admin API: list users (admin key only) |
| `GET` | `/v1/organization/users/<user_id>` | Admin API: one user's details (admin key only) |

---

## API Key Usage

```bash
curl https://lumen.example.com/v1/usage \
  -H "Authorization: Bearer sk_your_api_key_here"
```

Returns cumulative usage for the **specific key** in the Authorization header, including requests,
input and output tokens, their total, audio seconds, coins spent (`cost`), coins still available
(`coins_available`), and the UTC time the key was last used:

```json
{
  "requests": 12,
  "input_tokens": 2400,
  "cached_input_tokens": 1800,
  "cache_write_tokens": 200,
  "output_tokens": 600,
  "total_tokens": 3000,
  "audio_seconds": 0,
  "cost": 0.012345,
  "coins_available": 83.25,
  "last_used_at": "2026-09-28T14:00:00Z"
}
```

`cached_input_tokens` and `cache_write_tokens` are the parts of `input_tokens` that the upstream
model reported as read from and written to its prompt cache; they are not extra tokens, so
`total_tokens` is still `input_tokens + output_tokens`. A request whose upstream reported no such
count adds 0 to them.

`last_used_at` is `null` for a key that has never been used. `coins_available` is the coin balance
of the account that owns the key, so it is shared with that account's other keys and browser chat;
`-2` means the account has an unlimited coin pool and `null` that the account has no coin pool
(none configured for it, its groups or the global default) or a blocked one (Max Coins `0`). This endpoint does not add to the request count. It does not include
usage from other keys, browser chat, or earlier periods before the key was created.

---

## List Models

```bash
curl https://lumen.example.com/v1/models \
  -H "Authorization: Bearer sk_your_api_key_here"
```

Returns a list of model IDs you can use in chat completion requests.
Aliases of renamed models are listed as additional IDs whose `parent` field names the canonical model; standalone models have `parent: null`.

Each model carries its prices in coins per million tokens:

| Field | Meaning |
|-------|---------|
| `input_cost_per_million` | Price of input tokens. |
| `cached_input_cost_per_million` | Price of input tokens read from the prompt cache. `null` means they are billed at the input rate. |
| `cache_write_cost_per_million` | Price of input tokens written to the prompt cache. `null` means they are free (0). |
| `output_cost_per_million` | Price of output tokens. |

Each model may carry consent-state fields for the calling account:

| Field | Meaning |
|-------|---------|
| `tags` | The acknowledgment requirements still open for your account: `needs_ack`, `early_access`, both, or `[]`. A non-empty `tags` means you cannot use the model until you acknowledge it (see below). |
| `acknowledged_at` | Timestamp of your most recent acknowledgment, `null` while any requirement is still open. |
| `notice` | The model's acknowledgment / early-access notice text, present only while a requirement is open so you can read it before acknowledging. |

Models you are blocked from (owned by someone else without a grant, disabled, or expired) are omitted entirely.

By default the list also omits models you cannot use until you acknowledge them (any `needs_ack` or `early_access` requirement still open), so OpenAI-compatible clients only see models they can call. Every listed model then has `tags: []`.

Add `?include_unacknowledged=true` to also list those models, each with its open `tags` and its `notice`. The parameter also accepts `1` or `yes` (case-insensitive); any other value, including an empty one, keeps them hidden.

```bash
curl "https://lumen.example.com/v1/models?include_unacknowledged=true" \
  -H "Authorization: Bearer sk_your_api_key_here"
```

### Token counts (monitoring and admin keys only)

With a monitoring or admin key, or the deprecated [monitoring token](../admin/config-application.md#apimonitoring), `?tokens=true` (also `1` or `yes`) adds per-model token counts to `GET /v1/models` and `GET /v1/models/<id>`:

| Field | Meaning |
|-------|---------|
| `input_tokens` | Input tokens of the model's requests, per window. |
| `output_tokens` | Output tokens of the model's requests, per window. |
| `cache_metrics` | `requests` is the model's request count over the last day; `reads` and `writes` say whether the backend reported a cache-read or cache-write count on any of those requests. With no traffic both are `false`, so check `requests` first. |

`input_tokens` and `output_tokens` each hold `last_minute` (the last complete minute), `last_hour`, `last_day` and `last_month` (30 days). Alias rows repeat their model's counts. Like the `requests` counts, they may be up to about a minute old. A normal API key gets the normal response, with request counts only: the parameter is ignored. Without the parameter, every credential gets the normal response.

```bash
curl "https://lumen.example.com/v1/models?tokens=true" \
  -H "Authorization: Bearer $MONITORING_KEY"
```

---

## Retrieve a Model

```bash
curl https://lumen.example.com/v1/models/qwen-coder-cc \
  -H "Authorization: Bearer sk_your_api_key_here"
```

Returns the same metadata as the list, for one model. A model specified by alias returns its metadata under the requested alias with `parent` naming the canonical model.

`GET /v1/models/<id>` returns **404** for blocked, disabled, expired, deleted, or unknown models. A model that merely awaits your acknowledgment is returned so you can read its `notice` before acknowledging.

---

## Acknowledge a Model

Models that require a one-time acknowledgment (`needs_ack`) or early-access acknowledgment (`early_access`) block use until you consent. Over the API, acknowledge once per model:

```bash
curl -X POST https://lumen.example.com/v1/models/qwen-coder-cc/acknowledge \
  -H "Authorization: Bearer sk_your_api_key_here"
```

`<id>` may be an alias or the canonical name (including names containing slashes, such as `org/model`). Consent is recorded per account (shared across all of its API keys, matching the web UI), and one call satisfies every currently-open requirement — `needs_ack` and/or `early_access`. The endpoint is idempotent: repeating it returns the same `200` shape, so callers can safely retry after a timeout, even when two retries land at once. Acking a blocked, disabled, expired, deleted, or unknown model returns `404`.

Response (`200`):

```json
{
  "id": "qwen-coder-cc",
  "tags": [],
  "acknowledged_at": "2026-10-05T16:00:00Z"
}
```

- `tags` is `[]` after a successful acknowledgment. If the model later gains a new requirement (for example it becomes early access), `tags` reports it and you must acknowledge once more.
- `notice` is included only while `tags` is non-empty, matching `GET /v1/models/<id>`; read the notice there before acknowledging. A model with no requirements returns `tags: []` as a no-op.
- `GET /v1/models/<id>/acknowledge` returns `405` with `Allow: POST`. The `/acknowledge` suffix is reserved: config validation rejects a model name or alias ending in `/acknowledge`, so `POST /v1/models/<id>/acknowledge` always means this endpoint.
- If your account cannot use the model, an un-acknowledged consume call above returns `403` with `code: "consent_required"` and a message pointing at this endpoint.

---

## Chat Completions

### Basic request (curl)

```bash
curl https://lumen.example.com/v1/chat/completions \
  -H "Authorization: Bearer sk_your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [
      {"role": "user", "content": "Explain quantum entanglement in plain English"}
    ]
  }'
```

### Python (openai SDK)

Install the library once: `pip install openai`

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk_your_api_key_here",
    base_url="https://lumen.example.com/v1"
)

response = client.chat.completions.create(
    model="gpt-4o",
    messages=[
        {"role": "user", "content": "Explain quantum entanglement in plain English"}
    ]
)

print(response.choices[0].message.content)
```

### Python — multi-turn conversation

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk_your_api_key_here",
    base_url="https://lumen.example.com/v1"
)

history = []

def chat(user_message):
    history.append({"role": "user", "content": user_message})
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=history
    )
    reply = response.choices[0].message.content
    history.append({"role": "assistant", "content": reply})
    return reply

print(chat("What is a transformer model?"))
print(chat("How does the attention mechanism work?"))
```

### Python — streaming responses

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk_your_api_key_here",
    base_url="https://lumen.example.com/v1"
)

with client.chat.completions.stream(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Write a short poem about data science"}]
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
print()
```

### Python — system prompt

```python
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[
        {"role": "system", "content": "You are a helpful research assistant who always cites sources."},
        {"role": "user", "content": "Summarize recent advances in protein folding"}
    ]
)
print(response.choices[0].message.content)
```

### Node.js (openai SDK)

Install the library once: `npm install openai`

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: "sk_your_api_key_here",
  baseURL: "https://lumen.example.com/v1",
});

const response = await client.chat.completions.create({
  model: "gpt-4o",
  messages: [
    { role: "user", content: "Explain quantum entanglement in plain English" }
  ],
});

console.log(response.choices[0].message.content);
```

### Node.js — streaming

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: "sk_your_api_key_here",
  baseURL: "https://lumen.example.com/v1",
});

const stream = await client.chat.completions.stream({
  model: "gpt-4o",
  messages: [{ role: "user", content: "Write a haiku about machine learning" }],
});

for await (const chunk of stream) {
  const text = chunk.choices[0]?.delta?.content ?? "";
  process.stdout.write(text);
}
```

### Using Lumen as a drop-in replacement for OpenAI

If you have existing code that uses the OpenAI API, you can redirect it to Lumen by changing two values:

```python
# Before (standard OpenAI)
client = OpenAI(api_key="sk-...")

# After (Lumen)
client = OpenAI(
    api_key="sk_your_lumen_key",
    base_url="https://lumen.example.com/v1"
)
```

Everything else — model names, message format, streaming, tool calls — works identically as long as the model you request is available in your Lumen instance.

---

## Audio Transcriptions and Translations

Lumen proxies OpenAI-compatible **speech-to-text** endpoints for backends that support them (e.g. Qwen3-ASR). `transcriptions` returns text in the spoken language; `translations` returns English text. Both accept a `multipart/form-data` upload, not JSON. They accept only models whose `input_modalities` includes `"audio"`; embedding models and other models get a 400.

| Form field | Required | Applies to | Description |
|------------|----------|------------|-------------|
| `file` | yes | both | The audio file to transcribe/translate |
| `model` | yes | both | A Lumen model name backed by a speech-to-text endpoint |
| `language` | no | transcriptions | ISO-639-1 code of the input language (improves accuracy) |
| `prompt` | no | both | Optional text to guide the model's style or continue prior audio |
| `response_format` | no | both | `json` (default), `verbose_json`, `text`, `srt`, or `vtt` |
| `temperature` | no | both | Sampling temperature |

### curl

```bash
curl https://lumen.example.com/v1/audio/transcriptions \
  -H "Authorization: Bearer sk_your_api_key_here" \
  -F file=@speech.flac \
  -F model=qwen3-asr \
  -F response_format=verbose_json
```

### Python (openai SDK)

```python
from openai import OpenAI

client = OpenAI(base_url="https://lumen.example.com/v1", api_key="sk_your_api_key_here")

with open("speech.flac", "rb") as f:
    result = client.audio.transcriptions.create(model="qwen3-asr", file=f)
print(result.text)
```

### Billing

Speech-to-text models are billed **per hour of audio**. Lumen reads the upstream `usage` object:

- `{"type": "duration", "seconds": N}` → cost = `N / 3600 × audio_cost_per_hour` (configured per model).
- `{"type": "tokens", ...}` (e.g. gpt-4o-transcribe-style models) → billed via the usual per-token pricing.
- No usage reported → the request succeeds at zero cost and a warning is logged.

---

## Embeddings

Lumen proxies the OpenAI-compatible **embeddings** endpoint for embedding models (e.g. Qwen3-Embedding). The request is JSON:

| Field | Required | Description |
|-------|----------|-------------|
| `model` | yes | A Lumen model name (or alias) backed by an embedding endpoint |
| `input` | yes | A non-empty string, or a non-empty list of strings, of token ids, or of token-id lists |
| `encoding_format` | no | `float` (default) or `base64` |
| `dimensions` | no | Number of dimensions, for models that support shortening |
| `user` | no | An end-user identifier, passed to the backend |

Any other field is ignored. The response is the backend's `{"object": "list", "data": [...], "usage": {...}}` with `model` set to the name you requested.

### curl

```bash
curl https://lumen.example.com/v1/embeddings \
  -H "Authorization: Bearer sk_your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3-embedding-8b", "input": ["first sentence", "second sentence"]}'
```

### Python (openai SDK)

```python
from openai import OpenAI

client = OpenAI(base_url="https://lumen.example.com/v1", api_key="sk_your_api_key_here")

result = client.embeddings.create(model="qwen3-embedding-8b", input=["first sentence", "second sentence"])
print(len(result.data), len(result.data[0].embedding))
```

### Billing

Embeddings are billed **per input token** at the model's input price: cost = `prompt_tokens × input_cost_per_million / 1,000,000`. There are no output tokens, so the output price never applies. Requests show up in usage and `/v1/usage` as input tokens, and share the per-key rate limit with the other `/v1` model calls. If the backend reports no usage, the request succeeds at zero cost and a warning is logged.

---

## Using Lumen in Third-Party Chat Tools

Many desktop and web chat applications support custom OpenAI-compatible endpoints. Look for a setting labelled **API Base URL**, **Custom endpoint**, or **OpenAI-compatible server** and enter:

```
https://lumen.example.com/v1
```

Then paste your `sk_...` key as the API key. Common tools that support this pattern include Jan, Open WebUI, Msty, and most AI IDE extensions.

---

## Token Usage in Responses

Every response includes a `usage` field with exact token counts:

```json
{
  "choices": [...],
  "usage": {
    "prompt_tokens": 42,
    "completion_tokens": 183,
    "total_tokens": 225
  }
}
```

These counts drive the coin deduction on your account. You can retrieve the same numbers from the Usage page after the fact.

---

## Rate Limits

If you send too many requests too quickly, the API returns:

```
HTTP 429 Too Many Requests
```

Wait a moment and retry. The Usage page shows your recent request volume so you can gauge how close you are to the limit. The limit is per API key, and chat, completions, messages and audio share one budget: two chat requests and two Anthropic `/v1/messages` requests count as four against it. `POST /v1/messages/count_tokens` and `POST /v1/models/{id}/acknowledge` each have their own budget of the same size. Listing models (`GET /v1/models` and `GET /v1/models/{id}`) and `GET /v1/usage` are not counted, so a client that lists models before every completion is not penalised for it.

A `429` is also returned when your coin budget is exhausted, which needs the opposite
reaction — retrying shortly will not help until the budget refills. The two are told
apart by the error `code`:

| `code` | Meaning | What to do |
|--------|---------|------------|
| `rate_limit_exceeded` | Too many requests too quickly | Retry after a short pause |
| `insufficient_quota` | Your coin budget is spent | Wait for the refill, or ask for a larger budget |

Both carry a `Retry-After` header, in seconds, whenever the wait is knowable — the
OpenAI SDK honours it. A coin budget that never refills automatically sends no
`Retry-After`, because there is no time to give.

---

## CORS (browser apps)

Every `/v1` endpoint allows cross-origin requests, so a web page on any origin can call the API directly with `fetch`, the OpenAI JS SDK or the Anthropic TypeScript SDK (both with `dangerouslyAllowBrowser: true`). Each `/v1` response, including errors and streaming responses, carries:

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, POST, OPTIONS
Access-Control-Allow-Headers: Authorization, Content-Type, OpenAI-Organization, OpenAI-Project, OpenAI-Beta,
    X-Stainless-Lang, X-Stainless-Package-Version, X-Stainless-OS, X-Stainless-Arch, X-Stainless-Runtime,
    X-Stainless-Runtime-Version, X-Stainless-Retry-Count, X-Stainless-Timeout, X-Stainless-Helper-Method,
    x-api-key, anthropic-version, anthropic-beta, anthropic-dangerous-direct-browser-access
Access-Control-Expose-Headers: Retry-After
Access-Control-Max-Age: 600
```

(Beyond `Authorization` and `Content-Type`, these are the headers the OpenAI JS SDK and the Anthropic TypeScript SDK send; `/v1/messages` reads the key from `x-api-key`.) Preflight `OPTIONS` requests are answered `204` without an API key and do not count against your rate limit. Credentials are never allowed: the browser sends no cookies, and the page must attach the `Authorization: Bearer` (or `x-api-key`) header itself. Any script on that page can read the key, so treat a key held in the browser like any other secret and revoke it on your [Profile](../guides/profile.md#api-keys) if it leaks.

`Retry-After` is exposed so browser code can honour it on a `429` (see Rate Limits).

> **Reverse proxies:** if your Lumen sits behind an ingress or proxy, make sure it passes `OPTIONS` requests through to Lumen and does not add its own CORS headers. A proxy that answers preflights itself can drop the allowed headers, and one that adds a second `Access-Control-Allow-Origin` makes browsers reject every response.

---

## Error Responses

| HTTP Status | Meaning |
|-------------|---------|
| `400` | Missing or malformed `Authorization` header (not `Bearer <key>`, or an empty key), or an invalid request body |
| `401` | Unknown, revoked or expired API key, or an admin or monitoring key whose owner is no longer an admin |
| `403` | Your account does not have access to the requested model, or the key's type may not use the endpoint (see [Key types and access](#key-types-and-access)) |
| `404` | Model not found |
| `429` | Rate limit exceeded, or coin budget exhausted (see Rate Limits) |
| `503` | Model backend is currently unavailable |

A `403` on a consume endpoint either means the model is blocked for your account or — for a model that requires acknowledgment you have not given — that you must acknowledge it first. The two need opposite reactions, so they are told apart by `code`:

```json
{
  "error": {
    "message": "This model requires acknowledgment before use. Acknowledge it via POST /v1/models/qwen-coder-cc/acknowledge.",
    "type": "invalid_request_error",
    "code": "consent_required"
  }
}
```

A `code: "consent_required"` means the model merely awaits your acknowledgment (see [Acknowledge a Model](#acknowledge-a-model)); a `403` without a `code` means the model is blocked for your account. This is the same `code`-disambiguation pattern used for `429`, where `insufficient_quota` (stop until the budget refills) is distinguished from a plain rate limit.

---

## Admin API

A read-only, OpenAI-compatible admin API under `/v1/organization`. Only an **admin key** (`sk_admin_…`) may call it; admins create one on their profile page in admin mode (see [Key Types](profile.md#key-types)). Admin keys expire within a week, so scripts that call the admin API must rotate their key. A key stops working when its owner is removed from the `admins` list or deactivated.

Errors use the OpenAI shape `{"error": {"message", "type", "code"}}`:

| Status | When |
|--------|------|
| `400` | Missing or malformed `Authorization` header, or a bad list parameter (`param` names it) |
| `401` | Unknown, revoked or expired key, an admin key whose owner is no longer an admin or is inactive, or a non-ASCII token |
| `403` | A normal key, a monitoring key or `api.monitoring.token` (`permission_error`, "This endpoint requires an admin key") |
| `404` | No such user (`code: user_not_found`) |
| `429` | The per-key rate limit (`rate_limiting.limit`) is exceeded |

Requests are never billed and are not written to the request log.

### Ids

Users and groups are named with a prefixed uuid, the same uuid the admin pages show: `user-<uuid>` and `group-<uuid>` (lowercase, dashed). On [Retrieve a user](#retrieve-a-user), anything else, a project or an unknown uuid, gets `404`; on [List users](#list-users), a bad `after` or `group` gets `400`.

### List users

```bash
curl "https://lumen.example.com/v1/organization/users?limit=1&sort_by=last_active" \
  -H "Authorization: Bearer sk_admin_your_admin_key_here"
```

```json
{
  "object": "list",
  "data": [
    {
      "object": "organization.user",
      "id": "user-3f6c1d2e-8a4b-4c1e-9f0a-1b2c3d4e5f60",
      "name": "Ada Lovelace",
      "email": "ada@example.edu",
      "role": "reader",
      "added_at": 1732291200,
      "lumen": {
        "status": "active",
        "groups": [{"id": "group-0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d", "name": "students"}],
        "last_active_at": 1732291200
      }
    }
  ],
  "first_id": "user-3f6c1d2e-8a4b-4c1e-9f0a-1b2c3d4e5f60",
  "last_id": "user-3f6c1d2e-8a4b-4c1e-9f0a-1b2c3d4e5f60",
  "has_more": true,
  "total": 42
}
```

Each user is the same object as [Retrieve a user](#retrieve-a-user) returns, without `lumen.coins` and `lumen.api_key_count`. Only users are listed, never projects.

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `limit` | `20` | Users per page, `1` to `100` |
| `after` | | A user id, usually the previous page's `last_id`; the page starts after that user |
| `sort_by` | `created_at` | `created_at`, `last_active` or `email` |
| `sort_order` | `desc` | `asc` or `desc`. With `last_active`, users who were never active come last in both orders |
| `search` | | Part of the name or email, case-insensitive; `%` and `_` match themselves |
| `group` | | Only members of this group, given by its id (`group-<uuid>`); a group name gets `400` |
| `status` | | `active` or `inactive` |

To fetch every user, repeat the request with `after` set to `last_id` until `has_more` is `false`. `total` counts every user that matches the filters, whatever `after` is. An `after` that is not a user id gets `400`.

### Retrieve a user

```bash
curl https://lumen.example.com/v1/organization/users/user-3f6c1d2e-8a4b-4c1e-9f0a-1b2c3d4e5f60 \
  -H "Authorization: Bearer sk_admin_your_admin_key_here"
```

```json
{
  "object": "organization.user",
  "id": "user-3f6c1d2e-8a4b-4c1e-9f0a-1b2c3d4e5f60",
  "name": "Ada Lovelace",
  "email": "ada@example.edu",
  "role": "reader",
  "added_at": 1732291200,
  "lumen": {
    "status": "active",
    "groups": [{"id": "group-0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d", "name": "students"}],
    "last_active_at": 1732291200,
    "coins": {"balance": 12.5, "allocation": 100, "per_hour": 5, "next_reset_at": 1732294800},
    "api_key_count": 2
  }
}
```

| Field | Meaning |
|-------|---------|
| `role` | `owner` if the user is in the `admins` list, else `reader` |
| `added_at` | When the user first signed in (Unix seconds) |
| `lumen.status` | `active` or `inactive` |
| `lumen.groups` | Every group the user belongs to |
| `lumen.last_active_at` | Last request (Unix seconds), `null` if never |
| `lumen.coins` | The user's coin pool: current `balance`, `allocation` (`-2` means unlimited), coins added `per_hour`, and `next_reset_at` (Unix seconds, `null` if the pool does not refill). `null` when the user is blocked |
| `lumen.api_key_count` | The user's own usable normal API keys (admin, monitoring, revoked and project keys are not counted) |

Keys, usage, tokens and cost are never returned.
