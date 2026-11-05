"""``flask upgrade`` and the per-month ``check-aggregate`` report on real TimescaleDB.

The aggregate's coverage is set up by hand: rows are inserted at chosen times and
``refresh_continuous_aggregate`` is called over chosen windows, the way the hourly
refresh policy or an interrupted backfill would leave it. Every background job in
this module's private database is paused, so the policy schedule never changes the
coverage under a test.

The command runs in-process (as in ``test_reassign_model_postgres.py``) so a test can
make one month's refresh fail on cue and record the window of every refresh. The
empty-database case runs it as a subprocess, as the image's ``upgrade`` mode does.
"""

import logging
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, exc, text

from lumen import commands
from lumen.commands import ENTITY_AGGREGATE, USAGE_AGGREGATES, check_aggregate_cmd, upgrade_cmd
from tests.conftest import no_snapshot_refresher
from tests.unit.test_migrations import _script_dir

from .conftest import PG_URL_ENV, TEST_CONFIG, _admin_engine

pytestmark = pytest.mark.postgres

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def pg_app(pg_migrated_isolated):
    url = pg_migrated_isolated[0]
    previous_env = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    os.environ["CONFIG_YAML"] = TEST_CONFIG
    os.environ["BACKGROUND_WORKER"] = "false"
    from config import Config

    previous_uri = Config.SQLALCHEMY_DATABASE_URI
    Config.SQLALCHEMY_DATABASE_URI = url
    from lumen import create_app

    with pytest.MonkeyPatch.context() as mp:
        no_snapshot_refresher(mp)
        application = create_app()
    Config.SQLALCHEMY_DATABASE_URI = previous_uri
    application.config["TESTING"] = True

    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        # Refresh, compression and retention policies alike: none may move the coverage.
        conn.execute(text(
            "SELECT alter_job(job_id, scheduled => false) FROM timescaledb_information.jobs "
            "WHERE job_id >= 1000"
        ))
    admin.dispose()
    yield application
    if previous_env is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous_env


@pytest.fixture
def engine(pg_app):
    with pg_app.app_context():
        from lumen.extensions import db

        yield db.engine


@pytest.fixture(autouse=True)
def clean(engine):
    """Leave no raw rows, no materialised rows and no retention policy behind.

    The migrations' ``fileConfig`` resets the root logger, so it is put back too.
    Deleting raw rows below the invalidation threshold logs them as invalidated, so
    the refresh then drops their materialised rows.
    """
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:], root.level = handlers, level
    with _autocommit(engine) as conn:
        conn.execute(text(
            "SELECT remove_retention_policy('request_logs', if_exists => true)"
        ))
        conn.execute(text("DELETE FROM request_logs"))
        # Bottom-up: each org-wide view is computed from its child's materialised rows.
        for view in USAGE_AGGREGATES:
            conn.execute(text(
                f"CALL refresh_continuous_aggregate('{view}', CAST(:s AS timestamptz), now())"
            ), {"s": _month(6)})
    for view in USAGE_AGGREGATES:
        assert _materialised_rows(engine, view) == 0, f"cleanup left {view} rows behind"


