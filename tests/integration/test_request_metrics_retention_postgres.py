"""The 30-day retention policy on ``request_metrics_1m`` (#169), on real TimescaleDB.

The migration chain adds exactly one retention policy, on the minute aggregate,
and never one on ``request_logs`` (#167). ``check-aggregate`` and the startup
tripwire look only for a policy on ``request_logs``, so this one must not trip
them. A retention policy is global to the database and the downgrade test drops
it, so this module runs against ``pg_migrated_isolated``.
"""
import os
import subprocess

import pytest
from sqlalchemy import text

from lumen.commands import _retention_job
from tests.integration.conftest import TEST_CONFIG, flask_db

pytestmark = pytest.mark.postgres

VIEW = "request_metrics_1m"
# The revision just below the one that adds the policy.
BEFORE_RETENTION = "c93d4de8245c"

# The jobs catalog names a continuous aggregate's policy by the view, as
# hypertable_name, not by its materialisation hypertable.
_VIEW_RETENTION = """
    SELECT job_id, config->>'drop_after' FROM timescaledb_information.jobs
    WHERE proc_name = 'policy_retention' AND hypertable_name = :v
"""


@pytest.fixture(scope="module")
def pg_url(pg_migrated_isolated):
    return pg_migrated_isolated[0]


@pytest.fixture(scope="module")
def pg_engine(pg_migrated_isolated):
    return pg_migrated_isolated[1]


def _retention_jobs(engine):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT job_id FROM timescaledb_information.jobs WHERE proc_name = 'policy_retention'"
        )).scalars().all()


def _view_retention(engine):
    with engine.connect() as conn:
        return conn.execute(text(_VIEW_RETENTION), {"v": VIEW}).all()


def test_the_only_retention_policy_keeps_30_days_of_request_metrics_1m(pg_engine):
    view_jobs = _view_retention(pg_engine)
    assert [drop_after for _, drop_after in view_jobs] == ["30 days"], view_jobs
    assert _retention_jobs(pg_engine) == [view_jobs[0][0]], \
        "a migration created a retention policy on something other than request_metrics_1m"


def test_request_logs_has_no_retention_policy(pg_engine):
    with pg_engine.connect() as conn:
        assert _retention_job(conn) is None


def test_check_aggregate_passes_with_the_view_policy_in_place(pg_url, pg_engine):
    assert _view_retention(pg_engine), "the policy must exist or this test proves nothing"
    result = subprocess.run(
        ["uv", "run", "flask", "--app", "run", "check-aggregate"],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "DATABASE_URL": pg_url, "CONFIG_YAML": TEST_CONFIG,
             "BACKGROUND_WORKER": "false"},
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "retention policy" not in result.stdout, output


def test_downgrade_removes_the_policy_and_upgrade_restores_it(pg_url, pg_engine):
    pg_engine.dispose()
    flask_db(pg_url, "downgrade", BEFORE_RETENTION)
    try:
        assert _retention_jobs(pg_engine) == [], "downgrade left a retention policy behind"
    finally:
        pg_engine.dispose()
        flask_db(pg_url, "upgrade")
    assert [d for _, d in _view_retention(pg_engine)] == ["30 days"]
