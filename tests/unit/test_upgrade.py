"""``flask upgrade``: the one-shot upgrade step, on SQLite.

The migrations are PostgreSQL-only (``migrations/env.py`` refuses SQLite), so here
they are stubbed and these tests cover the order of the steps and the SQLite skip.
``tests/integration/test_upgrade_postgres.py`` runs the real thing.
"""
import shlex

import pytest
from sqlalchemy.exc import DBAPIError

from lumen import commands
from lumen.commands import upgrade_cmd
from lumen.extensions import db

BEHIND_LINES = (
    "WARNING: TimescaleDB extension is 2.26.0 but the server has 2.31.0. Back up the "
    "database, then run as the first command of a fresh session:",
    "  psql -X -U lumen -d lumen -c 'ALTER EXTENSION timescaledb UPDATE;'",
    "  Compose: docker compose exec postgres psql -X -U lumen -d lumen "
    "-c 'ALTER EXTENSION timescaledb UPDATE;'",
    "  Helm (bundled PostgreSQL): kubectl exec -it <fullname>-postgresql-0 -- "
    "psql -X -U lumen -d lumen -c 'ALTER EXTENSION timescaledb UPDATE;'",
    'See "Upgrading TimescaleDB" in docs/admin/commands.md.',
)


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
    monkeypatch.setattr(commands, "_warn_if_timescaledb_behind",
                        lambda: calls.append("extension check"))
    monkeypatch.setattr(commands, "POST_MIGRATION_STEPS",
                        (lambda: calls.append("first"), lambda: calls.append("second")))
    result = _run(app)
    assert result.exit_code == 0, result.output
    assert calls == ["migrate", "extension check", "first", "second"]
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


# The TimescaleDB extension version check: on SQLite with the catalog reads stubbed.
# tests/integration/test_upgrade_postgres.py runs it against the real catalog.

def _as_postgres(monkeypatch, versions, owner=("lumen", "lumen")):
    """Run the check as if on PostgreSQL, with ``versions`` as (installed, available)."""
    monkeypatch.setattr(commands, "migrate_upgrade", lambda: None)
    monkeypatch.setattr(commands, "POST_MIGRATION_STEPS", ())
    monkeypatch.setattr(db.engine.dialect, "name", "postgresql")

    def read_versions(session):
        if isinstance(versions, Exception):
            raise versions
        return versions

    monkeypatch.setattr(commands, "extension_versions", read_versions)
    monkeypatch.setattr(commands, "_extension_owner_and_database", lambda session: owner)


def test_extension_behind_prints_the_exact_commands(app, monkeypatch):
    with app.app_context():
        _as_postgres(monkeypatch, ("2.26.0", "2.31.0"))
        result = _run(app)
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    start = lines.index(BEHIND_LINES[0])
    assert tuple(lines[start:start + len(BEHIND_LINES)]) == BEHIND_LINES
    assert lines[-1] == "Upgrade complete."


def test_the_commands_name_the_extension_owner_and_database(app, monkeypatch):
    with app.app_context():
        _as_postgres(monkeypatch, ("2.26.0", "2.31.0"), owner=("postgres", "prod"))
        result = _run(app)
    assert result.exit_code == 0, result.output
    assert "  psql -X -U postgres -d prod -c 'ALTER EXTENSION timescaledb UPDATE;'" \
        in result.output.splitlines()


@pytest.mark.parametrize(("owner", "database"), [
    ("DB Admin", "lumen prod"),                 # whitespace
    ("o'brien", "it's"),                        # apostrophes
    ("x; rm -rf ~", "$(id)`id`|&><*?\\\"!"),  # shell metacharacters
])
def test_each_name_stays_one_literal_shell_argument(app, monkeypatch, owner, database):
    with app.app_context():
        _as_postgres(monkeypatch, ("2.26.0", "2.31.0"), owner=(owner, database))
        result = _run(app)
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    start = lines.index(BEHIND_LINES[0])
    psql_line, compose_line, helm_line = lines[start + 1:start + 4]
    commands_shown = [psql_line.removeprefix("  "),
                      compose_line.removeprefix("  Compose: "),
                      helm_line.removeprefix("  Helm (bundled PostgreSQL): ")]
    assert compose_line.startswith("  Compose: ")
    assert helm_line.startswith("  Helm (bundled PostgreSQL): ")
    expected_psql = ["psql", "-X", "-U", owner, "-d", database,
                     "-c", "ALTER EXTENSION timescaledb UPDATE;"]
    assert shlex.split(commands_shown[0]) == expected_psql
    assert shlex.split(commands_shown[1]) == ["docker", "compose", "exec", "postgres", *expected_psql]
    assert shlex.split(commands_shown[2]) == [
        "kubectl", "exec", "-it", "<fullname>-postgresql-0", "--", *expected_psql]


@pytest.mark.parametrize("versions", [
    ("2.31.0", "2.31.0"),  # up to date
    (None, "2.31.0"),      # extension not installed
    (None, None),          # not available on the server at all
])
def test_nothing_printed_unless_the_extension_is_behind(app, monkeypatch, versions):
    with app.app_context():
        _as_postgres(monkeypatch, versions)
        result = _run(app)
    assert result.exit_code == 0, result.output
    assert result.output == "Applying migrations ...\nUpgrade complete.\n"


def test_sqlite_prints_nothing_for_the_extension(app, monkeypatch):
    monkeypatch.setattr(commands, "migrate_upgrade", lambda: None)
    monkeypatch.setattr(commands, "POST_MIGRATION_STEPS", ())

    def unexpected(session):
        raise AssertionError("the extension check must not query SQLite")

    monkeypatch.setattr(commands, "extension_versions", unexpected)
    result = _run(app)
    assert result.exit_code == 0, result.output
    assert result.output == "Applying migrations ...\nUpgrade complete.\n"


def test_a_catalog_error_is_a_note_and_the_upgrade_goes_on(app, monkeypatch):
    rollbacks, steps = [], []
    with app.app_context():
        _as_postgres(monkeypatch, DBAPIError("SELECT ...", {}, Exception("permission denied")))
        monkeypatch.setattr(commands, "POST_MIGRATION_STEPS", (lambda: steps.append("step"),))
        real_rollback = db.session.rollback
        monkeypatch.setattr(db.session, "rollback",
                            lambda: (rollbacks.append(True), real_rollback()))
        result = _run(app)
    assert result.exit_code == 0, result.output
    notes = [line for line in result.output.splitlines() if line.startswith("Note:")]
    assert notes == ["Note: could not check the TimescaleDB extension version: permission denied"]
    assert "WARNING:" not in result.output
    assert rollbacks
    assert steps == ["step"]
    assert "Upgrade complete." in result.output