def _autocommit(engine):
    return engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def _month(k):
    """First instant, in UTC, of the month ``k`` months before the current one."""
    now = datetime.now(timezone.utc)
    total = now.year * 12 + (now.month - 1) - k
    return datetime(total // 12, total % 12 + 1, 1, tzinfo=timezone.utc)


def _hour(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def _insert(engine, *times):
    with engine.begin() as conn:
        for t in times:
            conn.execute(text(
                "INSERT INTO request_logs (time, source, input_tokens, output_tokens, cost, duration) "
                "VALUES (:t, 'api', 1, 1, 0.01, 0.1)"
            ), {"t": t})


def _refresh(engine, start, end, views=(ENTITY_AGGREGATE,)):
    """Refresh ``views`` over ``[start, end)``, in the order given: pass them bottom-up."""
    with _autocommit(engine) as conn:
        for view in views:
            conn.execute(text(
                f"CALL refresh_continuous_aggregate('{view}', "
                f"CAST(:s AS timestamptz), CAST(:e AS timestamptz))"
            ), {"s": start, "e": end})


def _mat_table(conn, view=ENTITY_AGGREGATE):
    return commands._materialization_hypertable(conn, view)


def _first_bucket(engine, start, end):
    """Earliest *materialised* bucket in [start, end), read off the materialisation hypertable."""
    with engine.connect() as conn:
        return conn.execute(text(
            f"SELECT MIN(bucket) FROM {_mat_table(conn)} WHERE bucket >= :s AND bucket < :e"
        ), {"s": start, "e": end}).scalar()


def _materialised_rows(engine, view=ENTITY_AGGREGATE):
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {_mat_table(conn, view)}")).scalar()


def _upgrade(pg_app, monkeypatch, fail_at=None, view=ENTITY_AGGREGATE):
    """Run ``flask upgrade``; returns (result, the windows it refreshed of ``view``).

    ``fail_at``: the window start of ``view`` whose refresh fails as one does once its
    lock retries run out. Every other window is refreshed for real. The org-wide views
    are backfilled after the entity aggregate, from its rows.
    """
    windows = []
    real = commands._refresh_window

    def recording(conn, name, start, end):
        if name != view:
            return real(conn, name, start, end)
        windows.append((start, end))
        if start == fail_at:
            raise exc.DBAPIError("CALL refresh_continuous_aggregate", {}, Exception("injected failure"))
        return real(conn, name, start, end)

    monkeypatch.setattr(commands, "_refresh_window", recording)
    result = pg_app.test_cli_runner().invoke(upgrade_cmd, [])
    monkeypatch.setattr(commands, "_refresh_window", real)
    return result, windows


def _check(pg_app):
    return pg_app.test_cli_runner().invoke(check_aggregate_cmd, [])


def _starts(windows):
    return [start for start, _ in windows]


@pytest.fixture
def empty_pg_url():
    base = os.environ.get(PG_URL_ENV)
    if not base:
        pytest.skip(f"{PG_URL_ENV} is not set; skipping PostgreSQL/TimescaleDB tests")
    name = f"lumen_empty_{uuid.uuid4().hex[:12]}"
    admin = _admin_engine(base)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    try:
        yield base.rsplit("/", 1)[0] + "/" + name
    finally:
        admin = _admin_engine(base)
        with admin.connect() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"
            ), {"n": name})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def test_empty_database_migrates_to_head(empty_pg_url):
    """The image's ``upgrade`` mode on a fresh database: every migration, then the steps."""
    env = {**os.environ, "DATABASE_URL": empty_pg_url, "CONFIG_YAML": TEST_CONFIG,
           "BACKGROUND_WORKER": "false"}
    result = subprocess.run(["uv", "run", "flask", "--app", "run", "upgrade"],
                            capture_output=True, text=True, env=env, timeout=300)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    for view in USAGE_AGGREGATES:
        assert f"Aggregate backfill: no missing months in '{view}'." in result.stdout, output
    assert "Upgrade complete." in result.stdout, output
    engine = create_engine(empty_pg_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() \
            == _script_dir().get_current_head()
    engine.dispose()


def test_interrupted_backfill_resumes_at_the_failed_month(pg_app, engine, monkeypatch):
    months = [_month(4), _month(3), _month(2), _month(1)]
    _insert(engine, *(m + timedelta(days=2) for m in months))

    result, windows = _upgrade(pg_app, monkeypatch, fail_at=months[2])
    assert result.exit_code == 1, result.output
    assert _starts(windows) == months[:3]
    assert (f"Error: refreshing {months[2]:%Y-%m} of '{ENTITY_AGGREGATE}' failed: injected failure"
            in result.output)
    assert "Upgrade complete." not in result.output
    for m in months[:2]:
        assert _first_bucket(engine, m, commands._next_month(m)) == m + timedelta(days=2)

    result, windows = _upgrade(pg_app, monkeypatch)
    assert result.exit_code == 0, result.output
    assert _starts(windows) == months[2:], "the re-run refreshed months that were already done"
    for m in months:
        assert _first_bucket(engine, m, commands._next_month(m)) == m + timedelta(days=2)


def test_partly_covered_month_is_reported_and_refreshed(pg_app, engine, monkeypatch):
    """Days 20 to the end materialised, day 2 not: what the policy's trailing window leaves."""
    month, nxt = _month(2), _month(1)
    head = month + timedelta(days=1, hours=10)
    _insert(engine, head, month + timedelta(days=24, hours=10))
    _refresh(engine, month + timedelta(days=19), nxt)
    assert _first_bucket(engine, month, nxt) == month + timedelta(days=24, hours=10), \
        "the month must be partly covered here or this test proves nothing"

    check = _check(pg_app)
    assert check.exit_code == 1, check.output
    assert f"  {month:%Y-%m}: partly covered" in check.output, check.output

    result, windows = _upgrade(pg_app, monkeypatch)
    assert result.exit_code == 0, result.output
    assert windows == [(month, nxt)]
    assert _first_bucket(engine, month, nxt) == _hour(head)
    assert _check(pg_app).exit_code == 0


def test_failed_month_before_the_current_one_is_refreshed_after_a_policy_tick(
        pg_app, engine, monkeypatch):
    """The re-run must not trust the tail a policy tick materialised during the backoff."""
    m3, m2, m1, m0 = _month(3), _month(2), _month(1), _month(0)
    head = m1 + timedelta(days=1, hours=5)
    _insert(engine, m3 + timedelta(days=2), m2 + timedelta(days=2), head, m0 - timedelta(days=5))

    result, windows = _upgrade(pg_app, monkeypatch, fail_at=m1)
    assert result.exit_code == 1, result.output
    assert _starts(windows) == [m3, m2, m1]

    _refresh(engine, m0 - timedelta(days=10), m0)  # the policy tick
    assert _first_bucket(engine, m1, m0) == _hour(m0 - timedelta(days=5)), \
        "the tail must be materialised here or this test proves nothing"

    result, windows = _upgrade(pg_app, monkeypatch)
    assert result.exit_code == 0, result.output
    assert windows == [(m1, m0)]
    assert _first_bucket(engine, m1, m0) == _hour(head)


def test_current_month_is_refreshed_up_to_now(pg_app, engine, monkeypatch):
    m0 = _month(0)
    this_hour = _hour(datetime.now(timezone.utc))
    if this_hour - m0 < timedelta(hours=3):
        pytest.skip("the current month is too young to hold a head and a separate tail")
    head = m0 + timedelta(minutes=1)
    tail = this_hour - timedelta(minutes=59)
    _insert(engine, head, tail)
    _refresh(engine, this_hour - timedelta(hours=1), datetime.now(timezone.utc))
    assert _first_bucket(engine, m0, this_hour) == _hour(tail), \
        "only the tail may be materialised here or this test proves nothing"

    before = datetime.now(timezone.utc)
    result, windows = _upgrade(pg_app, monkeypatch)
    after = datetime.now(timezone.utc)
    assert result.exit_code == 0, result.output
    assert len(windows) == 1
    start, end = windows[0]
    assert start == m0
    assert before - timedelta(seconds=5) <= end <= after, "the window must end at now"
    assert _first_bucket(engine, m0, this_hour) == m0


def test_only_an_interior_month_with_deleted_buckets_is_refreshed(pg_app, engine, monkeypatch):
    m3, m2, m1, m0 = _month(3), _month(2), _month(1), _month(0)
    rows = [m3 + timedelta(days=2), m2 + timedelta(days=2), m1 + timedelta(days=2)]
    _insert(engine, *rows)
    _refresh(engine, m3, m0, USAGE_AGGREGATES)
    # Delete m2's buckets the only way a refresh can: without raw rows it materialises
    # the month as empty. Re-inserted below the invalidation threshold, the rows are
    # logged as invalidated, so a later refresh of the month picks them up again.
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM request_logs WHERE time >= :s AND time < :e"),
                     {"s": m2, "e": m1})
    _refresh(engine, m2, m1)
    _insert(engine, rows[1])
    assert _first_bucket(engine, m2, m1) is None
    assert _first_bucket(engine, m3, m2) is not None and _first_bucket(engine, m1, m0) is not None

    check = _check(pg_app)
    assert check.exit_code == 1, check.output
    assert f"  {m2:%Y-%m}: missing" in check.output, check.output
    assert f"{m3:%Y-%m}:" not in check.output and f"{m1:%Y-%m}:" not in check.output, check.output

    result, windows = _upgrade(pg_app, monkeypatch)
    assert result.exit_code == 0, result.output
    assert windows == [(m2, m1)]
    assert _first_bucket(engine, m2, m1) == rows[1]


