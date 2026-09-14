# Connect Your Tools

Lumen exposes an **OpenAI-compatible API**, so most tools that speak to OpenAI can talk to Lumen by changing two things: the **base URL** and the **API key**.

> **Tip:** The [Connect page](/connect) generates these snippets for you — including a ready-to-download OpenCode config listing every model your account can use, and curl/Python examples for a specific model. Log in first so it can fill in your models.

## 1. Create an API key

Create a key on your [Profile](/profile) page. Copy it when it is shown — it is only displayed once.

## 2. Set the `LUMEN_API_KEY` environment variable

Tools read the key from the environment rather than the config file, so the key never has to be written to disk.

```
export LUMEN_API_KEY="sk_…"      # macOS / Linux
setx LUMEN_API_KEY "sk_…"        # Windows (applies to new terminals)
```

The base URL is your Lumen host with `/v1` appended, for example `https://lumen.example.com/v1`.

## Desktop chat clients

Beyond command-line and coding tools, most **graphical chat clients** that talk to OpenAI can be pointed at Lumen with the same two pieces of information: the **base URL** (`https://lumen.example.com/v1`) and your **API key**. This keeps your chats on your own computer — Lumen is designed not to store or leak your chats and their responses, so your conversation content is not retained. Only usage metadata (request counts, tokens, and cost) is recorded and shown on your [Usage page](/usage); see [Profile](./profile.md) for details on what Lumen retains.

When you configure a new provider in such a client, look for the "OpenAI compatible" (or "OpenAI") option and fill in:

| Field | Value |
|-------|-------|
| **API Base URL** | `https://lumen.example.com/v1` |
| **API Key** | The token from your [Profile](/profile) page |
| **Model** | Choose a model id from the [Model Dashboard](/models) (most clients offer a "Fetch models" button instead) |

The steps below walk through a concrete example using [ChatWise](https://chatwise.app/), but the same settings apply to any OpenAI-compatible desktop client (LM Studio, Msty, Jan, etc.).

1. **Install the client** and launch it.
2. Go to **Settings → Providers** and click the **+** (add provider) button, choosing **OpenAI Compatible**.
3. Fill in the fields:
   - **Provider Name** — anything you like, e.g. `Lumen`.
   - **API Base URL** — `https://lumen.example.com/v1`.
   - **API Key** — paste your [API key](/profile). If the field name is `Api Key`/`Secret Key`, that is the same thing.
4. Click **Fetch Models** (sometimes *List Models* or *Test Connection*). The client will pull the list of models Lumen hosts; pick one from the [Model Dashboard](/models).
5. Back in the client's main window, select a Lumen model and start chatting.

> **Model acknowledgement:** Some models ask you to accept their license once before first use. On the [Profile page](/profile), the **Models** tab lists models whose access badge reads **Needs consent** — click it to acknowledge, then the model works from any client. See [Model Access](./profile.md) for the full list of badges.

## OpenCode

[OpenCode](https://opencode.ai) reads the key from `{env:LUMEN_API_KEY}`. Use the **Download config.json** button on the [Connect page](/connect) to get a file pre-filled with every model you can access, then save it as:

- **macOS / Linux:** `~/.config/opencode/opencode.json`
- **Windows:** `%USERPROFILE%\.config\opencode\opencode.json`
- Or `opencode.json` in a project's root directory for per-project settings.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "lumen": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Lumen",
      "options": {
        "baseURL": "https://lumen.example.com/v1",
        "apiKey": "{env:LUMEN_API_KEY}"
      },
      "models": {
        "MODEL": {
          "name": "MODEL via Lumen",
          "limit": { "context": 131072, "output": 32768 },
          "cost": { "input": 5.0, "output": 15.0 }
        }
      }
    }
  }
}
```

Each model entry includes `limit` (the context window and max output, in tokens) and `cost` (USD per million input/output tokens), so OpenCode can size the context and track spending. The **Download config.json** button fills these in from the model's configured limits and pricing.

To have OpenCode only use Lumen's models — and ignore every other installed provider — add an `enabled_providers` array at the top level of your config:

```json
{
  "enabled_providers": [
    "lumen"
  ]
}
```

OpenCode will then list only Lumen's models when you select a model.

## curl

Replace `MODEL` with a model id from the [Model Dashboard](/models).

```
curl https://lumen.example.com/v1/chat/completions \
  -H "Authorization: Bearer $LUMEN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "MODEL", "messages": [{"role": "user", "content": "Hello!"}]}'
```

## Python

Use the official `openai` package pointed at the Lumen base URL.

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="https://lumen.example.com/v1",
    api_key=os.environ["LUMEN_API_KEY"],
)

resp = client.chat.completions.create(
    model="MODEL",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

## R

Use the [ellmer](https://ellmer.tidyverse.org) package with `chat_openai_compatible()` pointed at the Lumen base URL. The `credentials` function in the snippet below reads your key from the `LUMEN_API_KEY` environment variable.

```r
library(ellmer)

chat <- chat_openai_compatible(
  base_url = "https://lumen.example.com/v1",
  name = "Lumen",
  model = "MODEL",
  credentials = function() Sys.getenv("LUMEN_API_KEY")
)

chat$chat("Hello!")
```

Instead of exporting `LUMEN_API_KEY` in your shell, you can store it in your R environment file: run `usethis::edit_r_environ()`, add a line `LUMEN_API_KEY="sk_…"`, save, and restart R.

For image-capable models, pass an image alongside your prompt with `content_image_url()` (or `content_image_file()` for a local file):

```r
chat$chat(
  "What is in this image?",
  content_image_url("https://example.com/photo.jpg")
)
```

## Images and audio

Models that accept images take standard OpenAI `image_url` content blocks in a chat request.

Audio models accept audio as `input_audio` content in a chat request. Some speech models require an audio placeholder token in the text so the model knows where the audio belongs — for example IBM granite-speech uses `<|audio|>`, followed by your instruction. Check the model's card for the exact token.

Base64-encoded audio is too large to pass as an inline `-d` argument, so build the request body in a file and post it with `-d @file`:

```
# 1) Build the request body, embedding the base64-encoded audio
cat > chat.json << EOF
{
  "model": "MODEL",
  "messages": [{"role": "user", "content": [
    {"type": "text", "text": "<|audio|> can you transcribe the speech into a written format?"},
    {"type": "input_audio", "input_audio": {"data": "$(base64 < audio.mp3 | tr -d '\n')", "format": "mp3"}}
  ]}]
}
EOF

# 2) Send it
curl https://lumen.example.com/v1/chat/completions \
  -H "Authorization: Bearer $LUMEN_API_KEY" \
  -H "Content-Type: application/json" \
  -d @chat.json
```

Speech-to-text models can also be used through the transcription endpoint:

```
curl https://lumen.example.com/v1/audio/transcriptions \
  -H "Authorization: Bearer $LUMEN_API_KEY" \
  -F file=@audio.mp3 \
  -F model=MODEL
```

Select a specific image- or audio-capable model on the [Connect page](/connect) to see tailored examples.
