"""The Phase 8 operator commands, run as an operator runs them.

These are subprocess tests on purpose. ``backfill-aggregate`` exists because
``CALL refresh_continuous_aggregate`` cannot run inside a transaction block and a
``flask`` command using ``db.session`` is already in one; the only way to prove the
command opens its own AUTOCOMMIT connection is to run the real command against a
real TimescaleDB and see it succeed. A version that "looks right" fails with
``cannot run inside a transaction block`` in the middle of a maintenance window,
which is exactly the failure these tests exist to catch — so they invoke
``uv run flask ...`` the same way ``tests/integration/conftest.py`` invokes
``flask db upgrade``.

These commands mutate state that is global to the database — they move every
aggregate's watermark, and some tests add a retention policy by hand — so this
module runs against ``pg_migrated_isolated`` rather than the shared ``pg_migrated``.

``request_counts_hourly_by_entity`` is created by a migration owned by another
change; the tests that need it skip cleanly while it is absent.
"""

import os
import subprocess
import threading
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from tests.integration.conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres

ENTITY_AGGREGATE = "request_counts_hourly_by_entity"
# request_logs.source is VARCHAR(8); this tags the rows these tests insert so the
# cleanup fixture can remove exactly them between tests.
SOURCE = "clitest"


def _run(pg_url, *args):
    env = {
        **os.environ,
        "DATABASE_URL": pg_url,
        "CONFIG_YAML": TEST_CONFIG,
        "BACKGROUND_WORKER": "false",
    }
    return subprocess.run(
        ["uv", "run", "flask", "--app", "run", *args],
        capture_output=True, text=True, env=env, timeout=300,
    )


def _autocommit(engine):
    return engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def _retention_drop_after(engine):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT config->>'drop_after' FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_retention' AND hypertable_name = 'request_logs'"
        )).scalar()


def _aggregate_exists(engine, name):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT COUNT(*) FROM timescaledb_information.continuous_aggregates "
            "WHERE view_name = :v"
        ), {"v": name}).scalar() == 1


def _months_ago(n):
    """A YYYY-MM string at least ``n`` months back, for --from."""
    now = datetime.now(timezone.utc)
    total = now.year * 12 + (now.month - 1) - n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


@pytest.fixture(scope="module")
def pg_url(pg_migrated_isolated):
    return pg_migrated_isolated[0]


@pytest.fixture(scope="module")
def pg_migrated(pg_migrated_isolated):
    return pg_migrated_isolated[1]


@pytest.fixture
def lifecycle_db(pg_migrated):
    """Leave the database exactly as this test found it.

    Every test here asserts on unqualified aggregate totals and on whether a
    retention policy exists, so one test's rows and policies would fail the next.
    Raw rows are deleted and every aggregate is then recomputed over the window
    they occupied, which drops their materialised rows. The window is bounded and
    ends at ``now()`` on purpose: ``NULL, NULL`` would move each aggregate's
    watermark past the current bucket and disable real-time aggregation for the
    tests that run after this one.
    """
    yield pg_migrated
    with _autocommit(pg_migrated) as conn:
        if conn.execute(text(
            "SELECT COUNT(*) FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_retention' AND hypertable_name = 'request_logs'"
        )).scalar():
            conn.execute(text("SELECT remove_retention_policy('request_logs')"))
        conn.execute(text("DELETE FROM request_logs WHERE source = :s"), {"s": SOURCE})
        for view in conn.execute(text(
            "SELECT view_name FROM timescaledb_information.continuous_aggregates"
        )).scalars().all():
            conn.execute(text(
                f"CALL refresh_continuous_aggregate('{view}', now() - INTERVAL '30 days', now())"
            ))


@pytest.fixture
def recent_row(lifecycle_db):
    with lifecycle_db.begin() as conn:
        conn.execute(text(
            "INSERT INTO request_logs (time, source, input_tokens, output_tokens, cost, duration) "
            "VALUES (now() - INTERVAL '3 days', :s, 11, 22, 0.25, 1.0)"
        ), {"s": SOURCE})
    return lifecycle_db