def test_fully_covered_aggregate_refreshes_nothing(pg_app, engine, monkeypatch):
    m2, m1 = _month(2), _month(1)
    _insert(engine, m2 + timedelta(days=2), m1 + timedelta(days=2))
    _refresh(engine, m2, datetime.now(timezone.utc), USAGE_AGGREGATES)

    check = _check(pg_app)
    assert check.exit_code == 0, check.output
    assert f"{ENTITY_AGGREGATE} covers request_logs." in check.output

    result, windows = _upgrade(pg_app, monkeypatch)
    assert result.exit_code == 0, result.output
    assert windows == []
    assert f"Aggregate backfill: no missing months in '{ENTITY_AGGREGATE}'." in result.output


def _view_requests(engine, view, bucket_of):
    """Requests ``view`` returns, real-time half included, in the bucket holding ``bucket_of``."""
    width = "1 day" if view == "request_counts_daily" else "1 hour"
    with engine.connect() as conn:
        return conn.execute(text(
            f"SELECT COALESCE(SUM(requests), 0) FROM {view} "
            f"WHERE bucket = time_bucket(CAST(:w AS interval), CAST(:t AS timestamptz))"
        ), {"w": width, "t": bucket_of}).scalar()


def _materialised_totals(engine, view, end):
    """``(requests, input_tokens, output_tokens, cost)`` materialised in ``view`` before ``end``."""
    with engine.connect() as conn:
        return tuple(conn.execute(text(
            "SELECT COALESCE(SUM(requests), 0), COALESCE(SUM(input_tokens), 0), "
            "COALESCE(SUM(output_tokens), 0), COALESCE(SUM(cost), 0) "
            f"FROM {_mat_table(conn, view)} WHERE bucket < :e"
        ), {"e": end}).one())


