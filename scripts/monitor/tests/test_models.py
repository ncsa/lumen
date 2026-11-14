import types
from pathlib import Path

from sqlalchemy import CheckConstraint

from monitor import models

# The literal migration 0001 creates; migrations never import the live models.
# This guards one direction only: editing the CHECK in migration 0001 leaves it
# green, and the Postgres round trip compares the constraint by name, not text.
MIGRATION_0001_SOURCE_CHECK = "source IN ('models.dev', 'lumen', 'hardware')"
SOURCES_LINE = 'SOURCES = ("models.dev", "lumen", "hardware")'


def _source_check(model) -> str:
    checks = {c.name: str(c.sqltext) for c in model.__table__.constraints if isinstance(c, CheckConstraint)}
    return checks["ck_daily_stats_source"]


def test_source_check_matches_migration_0001():
    assert _source_check(models.DailyStat) == MIGRATION_0001_SOURCE_CHECK


def test_source_check_is_built_from_sources():
    # Load models.py with a different SOURCES; a hardcoded CHECK would not follow it.
    path = Path(models.__file__)
    code = path.read_text()
    assert code.count(SOURCES_LINE) == 1
    module = types.ModuleType("models_with_other_sources")
    exec(compile(code.replace(SOURCES_LINE, 'SOURCES = ("lumen", "gpu")'), str(path), "exec"), module.__dict__)
    assert _source_check(module.DailyStat) == "source IN ('lumen', 'gpu')"
