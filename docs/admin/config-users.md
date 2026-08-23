# User Groups and Access Control

> 🔒 **Admin only.** This page documents administrator features. Configuration lives in `config.yaml` and the in-app Config editor (`/admin/config`), which are only available to administrators.

Lumen uses a group-based system to assign coin budgets and grant access to owned models. `config.yaml` carries a single user-management section: `admins:`. Everything about groups — coin pools, memberships (users and projects), model grants, and login auto-join rules — lives in the database and is managed on the [Groups pages](../groups/groups.md).

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

`app.dev_user.groups` still works: the listed groups are assigned to the dev user at login; names that don't match an existing group are ignored. To make the dev user an admin, add their email to the top-level `admins:` list. Group membership does not grant admin status.

`dev_user` is for development only and should be removed in production.

## Auto-join Rules

Login auto-assignment is configured per group on the group detail page's **Rules** tab (admin-only) — see [Group Management](../groups/groups-detail.md#rules-admin-only). A user matching **all** of a group's rules (AND logic) is added to the group at sign-in and removed again when they stop matching.

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

There is no `group_rules:` section in `config.yaml` — rules exist only in the database. A leftover section from an older config is ignored with a startup warning; remove it and recreate the rules on each group's Rules tab.

## Everything Else Lives in the Database

Config version 3 removed the `groups:` and `users:` sections. What they used to configure is now DB-managed:

- **Group coin pools** (`max`/`refresh`/`starting`) — an admin sets a group's Max Coins and Refill Rate from the group's Edit dialog. Entities without their own pool fall back to their best group pool and then the top-level `defaults.tokens` block (see [Admin Configuration](config.md)).
- **Group memberships** — owners and admins manage members on the group detail page; auto-join rules add members at login.
- **Per-user coin pools** — an admin sets a user's Max Coins and Refill Rate (and can enable/disable the account) from the **Edit** button on the user's profile page (`/admin/users/<id>/profile`, or the admin's own `/profile` in admin mode).

## Groups and Model Access

Model access follows ownership: a model with no owner is available to everyone; an owned model is available only to its owner and to members of groups the model has been granted to. Grants are managed by admins on the model detail page (see [Configuring Models](config-models.md#access-control)) — never in `config.yaml`.

Acknowledgement is also not a group setting — it lives on the model via `needs_ack`; if an accessible model has `needs_ack: true`, members still acknowledge it once before use.
