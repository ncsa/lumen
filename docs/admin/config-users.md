# User Groups and Access Control

Lumen uses a group-based system to assign coin budgets and model access controls. Groups are matched to users at login using OAuth identity-provider profiles.

## Admins

Admins have full access to the Lumen interface, including the ability to create clients, view all usage data, and manage all users. Add admin emails under `admins`:

```yaml
admins:
  - admin@example.com
  - another@example.com
```

The `dev_user` setting in `app` provides a login bypass for development — the specified email logs in directly without OAuth:

```yaml
app:
  dev_user:
    email: dev@example.com
    groups:
      - staff
```

To make the dev user an admin, add their email to the top-level `admins:` list. Group membership does not grant admin status.

`dev_user` is for development only and should be removed in production.

## Groups

Groups are defined under the `groups` key. Users are automatically placed into matching groups when they log in via OAuth.

```yaml
groups:
  default:
    max: 0
    refresh: 0
    starting: 0
    model_access:
      default: blocked

  staff:
    rules:
      - field: affiliation
        contains: staff@illinois.edu
      - field: idp
        equals: urn:mace:incommon:uiuc.edu
    max: 20.0
    refresh: 0.05
    starting: 20.0
    model_access:
      default: allowed
```

The `max`, `refresh`, and `starting` token fields fall back to the top-level `defaults.tokens` block when omitted — a group only needs to set the fields that differ from the defaults. See [Admin Configuration](config.md) for the `defaults` block.

### Group Structure

| Field | Description |
|-------|-------------|
| `rules` | Conditions that trigger group membership at login |
| `max` | Coin budget cap (0 = denied, -2 = unlimited) |
| `refresh` | Coins added per hour, up to the `max` cap (0 = no refresh) |
| `starting` | Initial coin pool when a user is first created |
| `model_access` | Per-group model allow/block rules |

### Group Model Access

A group's `model_access` block sets only the **allow/block axis** for its members:

```yaml
model_access:
  default: allowed | blocked   # baseline for models not listed below
  allowed: [model-name, ...]   # models this group may always use
  blocked: [model-name, ...]   # models this group may never use
```

| Field | Description |
|-------|-------------|
| `default` | What to do with models not named in `allowed`/`blocked`: `allowed` or `blocked` |
| `allowed` | Models always available to this group |
| `blocked` | Models always denied to this group |

Acknowledgement is **not** a group setting — it lives on the model via `needs_ack` (see [Configuring Models](config-models.md#access-control)). A group only decides whether a model is allowed or blocked; if an allowed model has `needs_ack: true`, members still acknowledge it once before use.

> **Deprecated keys:** the old `whitelist`/`blacklist`/`graylist` keys and the `graylist` default value are still accepted as input (with a deprecation warning) — `whitelist`→`allowed`, `blacklist`→`blocked`, and `graylist` maps to `allowed` plus a reminder to set `needs_ack` on the model. Prefer `allowed`/`blocked` in new configs.

## Group Rules

Rules match against fields in the user's OAuth identity-provider profile:

| Field | Available Values | Example |
|-------|-----------------|---------|
| `affiliation` | Email-style affiliations from the identity provider | `staff@illinois.edu`, `student@edu.org` |
| `idp` | Identity provider URN | `urn:mace:incommon:uiuc.edu` |
| `member_of` | Group membership reported by the identity provider | `icc-grp-aifarms` |
| `ou` | Organizational unit | `research@university.edu` |

Rules can use two matcher types:

| Matcher | Behavior | Example |
|---------|----------|---------|
| `contains` | Case-sensitive substring match | `contains: staff@illinois.edu` |
| `equals` | Exact match | `equals: urn:mace:incommon:uiuc.edu` |

All rules within a group must match for a user to be assigned that group (AND logic):

```yaml
  research-bot:
    rules:
      - field: affiliation
        contains: research@
      - field: idp
        equals: urn:mace:incommon:myuniversity.edu
```

## The default Group

The `default` group is applied to every user on login, even if no rules match. It defines the baseline budget and model access for someone who isn't assigned to any named group. Always set it explicitly so you know the fallback behavior.

## Multi-Tier Access Example

Here's an example with three tiers:

```yaml
groups:
  default:                    # everyone who doesn't match a named group
    max: 0
    refresh: 0
    starting: 0
    model_access:
      default: blocked
      allowed: [dummy]

  students:
    rules:
      - field: affiliation
        contains: student@
    max: 10.0
    refresh: 0.02
    starting: 10.0
    model_access:
      default: blocked
      allowed: [chat-basic]   # only this model

  researchers:
    rules:
      - field: affiliation
        contains: faculty@
    max: 50.0
    refresh: 0.1
    starting: 50.0
    model_access:
      default: allowed        # all models available
      blocked: [deprecated]   # except this one
```

- A **student** gets 10 coins and may use only `chat-basic`.
- A **researcher** gets 50 coins and can use all models except the deprecated one.
- An **unmatched user** gets nothing.

## Per-User Overrides

Individual users can be configured under a top-level `users` map keyed by email. A user override is layered **on top of** their group memberships and takes precedence over group rules.

```yaml
users:
  alice@example.edu:
    groups: [research-bot]      # extra named groups to add (in addition to rule-matched ones)
    max: 100                    # token pool override (missing fields fall back to defaults.tokens)
    refresh: 0.1
    starting: 100
    model_access:
      default: blocked          # this user's default for unlisted models
      allowed: [model-a]        # grant a model even when its own default is blocked
      blocked: [model-b]        # block a model for this user
```

| Field | Description |
|-------|-------------|
| `groups` | Named groups to add for this user, on top of any matched by group `rules`. |
| `max` / `refresh` / `starting` | Per-user token pool; missing fields fall back to `defaults.tokens`. |
| `model_access` | Same `allowed` / `blocked` / `default` shape as a group. A user rule beats group rules. |

**Legacy form:** an allowed-only `models: [name, ...]` list is still accepted and behaves like `model_access.allowed`. Prefer `model_access` for new config — the admin config editor writes that form.

These overrides are best managed from the **Users** section of the admin config editor, which lets you search the enabled models and set each one's access for the user while showing the resulting effective access and where it comes from (this user, a group, the model's own default, or the global default).

If any of those allowed models has `needs_ack: true`, the user must acknowledge it once before use — that requirement comes from the model, not from these groups.
