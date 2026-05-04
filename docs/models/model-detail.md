# Model Detail

The model detail page (`/models/<name>`) shows in-depth information about a specific AI model.

![Model detail page](/help/img/model-detail.png)

## Page Structure

The page is laid out in two columns:

### Left Column

- **Model name** with a HuggingFace link (if `url` is configured in `config.yaml`)
- **Description** — A short description from the config
- **HuggingFace README** — Automatically loaded and rendered from the configured URL (via marked.js)

### Right Column (Sidebar)

Six cards, some of which are conditional:

#### Access Acknowledgment (graylisted models only)

| State | Displays |
|-------|----------|
| **Not yet acknowledged** | Warning card with "Acknowledge & Enable Access" button |
| **Already acknowledged** | "Access enabled" with date of your acknowledgment |
| **Blocked** | Red alert saying access is denied |

Acknowledgment is a one-time per-user (or per-client) record stored in `EntityModelConsent`.

#### Availability

| Field | Description |
|-------|-------------|
| **Status** | Overall model health: ok / degraded / down |
| **Endpoints** | Healthy endpoint count vs total |
| **Endpoint URLs** | Shown only to admins, with per-endpoint up/down badge |
| **Requests / hr** | Request count in the last hour |
| **Requests / 24h** | Request count in the last 24 hours |

#### Model Details (conditional — only if defined in config)

Technical specifications from `config.yaml`:

| Field | Source |
|-------|--------|
| Context Window | `context_window` — max tokens the model can process |
| Max Output | `max_output_tokens` — maximum tokens the model can generate |
| Input | `input_modalities` — e.g., `["text", "image"]` |
| Output | `output_modalities` — e.g., `["text"]` |
| Knowledge Cutoff | `knowledge_cutoff` — e.g., `"2024-04"` |
| Reasoning | `supports_reasoning` — shows a checkmark if true |
| Function Calling | `supports_function_calling` — shows a checkmark if true |

#### Pricing

| Field | Description |
|-------|-------------|
| **Input** | Cost per million input tokens |
| **Output** | Cost per million output tokens |
