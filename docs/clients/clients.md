# Clients

The **Clients** page (`/clients`) is where administrators and managers manage service clients — entities that need their own programmatic access to AI models.

![Clients page](/help/img/clients.png)

## Concept: What is a Client?

A client is a named service or application entity (e.g., "research-bot", "production-app") that has its own API keys, coin pool, and usage tracking. Clients are separate from individual users but share the same underlying `Entity` table with `entity_type = 'client'`.

Clients are useful when:

- Building an application that interacts with AI models on behalf of users
- Running automated processes that need stable API credentials
- Separating usage and billing from individual user accounts

## Client List View

### What You See

Whether you can see all clients or only managed clients depends on your role:

| Role | Visibility |
|------|-----------|
| **Admin** | Sees **all** clients in the system |
| **Manager** | Sees only clients they are assigned to manage |

### Summary Cards

| Card | Description |
|------|-------------|
| **Total Clients** | "All" for admins, "managed by you" for managers |
| **Total Requests** | Combined requests across all visible clients |
| **Total Tokens Used** | Combined input + output tokens |
| **Coins Spent** | Combined cost across all visible clients |

### Client Table

| Column | Description |
|--------|------------|
| **Name** | Clickable link to the client detail page |
| **Managers** | Number of users who manage this client |
| **Active** | Green checkmark for active, red X for inactive (deactivated) |
| **Requests** | Total API requests made by this client |
| **Tokens Used** | Total input + output tokens |
| **Coins** | Total coins spent |
| **Created** | Client creation date |

The table is sortable by any column (click headers) and filterable via the search box. Admins can also deactivate clients directly from this view.

## Creating a Client

Only administrators can create new clients:

1. Click **+ New Client**
2. Type a name (e.g., `my-service-app`)
3. Click **Create**

You are redirected to the client detail page for the newly created client.
