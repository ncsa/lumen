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
        "MODEL": { "name": "MODEL via Lumen" }
      }
    }
  }
}
```

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