def _insert_row(engine, age):
    """One request_logs row ``age`` old (a PostgreSQL interval literal), tagged for cleanup."""
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO request_logs (time, source, input_tokens, output_tokens, cost, duration) "
            f"VALUES (now() - INTERVAL '{age}', :s, 1, 1, 0.01, 0.1)"
        ), {"s": SOURCE})


def test_refresh_inside_a_transaction_block_is_rejected(pg_migrated):
    """The premise of the whole command: this is what db.session would do.

    If a future TimescaleDB ever allows the CALL inside a transaction this test
    fails, and the AUTOCOMMIT connection can be reconsidered deliberately rather
    than removed on a hunch.
    """
    with pytest.raises(Exception) as exc:
        with pg_migrated.begin() as conn:
            conn.execute(text(
                "CALL refresh_continuous_aggregate('request_counts_hourly', NULL, NULL)"
            ))
    assert "transaction block" in str(exc.value)


def test_backfill_aggregate_runs_outside_a_transaction_block(pg_url, recent_row):
    """Fails if the command is ever changed to refresh through ``db.session``."""
    result = _run(pg_url, "backfill-aggregate",
                  "--name", "request_counts_hourly", "--from", _months_ago(1))
    output = result.stdout + result.stderr
    assert "cannot run inside a transaction block" not in output, output
    assert result.returncode == 0, output
    assert "request_logs rows" in result.stdout, output
    assert "Backfill complete:" in result.stdout, output

    with recent_row.connect() as conn:
        materialised = conn.execute(text(
            "SELECT SUM(requests) FROM request_counts_hourly"
        )).scalar()
    assert materialised == 1


def test_backfill_aggregate_refuses_window_before_retention_boundary(pg_url, recent_row):
    with _autocommit(recent_row) as conn:
        conn.execute(text(
            "SELECT add_retention_policy('request_logs', drop_after => INTERVAL '13 months')"
        ))

    result = _run(pg_url, "backfill-aggregate",
                  "--name", "request_counts_hourly", "--from", _months_ago(15))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "refusing to refresh" in result.stdout
    assert "EMPTY and DELETES" in result.stdout
    # The policy was added a moment ago and its job has not run, so the chunks are still
    # there. A refusal that says they "are gone" is one an operator knows to be false.
    assert "may already have been dropped" in result.stdout, result.stdout
    assert "are gone" not in result.stdout, result.stdout


def test_backfill_aggregate_force_overrides_the_retention_refusal(pg_url, recent_row):
    with _autocommit(recent_row) as conn:
        conn.execute(text(
            "SELECT add_retention_policy('request_logs', drop_after => INTERVAL '13 months')"
        ))

    result = _run(pg_url, "backfill-aggregate", "--name", "request_counts_hourly",
                  "--from", _months_ago(15), "--force")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "WARNING" in result.stdout
    assert "outside retention" in result.stdout
    assert "Backfill complete:" in result.stdout


def test_backfill_force_erases_the_materialised_rows_it_warns_about(pg_url, recent_row):
    """--force is only worth a flag if it really destroys; prove it on real dropped chunks.

    Contract (d): refreshing a window whose raw chunks are gone recomputes it as empty
    and DELETES the materialised rows, silently and with no error. The refusal exists to
    stop that happening by accident and --force to allow it deliberately, so the test
    seeds a materialised row *before* the retention boundary, drops its chunk the way the
    retention job eventually would, and asserts the row is gone afterwards. Asserting on
    the warning text alone cannot see the difference between erasing those months and
    silently skipping them — a run with nothing before the boundary prints the same words.
    """
    _insert_row(recent_row, "15 months")
    first = _run(pg_url, "backfill-aggregate", "--name", "request_counts_hourly",
                 "--from", _months_ago(15))
    assert first.returncode == 0, first.stdout + first.stderr

    def rows_before_boundary():
        with recent_row.connect() as conn:
            return conn.execute(text(
                "SELECT COUNT(*) FROM request_counts_hourly "
                "WHERE bucket < now() - INTERVAL '13 months'"
            )).scalar()

    assert rows_before_boundary() == 1, "the backfill did not materialise the pre-boundary row"

    with _autocommit(recent_row) as conn:
        conn.execute(text(
            "SELECT add_retention_policy('request_logs', drop_after => INTERVAL '13 months')"
        ))
        # What the retention job does an hour later. Without it the raw rows are still
        # there and the refresh recomputes them intact, so nothing would be erased.
        conn.execute(text(
            "SELECT drop_chunks('request_logs', older_than => INTERVAL '13 months')"
        ))

    result = _run(pg_url, "backfill-aggregate", "--name", "request_counts_hourly",
                  "--from", _months_ago(15), "--force")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "their 1 materialised rows will be erased" in result.stdout, result.stdout
    assert rows_before_boundary() == 0, "--force did not erase the rows it warned about"
    with recent_row.connect() as conn:
        surviving = conn.execute(text(
            "SELECT SUM(requests) FROM request_counts_hourly"
        )).scalar()
    assert surviving == 1, "the in-retention row was erased along with the old ones"


