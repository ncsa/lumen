# Configuring Models

Models are the core of Lumen. Each entry tells Lumen how to reach an AI model, what it costs, and what it can do.

## Basic Model Entry

Every model starts with a name and an `endpoints` list:

```yaml
models:
  - name: my-model
    access: allowed
    input_cost_per_million: 0.5
    output_cost_per_million: 1.0
    endpoints:
      - url: https://example.com/v1
        api_key: sk-your-key
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Lumen's internal identifier for the model. This is what appears in the chat UI and must be unique within your config. |
| `endpoints` | Yes | One or more back-end servers that provide this model |
| `access` | No (default: inherit) | The model's own default: `allowed` or `blocked`. Leave it **unset** to inherit each scope's `model_access.default` (then `defaults.models.access`). When set, it beats group/user *defaults* but is still overridden by an explicit per-scope `allowed`/`blocked` rule. |

## Access Control

Model access is **orthogonal**: three independent per-model fields control it, instead of a single status. Each axis answers a different question.

| Field | Default | Description |
|-------|---------|-------------|
| `access` | inherit | The model's own allow/block default. Leave unset to inherit scope defaults (`model_access.default`, then `defaults.models.access`). When set, it ranks above group/user *defaults* but below an explicit per-scope `allowed`/`blocked` rule — so a model can be blocked-by-default yet enabled for a specific group/user (see [User Groups](config-users.md) and [Clients](config-clients.md)). |
| `needs_ack` | `false` | When `true`, a user must acknowledge the model before using it. This is a **sticky, model-level property** — no group, client, or user scope can add or remove it. It only triggers the consent gate; it does not by itself grant or deny access. |
| `disabled` | `false` | **Hard off.** The model is hidden everywhere and cannot be used. This is **not overridable** by any scope — it always wins. Use it to take a model offline without deleting it. |
| `ack_message` | unset | Optional acknowledgement message shown when `needs_ack` is `true`. Overrides the global `defaults.models.ack_message`. |

```yaml
models:
  - name: my-model
    access: allowed       # baseline; overridable per group/client/user
    needs_ack: true       # require acknowledgement (model-level, sticky)
    ack_message: "This model was trained outside the U.S. — use with awareness."
    input_cost_per_million: 0.5
    output_cost_per_million: 1.0
    endpoints:
      - url: https://example.com/v1
        api_key: sk-your-key
```

### `disabled` is a hard off

Setting `disabled: true` short-circuits all access resolution to blocked — the model disappears from the chat UI, the API, and every scope's allow list. No group, client, or user override can bring it back. This replaces the old `active: false`. To **permanently** remove a model, delete its entry from `config.yaml` entirely.

> The legacy `active:` key is still accepted as input (with a deprecation warning): `active: false` maps to `disabled: true`. Prefer `disabled` in new configs.

### `needs_ack` lives on the model

Acknowledgement is a property of the model, not of any group or scope. There is no per-scope graylist anymore — set `needs_ack: true` on the model and every user who is allowed the model must acknowledge it once before using it.

## Pricing

Fields shown to users on the Models page:

| Field | Description | Default |
|-------|-------------|---------|
| `input_cost_per_million` | Coins charged per 1M input tokens | 0.0 |
| `output_cost_per_million` | Coins charged per 1M output tokens | 0.0 |

See the [Introduction](../introduction.md#tokens-and-coins) for how coin costs are calculated.

## Capabilities

These fields tell the UI what the model can do and help users pick the right one:

| Field | Description |
|-------|-------------|
| `description` | Short text shown next to the model name in the UI |
| `url` | Link to the model's documentation page (e.g. HuggingFace) |
| `context_window` | Maximum total tokens for input + output in one request |
| `max_output_tokens` | Maximum tokens the model can generate in a single reply |
| `knowledge_cutoff` | Month the model's training data extends to, e.g. `"2025-04"` |
| `supports_reasoning` | Whether the model can show step-by-step thinking |
| `supports_function_calling` | Whether the model supports tool/function calling via the API |
| `input_modalities` | What the model accepts: `["text"]`, `["text", "image"]`, `["text", "image", "video"]`, `["text", "image", "video", "audio"]` |
| `output_modalities` | What the model produces: typically `["text"]` |
| `notice` | Optional admin note shown to users on the model detail page |

All fields except `name`, `input_cost_per_million`, and `output_cost_per_million` are optional. Everything else fills in the UI and API responses.

## Endpoints

Each model can have one or more endpoints:

| Field | Description |
|-------|-------------|
| `url` | Base URL of the backend server (e.g. `https://internal-server/v1`) |
| `api_key` | API key required by the backend |
| `model` | The model name the endpoint actually expects (defaults to the parent `name` if omitted) |

Setting `model` to a different value lets Lumen map its internal model name to whatever the endpoint calls the same model. This is useful when a single server serves multiple variants.

Round-robin distributes requests across all configured endpoints. A health checker periodically probes each endpoint and automatically routes traffic away from servers that fail.

## Multiple Endpoints for Load Balancing

You can configure multiple endpoints for one model to distribute load:

```yaml
  - name: phi3
    access: allowed
    input_cost_per_million: 0.0
    output_cost_per_million: 0.0
    endpoints:
      - url: http://gpu-server-1.internal/v1
        api_key: key-one
        model: phi-3-mini
      - url: http://gpu-server-2.internal/v1
        api_key: key-two
        model: phi-3-mini
      - url: http://gpu-server-3.internal/v1
        api_key: key-three
        model: phi-3-mini
```

The models page shows how many of those endpoints are healthy. If all endpoints for a model are down, the model shows a "down" status and the chat interface hides it.

## Ollama (Local Models)

Ollama runs on your own hardware. It uses an OpenAI-compatible API at `http://localhost:11434/v1` and doesn't require a real API key — any non-empty string works:

```yaml
  - name: llama3.2
    access: allowed
    input_cost_per_million: 0.0
    output_cost_per_million: 0.0
    supports_reasoning: true
    input_modalities: ["text"]
    output_modalities: ["text"]
    endpoints:
      - url: http://localhost:11434/v1
        api_key: ollama
        model: llama3.2
```

## Duplicate Names

If the same `name` appears twice in `config.yaml`, the later entry wins. This can be useful for environment-specific overrides (e.g., a local dev model vs production).
