# Admin Configuration

Lumen is configured entirely through a single file: `config.yaml`. You place this file in the Lumen directory and the application reads it on startup, then watches for changes while running.

## Where to Find It

The file is typically named `config.yaml` and lives at the root of the Lumen installation. See the `config.py` file for the default name, or set a custom location with the `CONFIG_YAML` environment variable.

To get started, copy the example file:

```bash
cp config.yaml.example config.yaml
```

## Hot Reload

When running, Lumen watches `config.yaml` for changes and automatically reloads most settings every 5 seconds — no restart required. Changes that take effect immediately include:

| Setting | Effect |
|---------|--------|
| `models[*].active` | Enable or disable models |
| `models[*].endpoints` | Add, remove, or move model backend servers |
| `models[*].input_cost_per_million` / `output_cost_per_million` | Change pricing |
| `groups[*]` | Add, edit, or remove user groups |
| `groups[*].model_access` | Override model access rules per group |
| `clients.default` | Change default coin pool for new clients |
| `clients[*]` | Configure individual client budgets and access |
| `admins` | Update the list of administrator email addresses |
| `chat.remove` | Change conversation soft-delete vs hard-delete mode |
| `chat.upload` | Adjust upload file size limits and allowed file types |
| `rate_limiting.limit` | Change request rate limits |

## Changes That Require a Restart

Some settings are read only at startup and cannot be hot-reloaded. Lumen logs a warning when these change (except where noted):

| Setting | Why |
|---------|-----|
| `app.secret_key` | Flask session signing key — changing it invalidates all active sessions |
| `app.database_url` | Database connections are established at startup |
| `app.debug` | Debug flag affects core application initialization |
| `oauth2.*` | OAuth client ID, secret, and server metadata are used during session setup |
| `prometheus.enabled` | Metrics collector is initialized at startup |
| `prometheus.multiproc_dir` | Multi-process aggregation directory |
| `rate_limiting.storage_url` | Redis connection is established at startup; no runtime warning is emitted if this changes |

> **`app.encryption_key`** is not enforced by the watcher but is equally dangerous to rotate at runtime — changing it immediately invalidates all stored API key hashes. See [Security Notes](#security-notes).

## How It Works

On startup, Lumen validates `config.yaml` and loads it into memory. While running, a background thread checks the file's modification time every 5 seconds. When a change is detected, it re-parses the YAML, applies the differences, and logs `config.yaml reloaded`. If a restart-required setting changed, it also emits a warning.

The `init-db` command syncs config changes to the database (models, groups, model access, clients) without waiting for the watcher or restarting. It does not update in-memory settings like `APP_NAME` or `CHAT_CONVERSATION_REMOVE_MODE` — those only update when the watcher picks up the change or the app restarts.

```bash
uv run flask init-db
```

## Security Notes

- `app.secret_key` and `app.encryption_key` should be long random strings in production.
- Never commit `config.yaml` with real secrets to a shared repository — use the `.example` file as a template and keep your live `config.yaml` in a private location or inject secrets via environment variables.
- The `app.encryption_key` has special behavior: changing it invalidates **all** existing user API keys and requires a restart. Set the environment variable `LUMEN_API_KEY_SECRET` to override the encryption key at runtime, or set `LUMEN_ENCRYPTION_KEY` to override the `app.encryption_key` value.
