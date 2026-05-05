# User Groups and Access Control

Lumen uses a group-based system to assign coin budgets and model access controls. Groups are matched to users at login using identity-provider data from CILogon.

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

Groups are defined under the `groups` key. Users are automatically placed into matching groups when they log in via CILogon.

```yaml
groups:
  default:
    max: 0
    refresh: 0
    starting: 0
    model_access:
      default: blacklist

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
      default: whitelist
```

### Group Structure

| Field | Description |
|-------|-------------|
| `rules` | Conditions that trigger group membership at login |
| `max` | Daily coin budget (0 = denied, -2 = unlimited) |
| `refresh` | Coins added per hour (0 = no refresh) |
| `starting` | Initial coin pool when a user is first created |
| `model_access` | Per-group model access rules |

## Group Rules

Rules match against fields in the user's CILogon identity data:

| Field | Available Values | Example |
|-------|-----------------|---------|
| `affiliation` | Email-style affiliations from CILogon | `staff@illinois.edu`, `student@edu.org` |
| `idp` | Identity provider URN | `urn:mace:incommon:uiuc.edu` |
| `member_of` | Group membership from CILogon | `icc-grp-aifarms` |
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
      default: blacklist
      graylist: [dummy]

  students:
    rules:
      - field: affiliation
        contains: student@
    max: 10.0
    refresh: 0.02
    starting: 10.0
    model_access:
      default: graylist       # allows models in user's graylist

  researchers:
    rules:
      - field: affiliation
        contains: faculty@
    max: 50.0
    refresh: 0.1
    starting: 50.0
    model_access:
      default: whitelist      # all models available
      blacklist: [deprecated] # except this one
```

- A **student** gets 10 coins, can use graylisted models after acknowledging them.
- A **researcher** gets 50 coins, can use all models except the deprecated one.
- An **unmatched user** gets nothing.