def _raw_totals(engine, end):
    with engine.connect() as conn:
        return tuple(conn.execute(text(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0), "
            "COALESCE(SUM(cost), 0) FROM request_logs WHERE time < :e"
        ), {"e": end}).one())


def test_upgrade_backfills_the_org_wide_views_bottom_up(pg_app, engine, monkeypatch):
    """The entity aggregate first, then hourly from it, then hourly_total and daily from hourly.

    A current-bucket row is visible through every org-wide view before any refresh,
    through real-time aggregation chained over the child, and still visible after the
    backfill: the backfill stops at now, so the open buckets stay above the watermarks.
    """
    m2, m1 = _month(2), _month(1)
    _insert(engine, m2 + timedelta(days=2), m2 + timedelta(days=9, hours=23, minutes=59),
            m1 + timedelta(days=3))
    with engine.begin() as conn:
        current = conn.execute(text(
            "INSERT INTO request_logs (time, source, input_tokens, output_tokens, cost, duration) "
            "VALUES (now(), 'api', 7, 9, 0.123456, 0.1) RETURNING time"
        )).scalar()
    for view in ("request_counts_hourly", "request_counts_hourly_total", "request_counts_daily"):
        assert _view_requests(engine, view, current) == 1, f"{view} hides the current bucket"

    names = []
    real = commands._refresh_window

    def recording(conn, name, start, end):
        names.append(name)
        return real(conn, name, start, end)

    monkeypatch.setattr(commands, "_refresh_window", recording)
    result = pg_app.test_cli_runner().invoke(upgrade_cmd, [])
    monkeypatch.setattr(commands, "_refresh_window", real)
    assert result.exit_code == 0, result.output
    assert names == sorted(names, key=USAGE_AGGREGATES.index), names
    assert set(names) == set(USAGE_AGGREGATES), names

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for view in USAGE_AGGREGATES:
        assert _materialised_totals(engine, view, today) == _raw_totals(engine, today), view
        assert _view_requests(engine, view, current) == 1, f"{view} lost the current bucket"

    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO request_logs (time, source, input_tokens, output_tokens, cost, duration) "
            "VALUES (:t, 'api', 1, 1, 0.01, 0.1)"
        ), {"t": current})
    for view in ("request_counts_hourly", "request_counts_hourly_total", "request_counts_daily"):
        assert _view_requests(engine, view, current) == 2, \
            f"a request made after the backfill is invisible through {view}"


