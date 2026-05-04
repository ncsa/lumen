# API Reference

This section covers how Lumen API keys are structured, created, and used for programmatic access to AI models.

## How Keys Are Used

Lumen exposes an OpenAI-compatible REST API at `/v1/`. The two main endpoints are:

| Endpoint | Purpose |
|----------|---------|
| `GET /v1/models` | List available models |
| `POST /v1/chat/completions` | Send a chat completion request |

To authenticate:

```bash
curl https://your-lumen-instance/v1/chat/completions \
  -H "Authorization: Bearer sk_your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

The same format works with any OpenAI-compatible SDK (Python, Node.js, etc.):

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk_your_api_key_here",
    base_url="https://your-lumen-instance/v1"
)

response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Explain quantum computing"}]
)
print(response.choices[0].message.content)
```

## Key Structure

Every key follows the format `sk_<random>` and is composed of:

| Field | Description |
|-------|------------|
| **id** | Database primary key |
| **entity_id** | Tied to either a user (`Entity.entity_type = 'user'`) or a client (`Entity.entity_type = 'client'`) |
| **name** | Human-readable label you assign |
| **key_hint** | First 7 + last 4 characters (for display after creation) |
| **active** | Boolean — `false` means revoked (soft-delete) |
| **requests** | Total number of API requests made |
| **input_tokens / output_tokens** | Cumulative token counts |
| **cost** | Total coins (USD) billed to the key |
| **last_used_at** | Timestamp of the most recent request |

## Key Lifecycle

```
Generate ──> Display Once (must copy) ──> Save ──> Use ──> View / Revoke
```

1. **Generate** — `POST /usage/keys/generate` creates a random `sk_...` token via `secrets.token_urlsafe(32)`. For client keys, managers may supply their own key.
2. **Display** — The key is shown once on screen. After closing the modal without saving, it cannot be recovered.
3. **Save** — The plaintext key is **never stored**. Instead, it is hashed with HMAC-SHA256 (using the admin's `encryption_key` or `LUMEN_ENCRYPTION_KEY` env var) and the hash is saved. This means the plaintext can never be recovered from the database.
4. **Use** — Each API call validates the key hash, deducts coins, and updates usage counters.
5. **Revoke** — Setting `active = false` disables the key. Usage history is retained.

## Rate Limiting

All endpoints are rate-limited per authenticated entity (API key ID for `/v1/*` routes, session for `/chat/*`). The default limit is `30 per minute`, configurable in `config.yaml`:

```yaml
rate_limiting:
  limit: "30 per minute"
```

If you exceed the limit, the API returns a 429 error.
