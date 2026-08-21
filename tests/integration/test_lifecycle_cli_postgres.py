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

These commands mutate state that is global to the database — they add and remove
retention policies and they move every aggregate's watermark — so this module runs
against ``pg_migrated_isolated`` rather than the shared ``pg_migrated``.

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


def test_enable_retention_dry_run_adds_no_policy(pg_url, lifecycle_db):
    """The default is a report. Nothing about the database may change."""
    result = _run(pg_url, "enable-retention")
    assert "Retention window: 13 months" in result.stdout, result.stdout + result.stderr
    assert "rows that would eventually be dropped" in result.stdout
    assert "request_counts_hourly: earliest bucket" in result.stdout
    assert _retention_drop_after(lifecycle_db) is None, "dry run added a retention policy"


def test_enable_retention_dry_run_reports_and_exits_clean_once_backfilled(pg_url, recent_row):
    if not _aggregate_exists(recent_row, ENTITY_AGGREGATE):
        pytest.skip(f"{ENTITY_AGGREGATE} migration is not on disk yet")

    backfill = _run(pg_url, "backfill-aggregate", "--from", _months_ago(1))
    assert backfill.returncode == 0, backfill.stdout + backfill.stderr

    result = _run(pg_url, "enable-retention")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert f"{ENTITY_AGGREGATE} covers request_logs back to" in result.stdout
    assert "Dry run: no policy added." in result.stdout
    assert _retention_drop_after(recent_row) is None, "dry run added a retention policy"


def test_enable_retention_force_refuses_without_a_backfilled_aggregate(pg_url, lifecycle_db):
    """Retention over an empty entity aggregate destroys history held nowhere else."""
    result = _run(pg_url, "enable-retention", "--force")
    assert result.returncode != 0, result.stdout + result.stderr
    if _aggregate_exists(lifecycle_db, ENTITY_AGGREGATE):
        assert "is empty" in result.stdout
        assert "flask backfill-aggregate" in result.stdout
    else:
        assert "does not exist" in result.stdout
    assert _retention_drop_after(lifecycle_db) is None, "a refused run added a retention policy"


def test_enable_retention_force_refuses_an_aggregate_that_was_never_backfilled(pg_url, recent_row):
    """Non-empty, but holding only the last 30 days — the state a COUNT(*) guard cannot see.

    ``request_counts_hourly_by_entity`` is created WITH NO DATA yet has
    ``materialized_only = false`` and an hourly refresh policy with a 30-day
    ``start_offset``, so within an hour of deploy ordinary traffic makes it non-empty
    while no historical backfill has ever run. That is precisely the state in which
    enabling retention destroys history: the aggregate holds 30 days, raw holds
    everything, and retention deletes the difference — which lives in no aggregate.
    Reproduced by refreshing only the recent window, which is what the policy job does.

    The sibling test on an empty database cannot catch a guard weakened back to
    "is it empty?", because an empty aggregate fails every guard. This one can.
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

    result = _run(pg_url, "enable-retention", "--force")
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "does not cover the history retention would drop" in result.stdout, output
    # The refusal has to name both timestamps it compared and the command that fixes it.
    assert "earliest materialised bucket" in result.stdout, result.stdout
    assert "oldest surviving request_logs row" in result.stdout, result.stdout
    assert f"flask backfill-aggregate --from {_months_ago(15)}" in result.stdout, result.stdout
    assert _retention_drop_after(recent_row) is None, "a refused run added a retention policy"


# What TimescaleDB stores as the watermark of a continuous aggregate that has never
# been refreshed: the minimum timestamptz, in microseconds since the epoch. Read off a
# freshly created ``WITH NO DATA`` aggregate on 2.27.2.
_NEVER_REFRESHED_WATERMARK = -210866803200000000


def test_enable_retention_force_refuses_a_never_refreshed_aggregate(pg_url, recent_row):
    """The freshly-migrated state, where reading the *view* would fool the guard.

    ``flask db upgrade`` creates the aggregate WITH NO DATA, so its watermark sits at
    the minimum timestamp and real-time aggregation answers every query from raw. Read
    through the view, ``MIN(bucket)`` is then the oldest raw row and the aggregate looks
    like it covers everything while it holds nothing at all — and this is the state an
    operator is in when they run ``flask db upgrade`` and ``flask enable-retention``
    back to back, which is the whole reason the guard exists. Only the materialisation
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

    result = _run(pg_url, "enable-retention", "--force")
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "is empty" in result.stdout, result.stdout
    assert "flask backfill-aggregate" in result.stdout, result.stdout
    assert _retention_drop_after(recent_row) is None, "a refused run added a retention policy"


def test_enable_retention_warns_when_start_offset_is_wider_than_the_window(pg_url, recent_row):
    """A refresh policy reaching past the retention boundary erases what it recomputes.

    ``request_counts_hourly_by_entity`` refreshes 30 days back, so any window narrower
    than that leaves the scheduled job reaching into dropped chunks — the same contract
    (d) erasure the backfill refusal exists for, except nobody typed a command.
    """
    if not _aggregate_exists(recent_row, ENTITY_AGGREGATE):
        pytest.skip(f"{ENTITY_AGGREGATE} migration is not on disk yet")

    backfill = _run(pg_url, "backfill-aggregate", "--from", _months_ago(1))
    assert backfill.returncode == 0, backfill.stdout + backfill.stderr

    result = _run(pg_url, "enable-retention", "--window", "7 days")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "WARNING: start_offset" in result.stdout, result.stdout
    assert "wider than the retention window (7 days)" in result.stdout, result.stdout
    assert _retention_drop_after(recent_row) is None, "a dry run added a retention policy"


def test_enable_retention_says_when_an_existing_policy_ignores_the_window(pg_url, recent_row):
    """--window against an existing policy changes nothing, and must say so."""
    with _autocommit(recent_row) as conn:
        conn.execute(text(
            "SELECT add_retention_policy('request_logs', drop_after => INTERVAL '13 months')"
        ))

    result = _run(pg_url, "enable-retention", "--window", "6 months", "--force")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "already has a retention policy" in result.stdout
    assert "'6 months' was NOT applied" in result.stdout, result.stdout
    assert _retention_drop_after(recent_row) == "1 year 1 mon", "the window was changed"


def test_enable_retention_force_enables_the_policy_after_a_backfill(pg_url, recent_row):
    if not _aggregate_exists(recent_row, ENTITY_AGGREGATE):
        pytest.skip(f"{ENTITY_AGGREGATE} migration is not on disk yet")

    backfill = _run(pg_url, "backfill-aggregate", "--from", _months_ago(1))
    assert backfill.returncode == 0, backfill.stdout + backfill.stderr

    result = _run(pg_url, "enable-retention", "--window", "13 months", "--force")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Retention enabled on request_logs" in result.stdout
    assert _retention_drop_after(recent_row) == "1 year 1 mon"


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
    still there — which the retention guard is what enforces.
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
