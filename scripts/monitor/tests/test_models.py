from sqlalchemy import CheckConstraint

from monitor.models import DailyStat

# The literal migration 0001 creates; migrations never import the live models.
MIGRATION_0001_SOURCE_CHECK = "source IN ('models.dev', 'lumen', 'hardware')"


def test_source_check_matches_migration_0001():
    checks = {c.name: str(c.sqltext) for c in DailyStat.__table__.constraints if isinstance(c, CheckConstraint)}
    assert checks == {"ck_daily_stats_source": MIGRATION_0001_SOURCE_CHECK}
