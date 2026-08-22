# Configuring Projects

> 🔒 **Admin only.** This page documents administrator features.

Projects are named identities for automated tools or apps that need their own API access, separate from personal user accounts.

## When to Use Projects

Projects are useful when:

- You have an application or service that calls AI models on behalf of users
- You run automated scripts or pipelines that need stable, long-lived credentials
- You want to separate application usage from personal usage and budgets
- A team needs to share API access without giving out personal keys

## Managing Projects

Projects live entirely in the database and are managed through the web interface — there is no `projects:` section in `config.yaml`. (Config version 3 removed the section; a config that still contains one is rejected at startup — see [Admin Configuration](config.md#file-version).)

- **Create** a project at `/projects` (admin only). A new project has no coin pool of its own and falls back to the global `defaults.tokens` pool (see [Admin Configuration](config.md)) or a group pool if the project is a group member.
- **Edit** a project from its detail page (`/projects/<id>`) via the **Edit** button above the stats: the project owner or an admin can change the name and active flag; only admins can set the coin pool (**Max Coins** and **Refill Rate**). Clearing Max Coins removes the project's own pool so it falls back to the inherited group/default pool.
- **Assign managers and create API keys** from the detail page tabs.

### Coin Pool

| Field | Description |
|-------|-------------|
| Max Coins | Coin budget cap (0 = blocked, -2 = unlimited) |
| Refill Rate | Coins replenished per hour, up to the cap (0 = no auto-refresh) |

Setting Max Coins from the Edit dialog also sets the starting balance (what an admin coin reset refills to) to the same value. Lowering Max Coins clamps the project's current balance to the new cap.

### Model Access and Groups

Projects get model access the same way users do: every public (unowned) model is available to every project. Owned models are granted through groups, and there is currently **no way to add a project to a group** — group memberships created by older Lumen versions remain in effect, but new project memberships cannot be created yet (group membership dialogs are planned). Until then, a model that a project needs must stay public. Acknowledgement is a model-level property (`needs_ack` — see [Configuring Models](config-models.md#access-control)); managers grant acknowledgement on a project's behalf through the UI.

> **Migration note:** older Lumen versions synced per-project budgets, model access, and groups from a `projects:` section in `config.yaml`. Rows created by that sync remain in effect in the database; the config section itself is no longer read.
