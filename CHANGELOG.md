# Changelog

All notable changes to Lumen will be documented in this file.

## [Unreleased]

### Security
- API keys are now stored as HMAC-SHA256 hashes in the database; only a short hint (`sk_abcd...xyzw`) is retained for display, so a leaked database backup yields no usable keys
- Disabled user accounts are now immediately blocked from the chat interface; existing sessions are cleared on the next request
- Admin privileges are now re-verified on every request against the current config, so removing an admin from config takes effect immediately without requiring a server restart or logout
- Token refills are now performed exclusively by the background task; removed the on-request lazy refill that could race the background task and grant double tokens

### Changed
- Renamed Python package from `illm` to `lumen` (directory, imports, console script, CSS classes, storage keys)

## [1.0.0] - 2026-03-20

### Added
- Initial production release of **Lumen**, a self-hosted AI chat portal for research institutions
- Web chat interface compatible with OpenAI-compatible endpoints, Ollama, and vLLM
- Federated login via CILogon (institutional identity provider / OAuth2 + OIDC)
- Token budget system — per-user and per-group limits with optional background auto-refresh
- Group management: define groups in `config.yaml`, auto-assign users on login via CILogon attribute rules
- Admin panel for managing users, groups, models, and usage statistics
- Round-robin load balancing across multiple model backends
- Persistent conversation history with optional soft-delete
- Markdown rendering in assistant chat bubbles (XSS-safe)
- Per-model token balance display for users
- API endpoint with usage recording (`/api/...`)
- Model health dashboard with live status and disabled-model indicators
- Hot-reload support for `config.yaml` (app name, tagline, OAuth params, logging settings)
- Illinois Web Toolkit branding (UI colors, Block I logo, `il-blue` palette)
- Docker support with `Dockerfile`, `docker-compose.yml`, and GitHub Actions workflow to publish `ncsa/lumen`
- Configurable app name and tagline via `config.yaml`
- Configurable Werkzeug access log suppression