def test_backfill_defaults_from_to_the_month_of_the_oldest_row(pg_url, recent_row):
    """No --from: the first month comes from MIN(time), truncated to its month.

    Every other PostgreSQL test here passes --from, so the default path — the
    ``date_trunc('month', MIN(time) AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'`` round trip —
    would otherwise only ever run on SQLite, which returns before reaching it.
    """
    _insert_row(recent_row, "2 months")
    result = _run(pg_url, "backfill-aggregate", "--name", "request_counts_hourly")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert f"from {_months_ago(2)} to {_months_ago(0)}" in result.stdout, result.stdout
    with recent_row.connect() as conn:
        materialised = conn.execute(text(
            "SELECT SUM(requests) FROM request_counts_hourly"
        )).scalar()
    assert materialised == 2, "the default --from did not reach the oldest row"


def test_backfill_on_an_empty_request_logs_exits_clean(pg_url, lifecycle_db):
    """The MIN(time) IS NULL branch of the default --from, on PostgreSQL."""
    result = _run(pg_url, "backfill-aggregate", "--name", "request_counts_hourly")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "request_logs is empty; nothing to backfill." in result.stdout, output


def _job_count(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM timescaledb_information.jobs")).scalar()


def test_check_aggregate_passes_once_backfilled_and_adds_no_jobs(pg_url, recent_row):
    if not _aggregate_exists(recent_row, ENTITY_AGGREGATE):
        pytest.skip(f"{ENTITY_AGGREGATE} migration is not on disk yet")

    backfill = _run(pg_url, "backfill-aggregate", "--from", _months_ago(1))
    assert backfill.returncode == 0, backfill.stdout + backfill.stderr
    jobs = _job_count(recent_row)

    result = _run(pg_url, "check-aggregate")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "earliest materialised bucket:" in result.stdout, output
    assert "oldest request_logs row:" in result.stdout, output
    assert f"{ENTITY_AGGREGATE} covers request_logs." in result.stdout, output
    assert _job_count(recent_row) == jobs, "check-aggregate added a job"
    assert _retention_drop_after(recent_row) is None, "check-aggregate added a retention policy"


def test_check_aggregate_on_an_empty_request_logs_exits_clean(pg_url, lifecycle_db):
    result = _run(pg_url, "check-aggregate")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "request_logs is empty; nothing to cover." in result.stdout, output


def test_check_aggregate_fails_on_an_aggregate_that_was_never_backfilled(pg_url, recent_row):
    """Non-empty, but holding only the last 30 days — the state a COUNT(*) check cannot see.

    The aggregate is created WITH NO DATA but has an hourly refresh policy with a
    30-day ``start_offset``, so ordinary traffic makes it non-empty within an hour of
    deploy while no historical backfill has ever run. Reproduced by refreshing only the
    recent window, which is what the policy job does.
    """
    if not _aggregate_exists(recent_row, ENTITY_AGGREGATE):
        pytest.skip(f"{ENTITY_AGGREGATE} migration is not on disk yet")

    _insert_row(recent_row, "15 months")
    with _autocommit(recent_row) as conn:
        conn.execute(text(
            f"CALL refresh_continuous_aggregate('{ENTITY_AGGREGATE}', "
            f"now() - INTERVAL '30 days', now())"
        ))
        holds = conn.execute(text(f'SELECT COUNT(*) FROM "{ENTITY_AGGREGATE}"')).scalar()
    assert holds > 0, "the aggregate must be non-empty here or this test proves nothing"

    result = _run(pg_url, "check-aggregate")
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert f"'{ENTITY_AGGREGATE}' does not cover request_logs" in result.stdout, output
    assert f"flask backfill-aggregate --from {_months_ago(15)}" in result.stdout, output


# What TimescaleDB stores as the watermark of a continuous aggregate that has never
# been refreshed: the minimum timestamptz, in microseconds since the epoch. Read off a
# freshly created ``WITH NO DATA`` aggregate on 2.27.2.
_NEVER_REFRESHED_WATERMARK = -210866803200000000


def test_check_aggregate_fails_on_an_empty_aggregate_the_view_hides(pg_url, recent_row):
    """The freshly-migrated state, where reading the *view* would fool the check.

    ``flask db upgrade`` creates the aggregate WITH NO DATA, so its watermark sits at
    the minimum timestamp and real-time aggregation answers every query from raw. Read
    through the view, ``MIN(bucket)`` is then the oldest raw row and the aggregate looks
    like it covers everything while it holds nothing. Only the materialisation
    hypertable distinguishes the two. Reproduced by putting the watermark back where the
    migration left it; the cleanup fixture's refresh moves it forward again.
    """
    if not _aggregate_exists(recent_row, ENTITY_AGGREGATE):
        pytest.skip(f"{ENTITY_AGGREGATE} migration is not on disk yet")

    _insert_row(recent_row, "15 months")
    with _autocommit(recent_row) as conn:
        schema, table = conn.execute(text(
            "SELECT materialization_hypertable_schema, materialization_hypertable_name "
            "FROM timescaledb_information.continuous_aggregates WHERE view_name = :v"
        ), {"v": ENTITY_AGGREGATE}).one()
        conn.execute(text(f'DELETE FROM "{schema}"."{table}"'))
        conn.execute(text(
            "UPDATE _timescaledb_catalog.continuous_aggs_watermark SET watermark = :w "
            "WHERE mat_hypertable_id = (SELECT id FROM _timescaledb_catalog.hypertable "
            "WHERE schema_name = :s AND table_name = :t)"
        ), {"w": _NEVER_REFRESHED_WATERMARK, "s": schema, "t": table})
        through_the_view = conn.execute(text(
            f'SELECT MIN(bucket) FROM "{ENTITY_AGGREGATE}"'
        )).scalar()
    assert through_the_view is not None and \
        through_the_view.astimezone(timezone.utc).strftime("%Y-%m") == _months_ago(15), \
        "the view must look fully covered here or this test proves nothing"

    result = _run(pg_url, "check-aggregate")
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "earliest materialised bucket: none" in result.stdout, output
    assert f"'{ENTITY_AGGREGATE}' is empty" in result.stdout, output
    assert f"flask backfill-aggregate --from {_months_ago(15)}" in result.stdout, output


def test_check_aggregate_fails_when_request_logs_has_a_retention_policy(pg_url, recent_row):
    """A policy added by hand in SQL is reported even when the aggregate is covered."""
    if not _aggregate_exists(recent_row, ENTITY_AGGREGATE):
        pytest.skip(f"{ENTITY_AGGREGATE} migration is not on disk yet")

    backfill = _run(pg_url, "backfill-aggregate", "--from", _months_ago(1))
    assert backfill.returncode == 0, backfill.stdout + backfill.stderr
    with _autocommit(recent_row) as conn:
        conn.execute(text(
            "SELECT add_retention_policy('request_logs', drop_after => INTERVAL '13 months')"
        ))

    result = _run(pg_url, "check-aggregate")
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "request_logs has a retention policy" in result.stdout, output
    assert "SELECT remove_retention_policy('request_logs');" in result.stdout, output
    assert f"{ENTITY_AGGREGATE} covers request_logs." in result.stdout, output
    assert _retention_drop_after(recent_row) == "1 year 1 mon", "check-aggregate changed the policy"


def _retention_jobs(engine):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT job_id, config::text FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_retention' AND hypertable_name = 'request_logs'"
        )).all()


