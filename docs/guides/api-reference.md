# API Reference

> **Developer note:** This guide is for people writing code or integrating with Lumen programmatically. If you just want to use the chat interface, you don't need this page.

Lumen exposes an **OpenAI-compatible REST API** at `/v1/`. Any tool or library that works with OpenAI can be pointed at your Lumen instance with minimal changes.

## Base URL and Authentication

Replace `https://your-lumen-instance` with your institution's Lumen URL. All requests require an `Authorization` header:

```
Authorization: Bearer sk_your_api_key_here
```

See [Usage → API Keys](../guides/usage.md#api-keys) to create a key.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/models` | List available models |
| `POST` | `/v1/chat/completions` | Send a chat message and receive a reply |

---

## List Models

```bash
curl https://your-lumen-instance/v1/models \
  -H "Authorization: Bearer sk_your_api_key_here"
```

Returns a list of model IDs you can use in chat completion requests.

---

## Chat Completions

### Basic request (curl)

```bash
curl https://your-lumen-instance/v1/chat/completions \
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
    base_url="https://your-lumen-instance/v1"
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
    base_url="https://your-lumen-instance/v1"
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
    base_url="https://your-lumen-instance/v1"
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
  baseURL: "https://your-lumen-instance/v1",
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
  baseURL: "https://your-lumen-instance/v1",
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
    base_url="https://your-lumen-instance/v1"
)
```

Everything else — model names, message format, streaming, tool calls — works identically as long as the model you request is available in your Lumen instance.

---

## Using Lumen in Third-Party Chat Tools

Many desktop and web chat applications support custom OpenAI-compatible endpoints. Look for a setting labelled **API Base URL**, **Custom endpoint**, or **OpenAI-compatible server** and enter:

```
https://your-lumen-instance/v1
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

Wait a moment and retry. The Usage page shows your recent request volume so you can gauge how close you are to the limit.

---

## Error Responses

| HTTP Status | Meaning |
|-------------|---------|
| `401` | Invalid or missing API key |
| `403` | Your account does not have access to the requested model |
| `404` | Model not found |
| `429` | Rate limit exceeded |
| `503` | Model backend is currently unavailable |
