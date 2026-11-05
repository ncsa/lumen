"""The hierarchical usage aggregates of ``c6e8a0b2d4f6`` on real TimescaleDB 2.27.2.

``request_counts_hourly`` and ``request_counts_daily_by_entity`` (``e8a0c2d4f6b8``,
the per-entity Year and All ranges' daily view) are built on
``request_counts_hourly_by_entity``, and ``request_counts_daily`` and
``request_counts_hourly_total`` (``d7f9b1c3e5a7``, the org-wide heatmap's per-hour
totals) on ``request_counts_hourly``. Three TimescaleDB behaviours
carry that design, and the first tests here pin them:

  (a) a 1 hour aggregate over a 1 hour aggregate is accepted
  (b) real-time aggregation on a parent chains through the child's real-time half,
      so a row inserted now shows through both views before any refresh
  (c) below the child's watermark, a parent refresh reads the child's materialised
      rows, not raw request_logs

Every background job in this module's private database is paused, so no policy
refresh moves a watermark under a test.
"""

import os
import subprocess
from datetime import timedelta

import pytest
from sqlalchemy import text

from lumen import commands
from tests.unit.test_migrations import _script_dir

from .conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres

ENTITY = "request_counts_hourly_by_entity"
HOURLY = "request_counts_hourly"
DAILY = "request_counts_daily"
TOTAL = "request_counts_hourly_total"
DAILY_ENTITY = "request_counts_daily_by_entity"
REVISION = "c6e8a0b2d4f6"
BEFORE = "c8d0e2f4a6b8"
TOTAL_REVISION = "d7f9b1c3e5a7"
DAILY_ENTITY_REVISION = "e8a0c2d4f6b8"


@pytest.fixture(scope="module")
def db(pg_migrated_isolated):
    url, engine = pg_migrated_isolated
    with _autocommit(engine) as conn:
        conn.execute(text(
            "SELECT alter_job(job_id, scheduled => false) FROM timescaledb_information.jobs "
            "WHERE job_id >= 1000"
        ))
    return url, engine


@pytest.fixture(scope="module")
def engine(db):
    return db[1]


def _autocommit(engine):
    return engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def _refresh(engine, view, start, end):
    with _autocommit(engine) as conn:
        conn.execute(text(
            f"CALL refresh_continuous_aggregate('{view}', "
            "CAST(:s AS timestamptz), CAST(:e AS timestamptz))"
        ), {"s": start, "e": end})


def _insert(engine, source, at="now()"):
    """One request_logs row tagged ``source``; returns its time."""
    with engine.begin() as conn:
        return conn.execute(text(
            "INSERT INTO request_logs (time, source, input_tokens, output_tokens, cost, duration) "
            f"VALUES ({at}, :s, 3, 4, 0.123456, 0.1) RETURNING time"
        ), {"s": source}).scalar()


def _requests(engine, relation, source):
    with engine.connect() as conn:
        return conn.execute(text(
            f"SELECT COALESCE(SUM(requests), 0) FROM {relation} WHERE source = :s"
        ), {"s": source}).scalar()


def _mat(engine, view):
    with engine.connect() as conn:
        return commands._materialization_hypertable(conn, view)


