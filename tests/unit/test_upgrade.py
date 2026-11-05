"""``flask upgrade``: the one-shot upgrade step, on SQLite.

The migrations are PostgreSQL-only (``migrations/env.py`` refuses SQLite), so here
they are stubbed and these tests cover the order of the steps and the SQLite skip.
``tests/integration/test_upgrade_postgres.py`` runs the real thing.
"""
from lumen import commands
from lumen.commands import upgrade_cmd


def _run(app):
    return app.test_cli_runner().invoke(upgrade_cmd, [])


def test_registered_on_the_app(app):
    assert app.cli.commands["upgrade"] is upgrade_cmd


def test_the_post_migration_steps_are_only_the_aggregate_backfill():
    """An explicit list, and never the stats cutover: that command double-counts here."""
    assert commands.POST_MIGRATION_STEPS == (commands._backfill_missing_months,)


def test_migrates_then_runs_the_post_steps_in_order(app, monkeypatch):
    calls = []
    monkeypatch.setattr(commands, "migrate_upgrade", lambda: calls.append("migrate"))
    monkeypatch.setattr(commands, "POST_MIGRATION_STEPS",
                        (lambda: calls.append("first"), lambda: calls.append("second")))
    result = _run(app)
    assert result.exit_code == 0, result.output
    assert calls == ["migrate", "first", "second"]
    assert "Upgrade complete." in result.output


def test_a_failed_migration_runs_no_post_step(app, monkeypatch):
    calls = []

    def failing_migration():
        raise SystemExit(1)  # what Flask-Migrate does on an Alembic CommandError

    monkeypatch.setattr(commands, "migrate_upgrade", failing_migration)
    monkeypatch.setattr(commands, "POST_MIGRATION_STEPS", (lambda: calls.append("step"),))
    result = _run(app)
    assert result.exit_code == 1
    assert calls == []
    assert "Upgrade complete." not in result.output


def test_a_failed_post_step_stops_the_ones_after_it(app, monkeypatch):
    calls = []

    def failing_step():
        calls.append("first")
        raise SystemExit(1)

    monkeypatch.setattr(commands, "migrate_upgrade", lambda: None)
    monkeypatch.setattr(commands, "POST_MIGRATION_STEPS",
                        (failing_step, lambda: calls.append("second")))
    result = _run(app)
    assert result.exit_code == 1
    assert calls == ["first"]


def test_sqlite_skips_the_aggregate_backfill(app, monkeypatch):
    monkeypatch.setattr(commands, "migrate_upgrade", lambda: None)
    result = _run(app)
    assert result.exit_code == 0, result.output
    assert "Aggregate backfill: skipped, needs PostgreSQL/TimescaleDB (database is sqlite)." \
        in result.output
    assert "Upgrade complete." in result.output
