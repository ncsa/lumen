# API Reference

> **Developer note:** This guide is for people writing code or integrating with Lumen programmatically. If you just want to use the chat interface, you don't need this page.

Lumen exposes an **OpenAI-compatible REST API** at `/v1/`. Any tool or library that works with OpenAI can be pointed at your Lumen instance with minimal changes.

## Base URL and Authentication

Replace `https://lumen.example.com` with your institution's Lumen URL. All requests require an `Authorization` header:

```
Authorization: Bearer sk_your_api_key_here
```

See [Profile → API Keys](../guides/profile.md#api-keys) to create a key.

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
  "output_tokens": 600,
  "total_tokens": 3000,
  "audio_seconds": 0,
  "cost": 0.012345,
  "coins_available": 83.25,
  "last_used_at": "2026-09-28T14:00:00Z"
}
```

`last_used_at` is `null` for a key that has never been used. `coins_available` is the coin balance
of the account that owns the key, so it is shared with that account's other keys and browser chat;
`-2` means the account has an unlimited coin pool and `null` that no pool is configured or that
the account is disabled. This endpoint does not add to the request count. It does not include
usage from other keys, browser chat, or earlier periods before the key was created.

---

## List Models

```bash
curl https://lumen.example.com/v1/models \
  -H "Authorization: Bearer sk_your_api_key_here"
```

Returns a list of model IDs you can use in chat completion requests.
Aliases of renamed models are listed as additional IDs whose `parent` field names the canonical model; standalone models have `parent: null`.

Each model may carry consent-state fields for the calling account:

| Field | Meaning |
|-------|---------|
| `tags` | The acknowledgment requirements still open for your account: `needs_ack`, `early_access`, both, or `[]`. A non-empty `tags` means you cannot use the model until you acknowledge it (see below). |
| `acknowledged_at` | Timestamp of your most recent acknowledgment, `null` while any requirement is still open. |
| `notice` | The model's acknowledgment / early-access notice text, present only while a requirement is open so you can read it before acknowledging. |

Models you are blocked from (owned by someone else without a grant, disabled, or expired) are omitted entirely.

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

`<id>` may be an alias or the canonical name. Consent is recorded per account (shared across all of its API keys, matching the web UI), and one call satisfies every currently-open requirement — `needs_ack` and/or `early_access`. The endpoint is idempotent: repeating it returns the same `200` shape, so callers can safely retry after a timeout. Acking a blocked, disabled, expired, deleted, or unknown model returns `404`.

Response (`200`):

```json
{
  "id": "qwen-coder-cc",
  "tags": [],
  "acknowledged_at": "2026-10-05T16:00:00Z",
  "notice": "This model was trained in a US government designated Country of Concern. It may exhibit biases based on differences in geopolitical environments, cultural norms, policy constraints, or censorship behaviors."
}
```

- `tags` is `[]` after a successful acknowledgment. If the model later gains a new requirement (for example it becomes early access), `tags` reports it and you must acknowledge once more.
- `notice` echoes the text you acknowledged; a model with no requirements returns `tags: []` and no `notice` as a no-op.
- If your account cannot use the model, an un-acknowledged consume call above returns `403` with `code: "consent_required"` and a message pointing at this endpoint.
- If the server disables API model consent (`api.consent: false` in `config.yaml`), no requirement is enforced over the API: `tags` is always `[]` and `acknowledge` records nothing.

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

Lumen proxies OpenAI-compatible **speech-to-text** endpoints for backends that support them (e.g. Qwen3-ASR). `transcriptions` returns text in the spoken language; `translations` returns English text. Both accept a `multipart/form-data` upload, not JSON.

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

Wait a moment and retry. The Usage page shows your recent request volume so you can gauge how close you are to the limit. Listing models (`GET /v1/models` and `GET /v1/models/{id}`) does not count against the limit, so a client that lists models before every completion is not penalised for it.

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

## Error Responses

| HTTP Status | Meaning |
|-------------|---------|
| `401` | Invalid or missing API key |
| `403` | Your account does not have access to the requested model |
| `404` | Model not found |
| `429` | Rate limit exceeded, or coin budget exhausted (see Rate Limits) |
| `503` | Model backend is currently unavailable |

A `403` on a consume endpoint either means the model is blocked for your account
or — for a model that requires acknowledgment you have not given — that you must
acknowledge it first. The two need opposite reactions, so they are told apart by
`code`:

```json
{
  "error": {
    "message": "This model requires acknowledgment before use. Acknowledge it via POST /v1/models/qwen-coder-cc/acknowledge.",
    "type": "invalid_request_error",
    "code": "consent_required"
  }
}
```

A `code: "consent_required"` means the model merely awaits your acknowledgment
(see [Acknowledge a Model](#acknowledge-a-model)); a `403` without a `code` means
the model is blocked for your account. This is the same `code`-disambiguation
pattern used for `429`, where `insufficient_quota` (stop until the budget
refills) is distinguished from a plain rate limit.
