# AGENTS.md — scripts/monitor

The root `AGENTS.md` applies here too. Rules specific to the monitor:

- This is a standalone package. Never import from `lumen/`; run everything from
  `scripts/monitor` with uv (`uv sync`, `uv run pytest`, `uv run monitor ...`).
- **Drift caveat:** the models.dev fetch and matcher are a copy of
  `lumen/services/model_sync.py` (`_fetch_modelsdev`, `find_in_modelsdev`, `_average_price`),
  which `sync_models.py` also copies. When one of them changes, update the monitor's copy in
  the same PR, and the other way round.
- All timestamps are naive UTC; get "now" from `monitor.db.utcnow()`. Only `daily_stats.day`
  is a local date, in the configured `timezone`.
- Schema changes need a new Alembic revision in `monitor/migrations/versions/` that works on SQLite
  and PostgreSQL; `tests/test_migrations.py` checks the models and migrations agree. Give
  every table and column a comment.
- New `config.yaml` keys go in `monitor/config.py` with validation and tests, and in
  `config.yaml.example`. The config is re-read while the service runs, so read it from the
  `ConfigStore` rather than caching values.
- Run a single replica: two would double-write probes and double-count totals.
