# Configuring Models

> 🔒 **Admin only.** This page documents administrator features. Configuration lives in `config.yaml` and the in-app Config editor (`/admin/config`), which are only available to administrators.

Models are the core of Lumen. Each entry tells Lumen how to reach an AI model, what it costs, and what it can do.

## Basic Model Entry

Every model starts with a name and an `endpoints` list:

```yaml
models:
  - name: my-model
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

## Access Control

Who may use a model is **not** configured in `config.yaml`. A model may have an **owner** (a user): a model with no owner is available to everyone (users and projects), while an owned model is available only to its owner and to members of groups the model has been explicitly granted to. Other users and projects cannot list the model or retrieve its detail-page or README metadata. Models awaiting acknowledgment remain visible because acknowledgment is a consent requirement, not an access grant. Ownership and group grants live in the database and are edited by admins via the **Access** card on the model detail page (`/models/<name>`) — see [Model Detail](../models/model-detail.md#access-admin-only). Config sync never touches them; the per-model `access:` key was removed in config version 3. If it remains in a config, Lumen ignores it and it does not restrict access; assign an owner and group grants in the UI instead.

> **Upgrading:** after upgrading from a version that used the config-based allow/block system, all non-disabled models are **public** until an admin assigns owners.

The remaining per-model fields below stay in `config.yaml`. They control acknowledgement and lifecycle, not who has access.

| Field | Default | Description |
|-------|---------|-------------|
| `needs_ack` | `false` | When `true`, a user must acknowledge the model before using it. This is a **sticky, model-level property** — no group, project, or user scope can add or remove it. It only triggers the consent gate; it does not by itself grant or deny access. |
| `early_access` | `false` | When `true`, the model is an early-access preview: users must acknowledge that it may change or be removed at any time before using it. Works like `needs_ack` (sticky, model-level) and can be combined with it — one dialog acknowledges both at once. The warning text comes from `defaults.models.early_access_message` (a built-in default is used when unset). If a model gains `early_access` (or `needs_ack`) after a user already acknowledged it, the user is prompted once more for the new requirement. |
| `end_date` | unset | Date or datetime (UTC) after which the model is hidden everywhere and rejected, exactly like `disabled`. The comparison is exclusive: `end_date: 2026-09-01` means the model is usable through the end of August 31 UTC. Leave unset for no end date. |
| `disabled` | `false` | **Hard off.** The model is hidden everywhere and cannot be used. This is **not overridable** by any scope — it always wins. Use it to take a model offline without deleting it. |
| `ack_message` | unset | Optional acknowledgement message shown when `needs_ack` is `true`. Overrides the global `defaults.models.ack_message`. |

```yaml
models:
  - name: my-model
    needs_ack: true       # require acknowledgement (model-level, sticky)
    ack_message: "This model was trained outside the U.S. — use with awareness."
    early_access: true    # preview model; users must acknowledge it may change or be removed
    end_date: 2026-12-31  # hidden and rejected after this date (UTC, exclusive); omit for no end date
    input_cost_per_million: 0.5
    output_cost_per_million: 1.0
    endpoints:
      - url: https://example.com/v1
        api_key: sk-your-key
```

### `disabled` is a hard off

Setting `disabled: true` blocks the model for everyone — it disappears from the chat UI and the API, regardless of ownership or group grants. This replaces the old `active: false`. To **permanently** remove a model, delete its entry from `config.yaml` entirely.

> The legacy `active:` key is still accepted as input (with a deprecation warning): `active: false` maps to `disabled: true`. Prefer `disabled` in new configs.

### `needs_ack` lives on the model

Acknowledgement is a property of the model, not of any group or scope. Set `needs_ack: true` on the model and every user who has access to the model must acknowledge it once before using it.

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
| `url` | Link to the model's documentation page. A bare HuggingFace repo id (e.g. `meta-models/Muse-Glimmer-30B`) expands to `https://huggingface.co/<id>`, and HuggingFace host variants (`huggingface.com`, `www.`) are rewritten to `huggingface.co`; any other full URL is used as given. `huggingface.co` URLs also show the model's README on the detail page |
| `context_window` | Maximum total tokens for input + output in one request |
| `max_output_tokens` | Maximum tokens the model can generate in a single reply |
| `knowledge_cutoff` | Month the model's training data extends to, e.g. `"2025-04"`. Full dates (`"2025-04-15"`) are truncated to the month. |
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
