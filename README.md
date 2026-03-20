# iLLM

A Flask-based LLM gateway that proxies requests to multiple OpenAI-compatible endpoints with token budgeting, usage tracking, and OAuth2 authentication.

## Quick Start

### 1. Configure

Copy the example config and fill in your values:

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml`:
- Set `app.secret_key` to a random string
- Set `app.database_url` (defaults to SQLite)
- Fill in `oauth2.*` credentials (register at https://cilogon.org/oauth2/register)
- Add your model endpoints under `models:`

To use a config file at a different path:
```bash
CONFIG_YAML=/path/to/config.yaml uv run illm
```

### 2. Run (development)

```bash
uv run flask --app run db upgrade
uv run illm
```

Or use the convenience script:
```bash
./dev.sh
```

## CILogon Setup

1. Register your application at https://cilogon.org/oauth2/register
2. Set the redirect URI to `https://your-domain/callback` (or `http://localhost:5000/callback` for development)
3. Request the following scopes: `openid email profile org.cilogon.userinfo`

The `org.cilogon.userinfo` scope is required to receive campus-specific attributes such as:
- `affiliation` — semicolon-separated list (e.g. `staff@illinois.edu;member@illinois.edu`)
- `member_of` — semicolon-separated URNs for campus cluster group memberships
- `idp` — the user's identity provider URN
- `ou` — organizational unit

Without `org.cilogon.userinfo`, any group `rules:` that reference these fields will not match because the fields will be absent from the userinfo response.

### Group auto-assignment rules

Add `rules:` inside a group definition in `config.yaml` to auto-assign users at login:

```yaml
groups:
  aifarms:
    rules:
      - field: member_of
        contains: icc-grp-aifarms   # substring match
  uiuc-staff:
    rules:
      - field: affiliation
        contains: staff@illinois.edu
      - field: idp
        equals: urn:mace:incommon:uiuc.edu   # exact match
```

Each rule tests one CILogon userinfo field. The first matching rule assigns the group; remaining rules are skipped. Groups assigned via rules are marked `config_managed` and removed if the rule no longer matches on next login.

### 3. Run (production)

Production uses uvicorn via `entrypoint.sh` (e.g. in Docker):
```bash
./entrypoint.sh
```
