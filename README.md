# iLLM

A Flask-based LLM gateway that proxies requests to multiple OpenAI-compatible endpoints with token budgeting, usage tracking, and OAuth2 authentication.

## Quick Start

### 1. Configure

Copy the example config and fill in your values:

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml`:
- Set `app.secret_key` to a random string
- Set `app.database_url` (defaults to SQLite)
- Fill in `oauth2.*` credentials (register at https://cilogon.org/oauth2/register)
- Add your model endpoints under `models:`

To use a config file at a different path:
```bash
CONFIG_YAML=/path/to/config.yaml uv run illm
```

### 2. Run (development)

```bash
uv run flask --app run db upgrade
uv run illm
```

Or use the convenience script:
```bash
./dev.sh
```

### 3. Run (production)

Production uses uvicorn via `entrypoint.sh` (e.g. in Docker):
```bash
./entrypoint.sh
```