def _backfilled(pg_app, engine, monkeypatch):
    """Two rows at different hours of month -2, one in month -1, all three views backfilled."""
    m2, m1 = _month(2), _month(1)
    _insert(engine, m2 + timedelta(days=2, hours=3), m2 + timedelta(days=9, hours=15),
            m1 + timedelta(days=3))
    result, _ = _upgrade(pg_app, monkeypatch)
    assert result.exit_code == 0, result.output
    return m2, m1


def _delete_materialised(engine, view, start, end):
    """Remove ``view``'s materialised buckets in ``[start, end)``: drift no refresh logged."""
    with _autocommit(engine) as conn:
        deleted = conn.execute(text(
            f"DELETE FROM {_mat_table(conn, view)} WHERE bucket >= :s AND bucket < :e"
        ), {"s": start, "e": end}).rowcount
    assert deleted > 0, f"nothing of {view} was deleted, so this test proves nothing"


def test_check_passes_on_a_backfilled_database(pg_app, engine, monkeypatch):
    _backfilled(pg_app, engine, monkeypatch)
    check = _check(pg_app)
    assert check.exit_code == 0, check.output
    assert f"{ENTITY_AGGREGATE} covers request_logs." in check.output
    assert f"Usage aggregates agree on every month before {_month(0):%Y-%m}." in check.output
    assert "Error" not in check.output, check.output


def test_check_names_the_month_an_hourly_bucket_drifted_in(pg_app, engine, monkeypatch):
    """One hourly bucket gone, not the month's first: only the cross-view totals see it."""
    m2, m1 = _backfilled(pg_app, engine, monkeypatch)
    second = m2 + timedelta(days=9, hours=15)
    _delete_materialised(engine, "request_counts_hourly", second, second + timedelta(hours=1))

    check = _check(pg_app)
    assert check.exit_code == 1, check.output
    assert "Error: the usage aggregates disagree" in check.output, check.output
    assert f"  {m2:%Y-%m}: requests 2 / 1 / 2 / 2" in check.output, check.output
    assert f"  {m2:%Y-%m}: cost 0.020000 / 0.010000 / 0.020000 / 0.020000" in check.output, check.output
    assert f"  {m1:%Y-%m}:" not in check.output, check.output
    assert "missing or partly covered" not in check.output, check.output


def test_check_fails_when_the_daily_view_is_missing_a_month(pg_app, engine, monkeypatch):
    m2, m1 = _backfilled(pg_app, engine, monkeypatch)
    _delete_materialised(engine, "request_counts_daily", m2, m1)

    check = _check(pg_app)
    assert check.exit_code == 1, check.output
    assert "Error: these months of 'request_counts_daily' are missing or partly covered:" \
        in check.output, check.output
    assert f"  {m2:%Y-%m}: missing" in check.output, check.output
    assert f"flask backfill-aggregate --name request_counts_daily --from {m2:%Y-%m}" \
        in check.output, check.output
    assert f"  {m2:%Y-%m}: requests 2 / 2 / 2 / 0" in check.output, check.output
    assert f"  {m1:%Y-%m}:" not in check.output, check.output


def test_check_names_the_month_an_hourly_total_bucket_drifted_in(pg_app, engine, monkeypatch):
    """One request_counts_hourly_total bucket gone, not the month's first: the
    cross-view totals see it in the third column."""
    m2, m1 = _backfilled(pg_app, engine, monkeypatch)
    second = m2 + timedelta(days=9, hours=15)
    _delete_materialised(engine, "request_counts_hourly_total", second, second + timedelta(hours=1))

    check = _check(pg_app)
    assert check.exit_code == 1, check.output
    assert "Error: the usage aggregates disagree" in check.output, check.output
    assert f"  {m2:%Y-%m}: requests 2 / 2 / 1 / 2" in check.output, check.output
    assert f"  {m1:%Y-%m}:" not in check.output, check.output
    assert "missing or partly covered" not in check.output, check.output


