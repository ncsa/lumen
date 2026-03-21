# Changelog

All notable changes to Lumen will be documented in this file.

## [Unreleased]

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