def _catalog(engine):
    """``{view: (parent view or None, bucket width)}`` from TimescaleDB's own catalog."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT ca.user_view_name, parent.user_view_name, CAST(bf.bucket_width AS interval) "
            "FROM _timescaledb_catalog.continuous_agg ca "
            "LEFT JOIN _timescaledb_catalog.continuous_agg parent "
            "  ON parent.mat_hypertable_id = ca.parent_mat_hypertable_id "
            "JOIN _timescaledb_catalog.continuous_aggs_bucket_function bf "
            "  ON bf.mat_hypertable_id = ca.mat_hypertable_id"
        )).all()
    return {view: (parent, width) for view, parent, width in rows}


def _settings(engine, view):
    """``(materialized_only, policy config, comment)`` of ``view``."""
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT a.materialized_only, "
            "  (SELECT j.config FROM timescaledb_information.jobs j "
            "   WHERE j.proc_name = 'policy_refresh_continuous_aggregate' "
            "   AND j.hypertable_name = a.view_name), "
            "  obj_description(CAST(a.view_name AS regclass), 'pg_class') "
            "FROM timescaledb_information.continuous_aggregates a WHERE a.view_name = :v"
        ), {"v": view}).one()


def _definition(engine, view):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT view_definition FROM timescaledb_information.continuous_aggregates "
            "WHERE view_name = :v"
        ), {"v": view}).scalar()


def _flask_db(url, *args):
    return subprocess.run(
        ["uv", "run", "flask", "--app", "run", "db", *args],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "DATABASE_URL": url, "CONFIG_YAML": TEST_CONFIG, "BACKGROUND_WORKER": "false"},
    )


def _ok(result, what):
    assert result.returncode == 0, (
        f"flask db {what} failed:\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


# --- (a), (b), (c): the TimescaleDB behaviour the design depends on --------------------


def test_a_hourly_is_a_one_hour_aggregate_over_the_one_hour_entity_aggregate(engine):
    catalog = _catalog(engine)
    assert catalog[ENTITY] == (None, timedelta(hours=1))
    assert catalog[HOURLY] == (ENTITY, timedelta(hours=1))
    assert catalog[DAILY] == (HOURLY, timedelta(days=1))
    assert catalog[TOTAL] == (HOURLY, timedelta(hours=1))
    assert catalog[DAILY_ENTITY] == (ENTITY, timedelta(days=1))


def test_b_a_row_inserted_now_shows_through_both_views_before_any_refresh(engine):
    _insert(engine, "now-b")
    for view in (ENTITY, HOURLY, DAILY, DAILY_ENTITY):
        assert _requests(engine, view, "now-b") == 1, f"{view} hides the current bucket"
        assert _requests(engine, _mat(engine, view), "now-b") == 0, \
            f"{view} has the row materialised, so this test proves nothing about real-time"


def _hour_requests(engine, relation, at):
    """Requests in the hour bucket holding ``at``; the totals view has no source to filter on."""
    with engine.connect() as conn:
        return conn.execute(text(
            f"SELECT COALESCE(SUM(requests), 0) FROM {relation} "
            "WHERE bucket = time_bucket(INTERVAL '1 hour', CAST(:t AS timestamptz))"
        ), {"t": at}).scalar()


def test_b_a_row_inserted_now_shows_through_the_totals_view_before_any_refresh(engine):
    at = _insert(engine, "now-tot")
    with engine.connect() as conn:
        raw = conn.execute(text(
            "SELECT COUNT(*) FROM request_logs "
            "WHERE time_bucket(INTERVAL '1 hour', time) = time_bucket(INTERVAL '1 hour', CAST(:t AS timestamptz))"
        ), {"t": at}).scalar()
    assert raw >= 1
    assert _hour_requests(engine, TOTAL, at) == raw, f"{TOTAL} hides the current bucket"
    assert _hour_requests(engine, _mat(engine, TOTAL), at) == 0, \
        f"{TOTAL} has the row materialised, so this test proves nothing about real-time"


def _day(engine, source, days_ago, hour):
    """Insert one row ``days_ago`` days back at ``hour``; return its UTC day as a window."""
    at = _insert(engine, source, f"date_trunc('day', now()) - INTERVAL '{days_ago} days' "
                                 f"+ INTERVAL '{hour} hours'")
    day = at.replace(hour=0, minute=0, second=0, microsecond=0)
    return day, day + timedelta(days=1)


def test_c_a_parent_refresh_reads_the_childs_materialised_rows(engine):
    """Where the child has materialised, a parent refresh copies the child's rows, stale or not.

    That is why ``flask upgrade``, ``reassign-model`` and the seed refresh bottom-up.
    The watermark is the end of the newest materialised bucket, so both rows go into
    the same hour: the second is below the child's watermark and only in raw rows.
    """
    window = _day(engine, "mat-c", 40, 5)
    _refresh(engine, ENTITY, *window)
    _day(engine, "mat-c", 40, 5)
    _refresh(engine, HOURLY, *window)
    assert _requests(engine, _mat(engine, ENTITY), "mat-c") == 1
    assert _requests(engine, _mat(engine, HOURLY), "mat-c") == 1, \
        "the hourly refresh did not read the child's materialised rows"

    _refresh(engine, ENTITY, *window)
    _refresh(engine, HOURLY, *window)
    _refresh(engine, DAILY, *window)
    for view in (ENTITY, HOURLY, DAILY):
        assert _requests(engine, _mat(engine, view), "mat-c") == 2, view


def _monthly(engine, relation):
    """``{month: (requests, input_tokens, output_tokens, cost)}`` of ``relation``."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT date_trunc('month', bucket, 'UTC'), SUM(requests), SUM(input_tokens), "
            f"SUM(output_tokens), SUM(cost) FROM {relation} GROUP BY 1"
        )).all()
    return {row[0]: tuple(row[1:]) for row in rows}