def test_check_aggregate_fails_on_a_drop_created_before_retention_policy(pg_url, recent_row):
    """A policy keyed on drop_created_before stores no drop_after, and must still fail."""
    if not _aggregate_exists(recent_row, ENTITY_AGGREGATE):
        pytest.skip(f"{ENTITY_AGGREGATE} migration is not on disk yet")

    backfill = _run(pg_url, "backfill-aggregate", "--from", _months_ago(1))
    assert backfill.returncode == 0, backfill.stdout + backfill.stderr
    with _autocommit(recent_row) as conn:
        conn.execute(text(
            "SELECT add_retention_policy('request_logs', drop_created_before => INTERVAL '13 months')"
        ))
    before = _retention_jobs(recent_row)
    assert len(before) == 1 and _retention_drop_after(recent_row) is None, \
        "the policy must lack drop_after here or this test proves nothing"

    result = _run(pg_url, "check-aggregate")
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "request_logs has a retention policy" in result.stdout, output
    assert "drop_created_before" in result.stdout, output
    assert "SELECT remove_retention_policy('request_logs');" in result.stdout, output
    assert _retention_jobs(recent_row) == before, "check-aggregate changed the policy"


def test_check_aggregate_fails_when_the_aggregate_does_not_exist(pg_url, lifecycle_db):
    """The state before 'flask db upgrade' has created the aggregate.

    Reproduced by renaming the aggregate out of the way, which removes it from
    timescaledb_information.continuous_aggregates under its own name, and renaming it
    back afterwards whatever the outcome.
    """
    if not _aggregate_exists(lifecycle_db, ENTITY_AGGREGATE):
        pytest.skip(f"{ENTITY_AGGREGATE} migration is not on disk yet")

    hidden = f"{ENTITY_AGGREGATE}_hidden"
    jobs = _job_count(lifecycle_db)
    with _autocommit(lifecycle_db) as conn:
        conn.execute(text(f'ALTER MATERIALIZED VIEW "{ENTITY_AGGREGATE}" RENAME TO "{hidden}"'))
    try:
        assert not _aggregate_exists(lifecycle_db, ENTITY_AGGREGATE), \
            "the aggregate must be absent here or this test proves nothing"
        result = _run(pg_url, "check-aggregate")
    finally:
        with _autocommit(lifecycle_db) as conn:
            conn.execute(text(f'ALTER MATERIALIZED VIEW "{hidden}" RENAME TO "{ENTITY_AGGREGATE}"'))

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert f"'{ENTITY_AGGREGATE}' does not exist" in result.stdout, output
    assert "flask db upgrade" in result.stdout, output
    assert _job_count(lifecycle_db) == jobs, "check-aggregate added a job"
    assert _retention_drop_after(lifecycle_db) is None


