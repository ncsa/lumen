# Project Detail

This page is for people managing Lumen-based services and tools. For details on what a project is, start with the [Projects overview](./projects.md).

The project detail page (`/projects/<id>`) is where you manage a specific project: view usage, assign managers, create API keys, and check model access. It is laid out like the [Profile page](../guides/profile.md): a header card at the top, followed by **Managers**, **API Keys**, and **Models** tabs.

![Project detail page](../img/project-detail.png)

## Header Card

The left side of the card shows the project's auto-generated avatar, its name, the creation date, and — for the owner or an admin — the **Deactivate**/**Activate** button. The right side shows the project's activity and budget:

| Card | Description |
|------|-------------|
| **Managers** | Number of users managing this project |
| **Coins Used** | Total coins spent by this project |
| **Tokens Used** | All input + output tokens this project has consumed |
| **Favorite Model** | The model this project has sent the most requests to |
| **Coins Available** | Current balance (or **Unlimited** / not configured) |
| **Refill Rate** | Auto-refill rate and countdown to next refill |

## Managers

Managers are the users responsible for a project. They can create and revoke API keys, grant model consent, and view usage — but they cannot manage other managers or deactivate the project.

One manager can be designated as the **owner**. The owner has all manager powers plus the ability to add/remove managers, transfer ownership, and activate/deactivate the project. Each project has at most one owner. The owner is marked with an **Owner** badge in the managers table.

### Adding a Manager (admin or owner)

1. Click **+ Add Manager**.
2. A search dialog opens. Start typing a user's name or email.
3. Select the user from the dropdown.
4. Click **Add Manager**.

### Removing a Manager (admin or owner)

Click **Remove** next to any manager in the table. The owner cannot be removed directly — you must transfer ownership to another manager first.

### Transferring Ownership (admin or owner)

Click **Make Owner** next to a non-owner manager to transfer ownership. The previous owner becomes a regular manager. If you are the current owner, transferring ownership means you will no longer be able to manage managers or toggle the project.

### What Managers Can Do

| Action | Manager | Owner | Admin |
|--------|---------|-------|-------|
| Create API keys for this project | ✓ | ✓ | ✓ |
| Revoke API keys for this project | ✓ | ✓ | ✓ |
| Grant model consent for this project | ✓ | ✓ | ✓ |
| View usage on this page | ✓ | ✓ | ✓ |
| Add / remove managers | — | ✓ | ✓ |
| Transfer ownership | — | ✓ | ✓ |
| Activate / deactivate the project | — | ✓ | ✓ |

## API Keys

This section works exactly like the API Keys section on the [Profile page](../guides/profile.md#api-keys), but keys here belong to the project, not to your personal account.

### Creating a Key

1. Click **+ New API Key**.
2. The key is generated and shown **once** in a dialog — copy it immediately.
3. Give the key a descriptive name (e.g., `production`, `staging`, `ci-runner`).
4. Click **Save Key**.

Keys follow the same `sk_...` format as personal API keys. Use them exactly the same way in code:

```python
from openai import OpenAI

project = OpenAI(
    api_key="sk_project_key_here",
    base_url="https://your-lumen-instance/v1"
)

response = project.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Summarize this dataset: ..."}]
)
```

### Key Table

| Column | Description |
|--------|-------------|
| **Name** | Label you chose |
| **Hint** | First 4 + last 4 characters for identification |
| **Requests / Tokens / Coins** | Usage tracked on this key |
| **Last Used** | Timestamp of the last API call |
| **Actions** | Revoke button for active keys |

Use **Show deleted keys** to view previously revoked keys. Use the search box to filter.

## Model Access

This table shows which models this project can use and how much it has consumed on each. The columns and access badges are the same as on the [Profile page](../guides/profile.md#model-access).

If a model shows **Needs Consent**, you can click it to grant acknowledgment on behalf of the project, making the model available to all API keys associated with this project.