def test_totals_view_refreshed_in_order_has_the_hourly_views_monthly_totals(engine):
    """Rows in each of the three closed UTC months before this one, refreshed child first:
    hourly_total's materialised monthly totals equal the hourly view's, every column.

    The months are counted from this month's UTC start, not as day offsets from now, so
    the three always differ whatever the date.
    """
    month = "date_trunc('month', now(), 'UTC')"
    for back, hour in ((3, 3), (3, 3), (2, 22), (1, 0)):
        _insert(engine, f"tot-{back}",
                f"{month} - INTERVAL '{back} months' + INTERVAL '3 days {hour} hours'")
    with engine.connect() as conn:
        start, end = conn.execute(text(
            f"SELECT {month} - INTERVAL '3 months', {month}"
        )).one()
        seeded = {conn.execute(text(
            f"SELECT {month} - INTERVAL '{back} months'"
        )).scalar() for back in (1, 2, 3)}
    for view in commands.AGGREGATE_REFRESH_ORDER:
        if view in (ENTITY, HOURLY, TOTAL):
            _refresh(engine, view, start, end)
    hourly, total = _monthly(engine, _mat(engine, HOURLY)), _monthly(engine, _mat(engine, TOTAL))
    assert len(seeded) == 3
    assert seeded <= set(total), (seeded, total)
    assert total == hourly


def test_daily_entity_view_refreshed_in_order_has_the_entity_views_monthly_totals(engine):
    """Rows in each of the three closed UTC months before this one, refreshed child first:
    daily_by_entity's materialised monthly totals equal the entity aggregate's, every
    column, over the refreshed window.
    """
    month = "date_trunc('month', now(), 'UTC')"
    for back, hour in ((3, 3), (3, 23), (2, 22), (1, 0)):
        _insert(engine, f"dbe-{back}",
                f"{month} - INTERVAL '{back} months' + INTERVAL '5 days {hour} hours'")
    with engine.connect() as conn:
        start, end = conn.execute(text(
            f"SELECT {month} - INTERVAL '3 months', {month}"
        )).one()
        seeded = {conn.execute(text(
            f"SELECT {month} - INTERVAL '{back} months'"
        )).scalar() for back in (1, 2, 3)}
    for view in commands.AGGREGATE_REFRESH_ORDER:
        if view in (ENTITY, DAILY_ENTITY):
            _refresh(engine, view, start, end)
    entity = {m: v for m, v in _monthly(engine, _mat(engine, ENTITY)).items() if start <= m < end}
    daily = {m: v for m, v in _monthly(engine, _mat(engine, DAILY_ENTITY)).items()
             if start <= m < end}
    assert len(seeded) == 3
    assert seeded <= set(daily), (seeded, daily)
    assert daily == entity
    for back in (1, 2, 3):
        assert _requests(engine, _mat(engine, DAILY_ENTITY), f"dbe-{back}") == (2 if back == 3 else 1)


# --- The migration's settings -----------------------------------------------------------


@pytest.mark.parametrize("view, end_offset", [(HOURLY, "03:00:00"), (DAILY, "1 day"),
                                              (TOTAL, "04:00:00"), (DAILY_ENTITY, "1 day")])
def test_views_are_real_time_with_their_policy_and_comment(engine, view, end_offset):
    materialized_only, config, comment = _settings(engine, view)
    assert materialized_only is False, f"{view} hides the buckets after its last refresh"
    assert config["start_offset"] == "30 days"
    assert config["end_offset"] == end_offset
    assert comment and "hierarchical aggregate" in comment


def test_no_retention_on_the_org_wide_views(engine):
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_retention' AND hypertable_name IN (:h, :d, :e)"
        ), {"h": HOURLY, "d": DAILY, "e": DAILY_ENTITY}).scalar() == 0


@pytest.mark.parametrize("view", [HOURLY, DAILY])
def test_org_wide_sums_keep_the_column_types(engine, view):
    """The types of the raw-based hourly view this replaces: SUM of a bigint is numeric."""
    with engine.connect() as conn:
        types = dict(conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = :v"
        ), {"v": view}).all())
    assert types == {
        "bucket": "timestamp with time zone", "model_config_id": "integer",
        "source": "character varying", "requests": "bigint", "input_tokens": "bigint",
        "output_tokens": "bigint", "cost": "numeric",
    }


def test_totals_view_is_grouped_by_bucket_only_and_documents_the_walks(engine):
    with engine.connect() as conn:
        types = dict(conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = :v"
        ), {"v": TOTAL}).all())
        assert TOTAL not in commands._aggregates_with_column(conn, "model_config_id")
        assert TOTAL not in commands._aggregates_with_column(conn, "entity_id")
    assert types == {
        "bucket": "timestamp with time zone", "requests": "bigint", "input_tokens": "bigint",
        "output_tokens": "bigint", "cost": "numeric",
    }
    _, config, comment = _settings(engine, TOTAL)
    assert "reassign-model" in comment and "erase-entity" in comment