def test_check_fails_when_the_hourly_total_view_is_missing_a_month(pg_app, engine, monkeypatch):
    m2, m1 = _backfilled(pg_app, engine, monkeypatch)
    _delete_materialised(engine, "request_counts_hourly_total", m2, m1)

    check = _check(pg_app)
    assert check.exit_code == 1, check.output
    assert "Error: these months of 'request_counts_hourly_total' are missing or partly covered:" \
        in check.output, check.output
    assert f"  {m2:%Y-%m}: missing" in check.output, check.output
    assert f"flask backfill-aggregate --name request_counts_hourly_total --from {m2:%Y-%m}" \
        in check.output, check.output
    assert f"  {m2:%Y-%m}: requests 2 / 2 / 0 / 2" in check.output, check.output
    assert f"  {m1:%Y-%m}:" not in check.output, check.output


# The watermark TimescaleDB stores for a continuous aggregate never refreshed: the
# minimum timestamptz in microseconds, as ``test_lifecycle_cli_postgres.py`` reads it.
_NEVER_REFRESHED_WATERMARK = -210866803200000000


def test_check_compares_nothing_before_the_daily_view_is_refreshed(pg_app, engine, monkeypatch):
    """The freshly migrated daily view: a watermark no ``datetime`` can hold.

    The cleanup fixture's refresh moves the watermark forward again.
    """
    _backfilled(pg_app, engine, monkeypatch)
    with _autocommit(engine) as conn:
        conn.execute(text(
            "UPDATE _timescaledb_catalog.continuous_aggs_watermark SET watermark = :w "
            "WHERE mat_hypertable_id = (SELECT mat_hypertable_id "
            "FROM _timescaledb_catalog.continuous_agg WHERE user_view_name = 'request_counts_daily')"
        ), {"w": _NEVER_REFRESHED_WATERMARK})
        assert commands._watermark(conn, "request_counts_daily") is None

    check = _check(pg_app)
    assert check.exit_code == 0, check.output
    assert "'request_counts_daily' has never been refreshed; no month to compare" in check.output


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_retention_policy_on_request_logs_skips_the_backfill(pg_app, engine, monkeypatch):
    """Logged on the app's logger after the migrations ran in-process: they must not mute it."""
    _insert(engine, _month(2) + timedelta(days=2))
    with _autocommit(engine) as conn:
        conn.execute(text(
            "SELECT add_retention_policy('request_logs', drop_after => INTERVAL '13 months')"
        ))
    records = _Records()
    pg_app.logger.addHandler(records)
    try:
        result, windows = _upgrade(pg_app, monkeypatch)
    finally:
        pg_app.logger.removeHandler(records)

    assert result.exit_code == 0, result.output
    assert windows == []
    assert "Aggregate backfill: skipped, request_logs has a retention policy." in result.output
    errors = [r.getMessage() for r in records.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, errors
    assert errors[0].startswith("request_logs has a retention policy (job ")
    assert "SELECT remove_retention_policy('request_logs');" in errors[0]


def test_failing_migration_runs_no_post_step(pg_app, engine, monkeypatch, tmp_path):
    head = _script_dir().get_current_head()
    scripts = tmp_path / "migrations"
    shutil.copytree(REPO / "migrations", scripts)
    (scripts / "versions" / "ffff00000000_injected_failure.py").write_text(
        "from alembic import op\n\n"
        "revision = 'ffff00000000'\n"
        f"down_revision = '{head}'\n"
        "branch_labels = None\n"
        "depends_on = None\n\n\n"
        "def upgrade():\n"
        "    op.execute('SELECT * FROM lumen_no_such_table')\n\n\n"
        "def downgrade():\n"
        "    pass\n"
    )
    monkeypatch.setattr(pg_app.extensions["migrate"].migrate, "directory", str(scripts))
    steps = []
    monkeypatch.setattr(commands, "POST_MIGRATION_STEPS", (lambda: steps.append("step"),))

    result = pg_app.test_cli_runner().invoke(upgrade_cmd, [])
    assert result.exit_code != 0, result.output
    assert "lumen_no_such_table" in str(result.exception)
    assert steps == []
    assert "Upgrade complete." not in result.output
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == head