def test_enable_retention_is_gone(pg_url, lifecycle_db):
    help_ = _run(pg_url, "--help")
    assert "enable-retention" not in help_.stdout, help_.stdout
    assert "check-aggregate" in help_.stdout, help_.stdout

    result = _run(pg_url, "enable-retention", "--force")
    assert result.returncode != 0
    assert "No such command" in result.stderr, result.stdout + result.stderr
    assert _retention_drop_after(lifecycle_db) is None


def test_backfill_leaves_real_time_aggregation_working_for_the_current_bucket(pg_url, recent_row):
    """The backfill must not hide requests made after it runs.

    Refreshing past ``now`` materialises the current bucket and moves the aggregate's
    watermark beyond it, which switches real-time aggregation off for that bucket: a
    student running requests right after a backfill would see zero on /usage until a
    scheduled refresh caught up. Measured on 2.27.2 — this fails if the month loop
    ever stops clamping its window end to ``now()``.

    Uses the entity aggregate because it is the one the command backfills by
    default; ``request_counts_hourly`` is real-time too since ``j4k5l6m7n8o9``,
    so either would show the loss.
    """
    if not _aggregate_exists(recent_row, ENTITY_AGGREGATE):
        pytest.skip(f"{ENTITY_AGGREGATE} migration is not on disk yet")

    result = _run(pg_url, "backfill-aggregate", "--from", _months_ago(1))
    assert result.returncode == 0, result.stdout + result.stderr

    with recent_row.begin() as conn:
        conn.execute(text(
            "INSERT INTO request_logs (time, source, input_tokens, output_tokens, cost, duration) "
            "VALUES (now(), :s, 1, 1, 0.01, 0.1)"
        ), {"s": SOURCE})
    with recent_row.connect() as conn:
        total = conn.execute(text(
            f"SELECT SUM(requests) FROM {ENTITY_AGGREGATE} WHERE source = :s"
        ), {"s": SOURCE}).scalar()
    assert total == 2, "a request logged after the backfill is invisible in the aggregate"