def test_daily_entity_view_keeps_the_entity_keys_and_is_in_both_walks(engine):
    """Grouped like its child, so reassign-model and erase-entity find and refresh it."""
    with engine.connect() as conn:
        types = dict(conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = :v"
        ), {"v": DAILY_ENTITY}).all())
        for column in ("model_config_id", "entity_id"):
            walk = commands._aggregates_with_column(conn, column)
            assert DAILY_ENTITY in walk, (column, walk)
            assert walk.index(ENTITY) < walk.index(DAILY_ENTITY), (column, walk)
    assert types == {
        "bucket": "timestamp with time zone", "entity_id": "integer",
        "model_config_id": "integer", "source": "character varying", "requests": "bigint",
        "input_tokens": "bigint", "output_tokens": "bigint", "cost": "numeric",
    }


# --- Round trip --------------------------------------------------------------------------


def test_downgrade_drops_the_totals_view_and_upgrade_recreates_it(db, engine):
    url = db[0]
    assert _script_dir().get_revision(DAILY_ENTITY_REVISION).down_revision == TOTAL_REVISION

    _ok(_flask_db(url, "downgrade", REVISION), "downgrade")
    catalog = _catalog(engine)
    assert TOTAL not in catalog
    assert catalog[DAILY] == (HOURLY, timedelta(days=1))
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM timescaledb_information.jobs WHERE hypertable_name = :v"
        ), {"v": TOTAL}).scalar() == 0

    _ok(_flask_db(url, "upgrade"), "upgrade")
    assert _catalog(engine)[TOTAL] == (HOURLY, timedelta(hours=1))
    materialized_only, config, comment = _settings(engine, TOTAL)
    assert materialized_only is False
    assert (config["start_offset"], config["end_offset"]) == ("30 days", "04:00:00")
    assert comment.startswith("Hourly org-wide usage totals")
    at = _insert(engine, "rt-total")
    assert _hour_requests(engine, TOTAL, at) >= 1


def test_downgrade_drops_the_daily_entity_view_and_upgrade_recreates_it(db, engine):
    url = db[0]
    assert _script_dir().get_current_head() == DAILY_ENTITY_REVISION, \
        "this revision is no longer head"

    _ok(_flask_db(url, "downgrade", TOTAL_REVISION), "downgrade")
    catalog = _catalog(engine)
    assert DAILY_ENTITY not in catalog
    assert catalog[TOTAL] == (HOURLY, timedelta(hours=1))
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM timescaledb_information.jobs WHERE hypertable_name = :v"
        ), {"v": DAILY_ENTITY}).scalar() == 0

    _ok(_flask_db(url, "upgrade"), "upgrade")
    assert _catalog(engine)[DAILY_ENTITY] == (ENTITY, timedelta(days=1))
    materialized_only, config, comment = _settings(engine, DAILY_ENTITY)
    assert materialized_only is False
    assert (config["start_offset"], config["end_offset"]) == ("30 days", "1 day")
    assert comment.startswith("Daily per-entity usage")
    _insert(engine, "rt-dbe")
    assert _requests(engine, DAILY_ENTITY, "rt-dbe") == 1


def test_downgrade_restores_the_raw_hourly_view_and_upgrade_rebuilds_it(db, engine):
    url = db[0]

    _ok(_flask_db(url, "downgrade", BEFORE), "downgrade")
    assert DAILY not in _catalog(engine)
    assert DAILY_ENTITY not in _catalog(engine)
    assert _catalog(engine)[HOURLY] == (None, timedelta(hours=1))
    assert "request_logs" in _definition(engine, HOURLY)
    materialized_only, config, comment = _settings(engine, HOURLY)
    assert materialized_only is False
    assert (config["start_offset"], config["end_offset"]) == ("03:00:00", "01:00:00")
    assert comment.startswith("Hourly org-wide rollup of request_logs")
    _insert(engine, "rt-down")
    assert _requests(engine, HOURLY, "rt-down") == 1

    _ok(_flask_db(url, "upgrade"), "upgrade")
    catalog = _catalog(engine)
    assert catalog[HOURLY] == (ENTITY, timedelta(hours=1))
    assert catalog[DAILY] == (HOURLY, timedelta(days=1))
    assert catalog[TOTAL] == (HOURLY, timedelta(hours=1))
    assert catalog[DAILY_ENTITY] == (ENTITY, timedelta(days=1))
    assert _requests(engine, HOURLY, "rt-down") == 1
    assert _requests(engine, DAILY_ENTITY, "rt-down") == 1
    assert _requests(engine, DAILY, "rt-down") == 1
