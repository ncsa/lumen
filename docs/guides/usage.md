# Usage & API Keys

The **Usage** page (`/usage`) shows your coin balance, spending statistics, model access, and your personal API keys.

![Usage page](/help/img/usage.png)

## Status Cards

At the top of the page, four cards summarize your account:

| Card | Description |
|------|------------|
| Total Tokens Used | All input + output tokens across all models, all time |
| Coins Spent | Total coins (USD) spent across all models, all time |
| Coins Available | Your current pool balance with a progress bar (if limits are set) |
| Coin Refill | Auto-refill rate per hour, with countdown to next refill |

Your wallet is the default coin pool — a per-user `EntityBalance` record that tracks coins. Coins map to cost in USD based on each model's configured rates (`input_cost_per_million` and `output_cost_per_million`). Auto-refill runs as a background service.

## Coin Pool Indicators

| Value | Meaning |
|-------|---------|
| Positive number | You have coins available, up to the configured limit |
| **Unlimited** | No budget cap (value of -2 in `max`) |
| **Not configured** | No pool configured for this entity |
| 0 or negative (but not -1/-2) | Budget exhausted; blocked from further usage until refill or admin grant |

## Web Chat Usage

Below the stat cards, a one-row table summarizes your web chat usage:

- **Conversations** — Number of conversations started
- **Requests** — Total chat requests sent through the web interface
- **Tokens** — Total tokens (input + output via web chat)
- **Coins** — Total coins spent on web chat
- **Last Used** — Timestamp of your most recent web chat session

## Model Access

The Model Access table shows every model configured in `config.yaml` and your access status for each.

| Column | Description |
|--------|------------|
| **Model** | Clickable link to the model detail page |
| **Requests / Tokens / Coins / Last Used** | Your usage stats for that model across all interfaces |
| **Access** | Your current access level (see below) |
| **Status** | Health of the model's backend endpoints |

### Access Column

| Badge | Meaning |
|-------|---------|
| **Need Consent** (warning button) | Model is graylisted for you — click to acknowledge |
| **Consented** (success) | You've acknowledged and can use the model |
| **Allowed** (success) | Model is whitelisted for you |
| **Blocked** (danger) | Model is blacklisted — you cannot use it |

### Status Column

| Badge | Meaning |
|-------|---------|
| **ok** (green) | All endpoints healthy |
| **degraded** (yellow) | Some endpoints are down |
| **down** (red) | No healthy endpoints |
| **disabled** (gray) | Model is disabled in config |

The table supports sorting (click column headers) and filtering (search box + "Show disabled" checkbox).

## API Keys

API keys let you use Lumen's OpenAI-compatible API (`/v1/`) for programmatic access.

### What an API Key Is

Each key is a `sk_...` token hashed with HMAC-SHA256 in the database. The plaintext is shown only once — upon creation — so it must be copied immediately. A key is tied to an entity (user or client) and tracks requests, input tokens, output tokens, and cumulative cost.

### Creating a Key

1. Click **+ New API Key** at the top of the API Keys section.
2. A modal opens. The key is generated immediately and displayed in a read-only field.
3. **Copy the key now** — it will never be shown again.
4. Enter a name for the key (e.g., "My App").
5. Click **Save Key**. The page reloads and the key appears in the table.

### Viewing Your Keys

The API Keys table is sortable by name, requests, tokens, cost, and last used. Clicking "Show deleted keys" reveals soft-deleted (revoked) keys with strikethrough styling.

Each row shows:

| Column | Description |
|--------|------------|
| **Name** | The label you chose |
| **Hint** | First 4 + last 4 characters (hover to see full hint) |
| **Requests** | Total API requests made with this key |
| **Tokens** | Total input + output tokens |
| **Coins** | Cumulative cost |
| **Last Used** | Timestamp of last API call |
| **Actions** | Delete button for active keys |

### Revoking a Key

Click **Delete** on any active key. This soft-deletes it — usage history is preserved and displayed if you check "Show deleted keys." A disabled key cannot be used for API calls.