def _wait_for(predicate, timeout=60.0, interval=0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_backfill_retries_a_concurrent_refresh_instead_of_aborting(pg_url, recent_row):
    """A real SQLSTATE 55P03, not an injected one.

    TimescaleDB refuses an overlapping refresh rather than queueing behind it, and the
    aggregates ship with policies firing every minute and every hour — so a long backfill
    will meet one. Forcing it deterministically: a holder session takes ACCESS EXCLUSIVE
    on the aggregate's materialisation hypertable, then a second session starts a genuine
    refresh, which acquires TimescaleDB's concurrency guard and *then* blocks on the table
    lock. While it sits there, every other refresh — including the CLI's — fails with
    55P03 in about 20ms. The holder is released as soon as the CLI is seen to have tried,
    which lets the blocked refresh finish and the CLI's next retry succeed.

    Without the retry the command exits non-zero partway through the run.
    """
    engine = recent_row
    with engine.connect() as conn:
        mat = conn.execute(text(
            "SELECT materialization_hypertable_schema || '.' || materialization_hypertable_name "
            "FROM timescaledb_information.continuous_aggregates "
            "WHERE view_name = 'request_counts_hourly'"
        )).scalar()

    holder = engine.connect()
    holder.execute(text(f"LOCK TABLE {mat} IN ACCESS EXCLUSIVE MODE"))

    blocked = {}

    def blocked_refresh():
        with _autocommit(engine) as conn:
            try:
                conn.execute(text(
                    "CALL refresh_continuous_aggregate('request_counts_hourly', "
                    "now() - INTERVAL '400 days', now())"
                ))
                blocked["outcome"] = "ok"
            except Exception as exc:
                blocked["outcome"] = repr(exc)

    refresher = threading.Thread(target=blocked_refresh)
    refresher.start()

    def refreshers_running(minimum):
        with engine.connect() as conn:
            return conn.execute(text(
                "SELECT COUNT(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() "
                "AND datname = current_database() "
                "AND query LIKE 'CALL refresh_continuous_aggregate%'"
            )).scalar() >= minimum

    assert _wait_for(lambda: refreshers_running(1), timeout=30), \
        "the blocking refresh never started"

    # Release only once a second backend has attempted a refresh — that is the CLI
    # hitting 55P03. Releasing on a fixed timer would make the test a race.
    releaser = threading.Thread(
        target=lambda: (_wait_for(lambda: refreshers_running(2), timeout=90),
                        holder.rollback()),
        daemon=True,
    )
    releaser.start()
    try:
        result = _run(pg_url, "backfill-aggregate",
                      "--name", "request_counts_hourly", "--from", _months_ago(1))
    finally:
        releaser.join(timeout=120)
        holder.rollback()
        holder.close()
        refresher.join(timeout=120)

    output = result.stdout + result.stderr
    assert blocked.get("outcome") == "ok", blocked
    assert result.returncode == 0, output
    assert "lock retries" in result.stdout, output
    assert "Backfill complete:" in result.stdout, output


def test_backfill_is_idempotent_so_from_can_resume_mid_run(pg_url, recent_row):
    """Re-running an already-refreshed month recomputes it from raw and overwrites.

    That is what makes the resume hint on a failed month safe: an operator can re-run
    from any month without double-counting or losing rows, as long as the raw chunks are
    still there — and they always are: request_logs is never pruned (#167).
    """
    totals = []
    for _ in range(2):
        result = _run(pg_url, "backfill-aggregate",
                      "--name", "request_counts_hourly", "--from", _months_ago(1))
        assert result.returncode == 0, result.stdout + result.stderr
        with recent_row.connect() as conn:
            totals.append(conn.execute(text(
                "SELECT SUM(requests), SUM(input_tokens), SUM(output_tokens) "
                "FROM request_counts_hourly"
            )).one())
    assert totals[0] == totals[1], f"re-running a month changed the aggregate: {totals}"
