# Introduction

## What is Lumen?

Lumen is a self-hosted AI chat portal designed for research institutions such as universities and national labs. It provides a web-based interface where users can interact with AI models — including OpenAI-compatible endpoints, Ollama, vLLM, and Hugging Face — while giving administrators centralized control over authentication, model access, and spending budgets.

## Key Capabilities

- **Chat** — A browser-based chat interface with streaming responses, file attachments (images, documents, PDFs), and conversation management.
- **Federated Authentication** — Login via CILogon, the identity provider service used by research institutions, supporting all major universities and labs.
- **Coin Budgets** — Every user and client is assigned a coin pool (a token budget mapped to USD). Usage deducts from this pool. Groups can have auto-refreshing budgets.
- **Model Access Control** — Three access levels: whitelist (fully allowed), graylist (requires one-time user acknowledgment), and blacklist (blocked). Rules apply at the global, group, and per-user levels.
- **Admin Panel** — Manage users, groups, usage stats, and per-user model access overrides.
- **Round-Robin Load Balancing** — Distributes requests across multiple backend endpoints for each model, automatically skipping unhealthy ones.
- **OpenAI-Compatible API** — Programmatic access via a standard `/v1/` API using API keys.
- **Prometheus Metrics** — Scrape endpoint for monitoring request volume, token counts, and costs.

## Architecture at a Glance

```
Browser ──> Lumen (Flask + Bootstrap 5)
                │
                ├── CILogon OAuth (authentication)
                ├── config.yaml → sync → database
                │       ├── Models & endpoints
                │       ├── Groups & budgets
                │       └── Global access rules
                │
                └── Backend LLM endpoints (OpenAI, Ollama, vLLM, etc.)
```

Configuration lives in `config.yaml` and is synced to a SQLite or PostgreSQL database on startup. Background services handle endpoint health checks, coin auto-refill, and config hot-reloading.
