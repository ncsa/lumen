# Admin Configuration

> 🔒 **Admin only.** This page documents administrator features. Configuration lives in `config.yaml` and the in-app Config editor (`/admin/config`), which are only available to administrators.

> **Note:** Administrators act as normal users by default. To use the admin pages, turn on the **Admin mode** switch on your [profile page](../guides/profile.md#admin-mode); it resets when you log out.

Lumen is configured entirely through a single file: `config.yaml`. You place this file in the Lumen directory and the application reads it on startup, then watches for changes while running.

## File Version

The config file declares its schema version at the top:

```yaml
version: 3
```

Lumen 2.0 requires **version 3** and refuses to start on anything older — the startup error names the removed sections so you know what to migrate. A hot reload of an older-version file is skipped with a logged error. Version 3 removes from `config.yaml`:

| Removed | Where it went |
|---------|---------------|
| `users:` | Explicit group memberships and per-user coin pools live in the database (edited from the user's profile page). For auto-assignment at login, use each group's Rules tab |
| `projects:` | Projects live entirely in the database (see [Configuring Projects](config-projects.md)) |
| `clients:` | Same as projects |
| `groups:` | Groups — coin pools, memberships, model grants, and login auto-join rules — live in the database, managed on the Groups pages (a leftover `group_rules:` section is ignored with a startup warning; remove it and recreate the rules on each group's Rules tab) |
| `access:` on a model, `model_access` on groups/users, `defaults.models.access`, legacy `whitelist`/`blacklist`/`graylist` | DB-managed model ownership (see [Model Access Resolution](#model-access-resolution)) |

Existing database rows created by older config syncs (groups, memberships, pools) keep working and are fully manageable on the Groups pages. Login auto-join rules are edited per group on its Rules tab — see [User Groups and Access Control](config-users.md#auto-join-rules).

## Global Defaults

The top-level `defaults` block sets fallbacks used when a model or scope omits a field:

```yaml
defaults:
  models:
    ack_message: "This model was trained outside the U.S. — use with awareness."
    early_access_message: "This model is an early-access preview and may change or be removed."
  tokens:
    max: 0                     # fallback coin pool for users/projects without their own
    refresh: 0
    starting: 0
```

| Field | Description |
|-------|-------------|
| `defaults.models.ack_message` | Global acknowledgement message shown for `needs_ack` models that don't set their own `ack_message`. (Replaces the old `app.graylist_default_notice`, which is still accepted as input.) |
| `defaults.models.early_access_message` | Warning shown when acknowledging an `early_access` model. When unset, a built-in default message is used. |
| `defaults.tokens.max` / `refresh` / `starting` | Fallback coin-pool values. A group or project only needs to set the fields that differ from these; omitted token fields are filled from `defaults.tokens`. |

## Model Access Resolution

Model access is based on **ownership** and lives in the database — it is not configured in `config.yaml`. A model may have an owner (a user); ownership and group grants are edited by admins via the **Access** card on the model detail page (`/models/<name>`). For an entity (user or project) and a model, access resolves in this order:

1. **Disabled or expired** — `disabled: true` or a past `end_date` → blocked, for everyone, not overridable.
2. **No owner** — the model is **public**: available to every user and project.
3. **Owner** — the entity is the model's owner → allowed.
4. **Granted group** — the entity is a member of an active group the model has been granted to → allowed.
5. **Otherwise** → blocked.

Two model-level properties sit outside this chain and remain config keys:

- **`needs_ack: true`** does not affect access; it adds the one-time acknowledgement gate (the existing consent flow) for any user who has access to the model. `early_access` works the same way.
- **`disabled: true`** takes the model offline for everyone (step 1 above).

Config sync never touches owners or grants; the config keys that used to control access (`access:` on a model, `model_access` on groups or users, `defaults.models.access`) were removed in version 3 — a config that still contains them is rejected at startup. Deleting the owner user makes the model public again; deleting a granted group removes the grant.

So to **restrict `model-a` to a small set of test users**: assign `model-a` an owner from its detail page, put the test users in a group, and grant that group access to the model.

> **Upgrading:** after upgrading from the config-based allow/block system, all non-disabled models are **public** until an admin assigns owners. This is a breaking change — assign owners before inviting users if some models should be restricted.

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
| `models[*].disabled` | Take a model fully offline |
| `models[*].endpoints` | Add, remove, or move model backend servers |
| `models[*].input_cost_per_million` / `output_cost_per_million` | Change pricing |
| `admins` | Update the list of administrator email addresses |
| `chat.remove` | Change conversation soft-delete vs hard-delete mode |
| `chat.upload` | Adjust upload file size limits and allowed file types |
| `rate_limiting.limit` | Change request rate limits |

## Changes That Require a Restart

Some settings are read only at startup and cannot be hot-reloaded. Lumen logs a warning when these change:

| Setting | Why |
|---------|-----|
| `app.secret_key` | Flask session signing key — changing it invalidates all active sessions |
| `app.encryption_key` | API key hashing secret, read once at startup — rotating it invalidates every stored API key |
| `app.database` | Database connection URL and pool settings are established at startup |
| `app.debug` | Debug flag affects core application initialization |
| `app.logs.level` | The application log level is set on the logger at startup (`app.logs.access` and `app.logs.model` do hot-reload) |
| `oauth2.*` | OAuth client ID, secret, and server metadata are used during session setup |
| `api.prometheus.enabled` | Metrics collector is initialized at startup |
| `api.prometheus.multiproc_dir` | Multi-process aggregation directory |
| `rate_limiting.storage_url` | Redis connection is established at startup, and it also selects the live-state backend |

> **`app.encryption_key`** is as dangerous to rotate at runtime as it is to change at all — a new value invalidates every stored API key hash. See [Security Notes](#security-notes).

## How It Works

On startup, Lumen validates `config.yaml` and loads it into memory. While running, a background thread checks the file's modification time every 5 seconds. When a change is detected, it re-parses the YAML, applies the differences, and logs `config.yaml reloaded`. If a restart-required setting changed, it also emits a warning.

The `init-db` command syncs model config changes to the database without waiting for the watcher or restarting. It does not update in-memory settings like `APP_NAME` or `CHAT_CONVERSATION_REMOVE_MODE` — those only update when the watcher picks up the change or the app restarts.

```bash
uv run flask init-db
```

## Security Notes

- `app.secret_key` and `app.encryption_key` should be long random strings in production.
- Never commit `config.yaml` with real secrets to a shared repository — use the `.example` file as a template and keep your live `config.yaml` in a private location or inject secrets via environment variables.
- The `app.encryption_key` has special behavior: changing it invalidates **all** existing user API keys and requires a restart. Use `LUMEN_ENCRYPTION_KEY` to inject it at deploy time without writing it into the config file.
