# User Groups and Access Control

> 🔒 **Admin only.** This page documents administrator features. Configuration lives in `config.yaml` and the in-app Config editor (`/admin/config`), which are only available to administrators.

Lumen uses a group-based system to assign coin budgets and grant access to owned models. As of config version 3, `config.yaml` carries only two user-management sections: `admins:` and `group_rules:`. Everything else about groups — coin pools, memberships (users and projects), and model grants — lives in the database. Rows created by older config-based syncs keep working; management dialogs for groups and pools are planned.

## Admins

Admins have full access to the Lumen interface, including the ability to create projects, view all usage data, and manage all users. Add admin emails under `admins`:

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

`app.dev_user.groups` still works: the listed groups are assigned to the dev user at login. To make the dev user an admin, add their email to the top-level `admins:` list. Group membership does not grant admin status.

`dev_user` is for development only and should be removed in production.

## Group Rules

The top-level `group_rules:` section maps a group name to a list of rules matched against the user's OAuth identity-provider profile at every login. A user matching **all** rules of a group (AND logic) is automatically added to that group:

```yaml
group_rules:
  staff:
    - field: affiliation
      contains: staff@illinois.edu
    - field: idp
      equals: urn:mace:incommon:uiuc.edu
```

A group named under `group_rules` is created (as a bare group row) if it does not exist yet; an empty rule list just ensures the group exists:

```yaml
group_rules:
  manual-group: []    # created if missing; members are managed in the app
```

Config sync never edits or deletes groups — removing a name from `group_rules` only stops the auto-assignment; the group and its members stay in the database.

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

## Everything Else Lives in the Database

Config version 3 removed the `groups:` and `users:` sections. What they used to configure is now DB-managed:

- **Group coin pools** (`max`/`refresh`/`starting`) — pools created by older config syncs remain in effect; group management dialogs are planned. Entities without their own pool fall back to their best group pool and then the top-level `defaults.tokens` block (see [Admin Configuration](config.md)).
- **Explicit group memberships** (the old `users: <email>: groups: [...]`) — memberships created earlier keep working; new ones will be added through the planned group dialogs. Rule-based auto-assignment via `group_rules` is the config-driven path.
- **Per-user coin pools** — an admin sets a user's Max Coins and Refill Rate (and can enable/disable the account) from the **Edit** button on the user's profile page (`/admin/users/<id>/profile`, or the admin's own `/profile` in admin mode).

## Groups and Model Access

Model access follows ownership: a model with no owner is available to everyone; an owned model is available only to its owner and to members of groups the model has been granted to. Grants are managed by admins on the model detail page (see [Configuring Models](config-models.md#access-control)) — never in `config.yaml`.

Acknowledgement is also not a group setting — it lives on the model via `needs_ack`; if an accessible model has `needs_ack: true`, members still acknowledge it once before use.
