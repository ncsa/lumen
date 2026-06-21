# Admin Configuration

Lumen is configured entirely through a single file: `config.yaml`. You place this file in the Lumen directory and the application reads it on startup, then watches for changes while running.

## File Version

The config file declares its schema version at the top:

```yaml
version: 2
```

Version 2 introduced the orthogonal model-access model (`access` / `needs_ack` / `disabled` per model, and `allowed`/`blocked` allow-lists per scope) and the top-level `defaults` block. Legacy version-1 keys (`whitelist`/`blacklist`/`graylist`, `active:` on models, `app.graylist_default_notice`) are still accepted as input with a deprecation warning, but new configs should use the version-2 forms.

## Global Defaults

The top-level `defaults` block sets fallbacks used when a model or scope omits a field:

```yaml
defaults:
  models:
    access: blocked            # baseline for models that omit `access:`
    ack_message: "This model was trained outside the U.S. — use with awareness."
  tokens:
    max: 0                     # fallback coin pool for groups/clients
    refresh: 0
    starting: 0
```

| Field | Description |
|-------|-------------|
| `defaults.models.access` | Baseline allow/block state for any model that does not set its own `access`. |
| `defaults.models.ack_message` | Global acknowledgement message shown for `needs_ack` models that don't set their own `ack_message`. (Replaces the old `app.graylist_default_notice`, which is still accepted as input.) |
| `defaults.tokens.max` / `refresh` / `starting` | Fallback coin-pool values. A group or client only needs to set the fields that differ from these; omitted token fields are filled from `defaults.tokens`. |

## Model Access Resolution

The allow/block decision for an entity and a model is resolved in this order — **explicit rules always beat defaults**, and a model's own `access` beats group/entity *defaults* but is itself overridden by an explicit per-scope rule:

1. **Entity rule** — an `allowed`/`blocked` entry in the user's or client's own `model_access`.
2. **Group rule** — an `allowed`/`blocked` entry in any of the user's groups (a `blocked` in any group beats an `allowed`).
3. **Model `access`** — the model's own `access` field, when set (`allowed`/`blocked`). This lets a model be blocked-by-default even for groups whose `default` is `allowed`, while still being grant-able via an explicit group/user `allowed` rule (tiers 1–2).
4. **Group default** — `model_access.default` of the user's groups.
5. **Entity default** — the entity's own `model_access.default`.
6. **Global default** — `defaults.models.access` (used when the model leaves `access` unset and no scope default applies).

Two model-level properties sit outside this chain:

- **`disabled: true`** short-circuits the whole resolution to blocked — it is never overridable by any scope.
- **`needs_ack: true`** does not affect allow/block; it adds the one-time acknowledgement gate (the existing consent flow) for any user who is allowed the model.

### Worked example: `model-a`

Suppose a user is a member of the group **`students`**, whose `model_access.default` is `allowed`. The table shows how `model-a` resolves for that user under different settings (top rows win):

| `model-a` setting | A group/user explicit rule for `model-a`? | Result for the user | Why |
|-------------------|--------------------------------------------|---------------------|-----|
| `disabled: true` | user `allowed: [model-a]` | **Blocked** | `disabled` short-circuits everything — not overridable (tier 0) |
| `access: blocked` | user `model_access.allowed: [model-a]` | **Allowed** | entity rule (tier 1) beats the model's `access` |
| `access: blocked` | group `model_access.allowed: [model-a]` | **Allowed** | explicit group rule (tier 2) beats the model's `access` — *this is the "blocked by default, enabled for a test group" pattern* |
| `access: blocked` | none | **Blocked** | model `access` (tier 3) beats the group's `default: allowed` (tier 4) |
| `access: allowed` | none | **Allowed** | model `access` (tier 3) |
| `access` unset | none | **Allowed** | inherits the `students` group `default: allowed` (tier 4) |
| `access` unset | none, **and user is in no group** | **Blocked** | falls through to the global `defaults.models.access: blocked` (tier 6) |
| `access: allowed` + `needs_ack: true` | none | **Allowed, after the user acknowledges it once** | `needs_ack` adds the consent gate on top of an allowed result |

So to **block `model-a` for everyone but a small set of test users**: set `access: blocked` on the model, then add those users (or a group they're in) with an explicit `model_access.allowed: [model-a]`.

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
| `models[*].access` / `disabled` | Allow/block a model or take it fully offline |
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
| `app.database` | Database connection URL and pool settings are established at startup |
| `app.debug` | Debug flag affects core application initialization |
| `oauth2.*` | OAuth client ID, secret, and server metadata are used during session setup |
| `api.prometheus.enabled` | Metrics collector is initialized at startup |
| `api.prometheus.multiproc_dir` | Multi-process aggregation directory |
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
- The `app.encryption_key` has special behavior: changing it invalidates **all** existing user API keys and requires a restart. Use `LUMEN_ENCRYPTION_KEY` to inject it at deploy time without writing it into the config file.
