# Configuring Clients

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
      default: blacklist   # deny everything not explicitly listed
      blacklist: []        # always-deny list
      whitelist: [dummy]   # always-allow list
```

| Field | Description |
|-------|-------------|
| `max` | Daily coin budget (0 = blocked, -2 = unlimited) |
| `refresh` | Coins replenished per hour (0 = no auto-refresh) |
| `starting` | Initial coins when a client's pool is created |
| `model_access.default` | Default behavior for unlisted models: `whitelist`, `blacklist`, or `graylist` |
| `model_access.whitelist` | Models always accessible to this client |
| `model_access.blacklist` | Models always denied to this client |
| `model_access.graylist` | Models the client can access after consent is recorded via the UI |

## Per-Client Overrides

Add a named entry under `clients` to give a specific client different settings:

```yaml
clients:
  default:
    max: 100.0
    refresh: 0.05
    starting: 100.0
    model_access:
      default: blacklist

  research-bot:
    max: 500.0
    refresh: 1.0
    starting: 500.0
    model_access:
      default: whitelist
      whitelist: [gpt-4o, llama3, qwen3.5-9b-q5]
```

A named entry **completely replaces** the defaults for that client — there is no partial inheritance. Any field omitted from a named entry is not inherited from `default`; the client will have no budget or model access for that field until it is explicitly set.

## Creating Clients

Clients must be created through the web interface at `/clients` by an admin — YAML named entries only control budgets and model access for clients that already exist in the database. The sync command (`uv run flask init-db`) and the config watcher will silently skip any named entry for a client that hasn't been created through the UI yet.

Once a client exists, the UI lets you assign managers and create API keys, and the YAML controls the coin budget and model access that sync applies.
