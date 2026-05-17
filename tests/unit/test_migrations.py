"""Sanity checks on the Alembic migration graph (no database required)."""
from alembic.config import Config
from alembic.script import ScriptDirectory


def _script_dir():
    cfg = Config("migrations/alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    return ScriptDirectory.from_config(cfg)


def test_single_migration_head():
    heads = _script_dir().get_heads()
    assert len(heads) == 1, f"Expected 1 migration head, got {len(heads)}: {heads}"
