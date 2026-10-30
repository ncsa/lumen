"""A background job that really fails shows on the TimescaleDB gauges (#173).

``run_job`` cannot produce this: it runs the job in the calling session and
never writes ``job_stats`` (only the scheduler marks a run's start and end). So
the expected ``refresh:request_counts_hourly`` job is pointed at a procedure
that raises, the scheduler is asked to run it now, and the test waits for the
failure to land in ``job_stats``. That has to be committed for the scheduler to
see it, so this module has a database of its own (``pg_migrated_isolated``).
"""
import logging
import time

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from sqlalchemy import text

from lumen.blueprints.metrics.routes import _timescale_families
from lumen.services import metrics_snapshot
from lumen.services.metrics_snapshot import TimescaleHealthSnapshot
from lumen.services.timescale_health import check

pytestmark = pytest.mark.postgres

JOB = "refresh:request_counts_hourly"
_FAILING_PROC = "lumen_test_failing_job"
_WAIT = 120.0


class _Collector:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def collect(self):
        yield from _timescale_families(self._snapshot)


def _health(engine):
    with engine.connect() as conn:
        return check(conn)


def _sample(body, name):
    prefix = f'{name}{{job="{JOB}"}} '
    return next(float(line[len(prefix):]) for line in body.splitlines() if line.startswith(prefix))


def test_a_failing_job_sets_last_run_failed_and_raises_failures_total(pg_migrated_isolated, caplog):
    engine = pg_migrated_isolated[1]
    before = _health(engine)
    job = before.job(JOB)
    assert job is not None and job.last_run_status != "Failed"
    params = {"id": job.job_id}

    with engine.begin() as conn:
        proc_schema, proc_name = conn.execute(text(
            "SELECT proc_schema, proc_name FROM _timescaledb_config.bgw_job WHERE id = :id"
        ), params).one()
        conn.execute(text(
            f"CREATE FUNCTION public.{_FAILING_PROC}(job_id integer, config jsonb) RETURNS void "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'deliberate job failure (#173)'; END $$"
        ))
        conn.execute(text(
            "UPDATE _timescaledb_config.bgw_job SET proc_schema = 'public', proc_name = :proc "
            "WHERE id = :id"
        ), {**params, "proc": _FAILING_PROC})
        conn.execute(text("SELECT alter_job(:id, next_start => now())"), params)
    try:
        with engine.begin() as conn:
            # Starts this database's scheduler if the launcher has not picked it up yet.
            conn.execute(text("SELECT _timescaledb_functions.start_background_workers()"))
        deadline = time.monotonic() + _WAIT
        while True:
            with engine.connect() as conn:
                failures = conn.execute(text(
                    "SELECT total_failures FROM timescaledb_information.job_stats WHERE job_id = :id"
                ), params).scalar()
            if (failures or 0) > (job.total_failures or 0):
                break
            assert time.monotonic() < deadline, f"the scheduler did not run job {job.job_id} in {_WAIT}s"
            time.sleep(0.5)
    finally:
        with engine.begin() as conn:
            # Unscheduled first, so no retry runs the restored policy and clears the status.
            conn.execute(text("SELECT alter_job(:id, scheduled => false)"), params)
            conn.execute(text(
                "UPDATE _timescaledb_config.bgw_job SET proc_schema = :schema, proc_name = :proc "
                "WHERE id = :id"
            ), {**params, "schema": proc_schema, "proc": proc_name})

    after = _health(engine)
    failed = after.job(JOB)
    assert failed is not None, "the restored job must match its expected name again"
    assert failed.last_run_status == "Failed"
    assert failed.total_failures > (job.total_failures or 0)

    def scrape(health):
        registry = CollectorRegistry()
        registry.register(_Collector(TimescaleHealthSnapshot(health, time.monotonic())))
        return generate_latest(registry).decode()

    body_before, body_after = scrape(before), scrape(after)
    assert _sample(body_before, "lumen_timescaledb_job_last_run_failed") == 0
    assert _sample(body_after, "lumen_timescaledb_job_last_run_failed") == 1
    assert _sample(body_after, "lumen_timescaledb_job_failures_total") \
        > _sample(body_before, "lumen_timescaledb_job_failures_total")

    with caplog.at_level(logging.WARNING, logger=metrics_snapshot.logger.name):
        metrics_snapshot._log_health_changes(before, after)
    assert any(f"Background job {JOB} (job {job.job_id}) failed its last run" in r.getMessage()
               for r in caplog.records)
