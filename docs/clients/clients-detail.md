# Client Detail & Management

The client detail page (`/clients/<id>`) is where clients are configured: managers are assigned, API keys are created, and model access is reviewed.

![Client detail page](/help/img/client-detail.png)

## Stat Cards

Same layout as the Usage page — shows the client's usage and coin pool:

| Card | Description |
|------|-------------|
| **Total Tokens Used** | All input + output tokens |
| **Coins Spent** | Total cost |
| **Coin Pool** | Current balance with progress bar (or "Unlimited" / "not configured") |
| **Coin Refill** | Auto-refill rate and countdown to next refill |

## Managers

Managers are users who can administer a client — creating API keys, assigning graylist consent, and viewing usage.

### Adding a Manager

1. Click **+ Add Manager** (admin-only button)
2. A search modal opens with an autocomplete input
3. Type a user's name or email — matching users appear in a dropdown
4. Use arrow keys to navigate the dropdown, or click a suggestion
5. Click **Add Manager**

Adding a manager records an `EntityManager` row linking the user to this client.

### Removing a Manager

Admins can click **Remove** next to any manager in the table. This deletes the `EntityManager` association.

### Permissions of a Manager

A manager can:

- Create API keys for the client
- Delete (revoke) API keys for the client
- Grant graylist consent on behalf of the client
- View the client's usage on this detail page

Managers **cannot**:

- Add or remove other managers
- Activate or deactivate the client
- View or modify user-level data

## API Keys

This section is identical to the Usage page's API Keys section but scoped to the client entity.

### Creating a Client Key

Managers and admins can create keys for the client:

1. Click **+ New API Key**
2. The key is auto-generated and displayed once
3. Enter a name (e.g., "production key")
4. Click **Save Key**

Note: Key generation uses the `/usage/keys/generate` endpoint, so client keys use the same `sk_...` format as user keys.

### API Keys Table

Same columns and functionality as the user API Keys table:

| Column | Description |
|--------|-------------|
| **Name** | Label you chose |
| **Requests / Tokens / Coins** | Usage tracked on this key |
| **Last Used** | Timestamp of last request |
| **Actions** | Delete button for active keys |

Use the "Show deleted keys" checkbox and the search box to find and filter keys.

## Model Access

Similar to the Usage page's Model Access table, this shows the models this client can access, their usage, and access status. Managers can grant graylist consent for the client directly from this page by clicking "Needs Consent" on a model row.
