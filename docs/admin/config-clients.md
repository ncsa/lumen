# Configuring Clients

> 🔒 **Admin only.** This page documents administrator features. Configuration lives in `config.yaml` and the in-app Config editor (`/admin/config`), which are only available to administrators.

Clients are named identities for automated tools or apps that need their own API access, separate from personal user accounts.

## When to Use Clients

Clients are useful when:

- You have an application or service that calls AI models on behalf of users
- You run automated scripts or pipelines that need stable, long-lived credentials
- You want to separate application usage from personal usage and budgets
- A team needs to share API access without giving out personal keys

## Global Defaults

Under `clients.default`, set the default coin pool and model access for any client that doesn't have a named entry:

```yaml
clients:
  default:
    max: 100.0            # coin budget (-2 = unlimited, 0 = blocked)
    refresh: 0.05         # coins added per hour
    starting: 100.0       # coins when pool is first created
    model_access:
      default: blocked     # deny everything not explicitly listed
      blocked: []          # always-deny list
      allowed: [dummy]     # always-allow list
```

| Field | Description |
|-------|-------------|
| `max` | Coin budget cap (0 = blocked, -2 = unlimited) |
| `refresh` | Coins replenished per hour, up to the `max` cap (0 = no auto-refresh) |
| `starting` | Initial coins when a client's pool is created |
| `model_access.default` | Default behavior for unlisted models: `allowed` or `blocked` |
| `model_access.allowed` | Models always accessible to this client |
| `model_access.blocked` | Models always denied to this client |

The `max`, `refresh`, and `starting` fields fall back to the top-level `defaults.tokens` block when omitted from a named client; only the fields that differ from the defaults need to be set. See [Admin Configuration](config.md) for the `defaults` block.

Like groups, a client's `model_access` controls only the **allow/block axis**. Acknowledgement is a model-level property (`needs_ack` — see [Configuring Models](config-models.md#access-control)); there is no per-client graylist. Managers grant acknowledgement on a client's behalf through the UI.

> **Deprecated keys:** legacy `whitelist`/`blacklist`/`graylist` are still accepted as input (with a deprecation warning) — `whitelist`→`allowed`, `blacklist`→`blocked`, `graylist`→`allowed` plus a reminder to set `needs_ack` on the model. Prefer `allowed`/`blocked`.

## Per-Client Overrides

Add a named entry under `clients` to give a specific client different settings:

```yaml
clients:
  default:
    max: 100.0
    refresh: 0.05
    starting: 100.0
    model_access:
      default: blocked

  research-bot:
    max: 500.0
    refresh: 1.0
    starting: 500.0
    model_access:
      default: allowed
      allowed: [gpt-4o, llama3, qwen3.5-9b-q5]
```

A named entry **completely replaces** the defaults for that client — there is no partial inheritance. Any field omitted from a named entry is not inherited from `default`; the client will have no budget or model access for that field until it is explicitly set. (An **empty** entry, `my-client: {}`, is the one exception: it carries no settings and so falls back to `clients.default`, just like a client with no entry at all. This is what the UI writes when a client is first created — see [Creating Clients](#creating-clients).)

### Groups

A client can be added to one or more groups with a `groups:` list. Membership can grant access to models the client's own `model_access` rules would otherwise block — the same group model-access resolution used for users applies to clients:

```yaml
clients:
  research-bot:
    max: 500.0
    groups: [research, beta-models]
```

Memberships listed here are config-managed: they are added on config reload and removed when dropped from the list. Unknown group names are skipped with a warning. See [Configuring Users](config-users.md) for how groups and their model access work.

## Creating Clients

Clients are created through the web interface at `/clients` by an admin. When you create one, Lumen also writes an empty entry (`<client-name>: {}`) into `config.yaml` so the file always records that the client exists; you can then fill in its budget, model access, and groups. Because an empty entry falls back to `clients.default`, a newly created client starts with the default budget and access until you customize it.

> **Note:** Writing to `config.yaml` (on client creation or via the Config editor) rewrites the file and does not preserve comments. Client creation skips the write when the Config editor is disabled or the file is not writable — the client is still created in the database, but you'll need to add its entry manually.

On startup, any clients that already exist in the database but are missing from `config.yaml` are backfilled with an empty entry (when the file is writable and the Config editor is enabled), so upgrading an existing install populates the file automatically.

The sync command (`uv run flask init-db`) and the config watcher apply budgets, model access, and group membership to clients that exist in the database. Once a client exists, the UI lets you assign managers and create API keys.
