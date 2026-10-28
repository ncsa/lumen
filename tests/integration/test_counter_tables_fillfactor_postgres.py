"""``fillfactor = 80`` on the per-request counter tables, as the migration chain sets it.

A storage parameter lives only in ``pg_class.reloptions``, which neither the
SQLite suite nor ``create_all`` ever touches, so only a real PostgreSQL can show
it is set at head and cleared on downgrade. The module steps its private
database below the revision and back up, so it runs against
``pg_migrated_isolated`` and leaves it at head.
"""
import os
import subprocess

import pytest
from sqlalchemy import text

from tests.integration.conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres

_BEFORE = "c2d3e4f5a6b7"
_REVISION = "d3e4f5a6b7c8"
_TABLES = ("entity_balances", "entity_stats", "model_stats", "api_keys")


def _flask_db(url, *args):
    result = subprocess.run(
        ["uv", "run", "flask", "--app", "run", "db", *args],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "DATABASE_URL": url, "CONFIG_YAML": TEST_CONFIG, "BACKGROUND_WORKER": "false"},
    )
    assert result.returncode == 0, (
        f"flask db {' '.join(args)} failed:\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


def _reloptions(engine):
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT relname, reloptions FROM pg_class "
            "WHERE relkind = 'r' AND relname = ANY(:tables) "
            "AND relnamespace = 'public'::regnamespace"
        ), {"tables": list(_TABLES)}).all()
    return {name: options or [] for name, options in rows}


def test_upgrade_sets_fillfactor_and_downgrade_resets_it(pg_migrated_isolated):
    url, engine = pg_migrated_isolated

    at_head = _reloptions(engine)
    assert set(at_head) == set(_TABLES)
    for table, options in at_head.items():
        assert "fillfactor=80" in options, f"{table}: {options}"

    _flask_db(url, "downgrade", _BEFORE)
    engine.dispose()
    for table, options in _reloptions(engine).items():
        assert not any(o.startswith("fillfactor=") for o in options), f"{table}: {options}"
    engine.dispose()

    _flask_db(url, "upgrade", _REVISION)
    for table, options in _reloptions(engine).items():
        assert "fillfactor=80" in options, f"{table}: {options}"
    engine.dispose()
