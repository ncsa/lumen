"""Every PostgreSQL session this process opens must be in UTC.

CLAUDE.md: "All times are UTC everywhere in the app and the DB; only convert to
the user's local timezone when displaying to them." Nothing enforced that at the
connection level. ``date_trunc``, ``EXTRACT`` and every ``timestamptz`` psycopg2
hands back are evaluated in the *session's* ``TimeZone``, which Postgres inherits
from whatever ``initdb`` saw on the host, so the invariant held only because the
``timescale/timescaledb`` image happens to default to UTC.

On a server initialised as, say, ``CST6CDT`` the same instant reads differently::

    SELECT EXTRACT(HOUR FROM TIMESTAMPTZ '2026-09-01 02:30:00+00'),
           EXTRACT(DOW  FROM TIMESTAMPTZ '2026-09-01 02:30:00+00');
    UTC server : hour=2   dow=2 (Tue)
    CST server : hour=21  dow=1 (Mon)

— so /usage's heatmap plots every request in the wrong hour *and* the wrong day,
the day/week/month buckets land in the wrong period, and ``backfill-aggregate``
picks the wrong starting month. None of it raises anything.

Why the assertions here are absolute rather than comparative: the rest of the
integration suite proves the aggregate arm equals the raw arm, and both arms run
in the *same* session timezone, so they agree with each other while both are
wrong. Only a fixed expected value catches this — hence a row seeded at a known
UTC instant near a day boundary and a literal expected ``(dow, hour)``.

Both arms are exercised, in that order and for a reason: ``_entity_aggregate_covers``
answers from the aggregate once it reaches back past the oldest raw row, so the
test seeds and refreshes entity A first (aggregate arm), then seeds entity B
*older* than the aggregate's earliest bucket, which forces the raw arm globally.

This module takes a database of its own (``pg_migrated_isolated``) because it
issues an unbounded ``refresh_continuous_aggregate``, which would move the
watermark past the current hour and silently break the current-bucket assertions
in ``test_usage_entity_aggregate_postgres``.
"""

import os
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

import pytest
from sqlalchemy import create_engine, exc, text
from sqlalchemy.pool import NullPool

from .conftest import TEST_CONFIG

SOURCE = "tzq"  # request_logs.source is VARCHAR(8)
VIEW = "request_counts_hourly_by_entity"

# Both instants are minutes from a UTC day boundary, in opposite directions, so a
# westward session timezone moves them into the previous UTC day (changing DOW)
# as well as into a different hour. AGG_HOUR=2 reads as 20/21 on CST6CDT;
# RAW_HOUR=0 reads as 18/19 on the previous day.
AGG_DAYS_AGO, AGG_HOUR = 2, 2
RAW_DAYS_AGO, RAW_HOUR = 60, 0

_INSERT = f"""
    INSERT INTO request_logs
        (time, entity_id, model_config_id, source, input_tokens, output_tokens,
         cost, duration, ttft_visible, outcome, aborted)
    VALUES (:time, :eid, :model, '{SOURCE}', 10, 20, 0.25, 1.5, 0.75, 'ok', false)
"""


def _instant(days_ago: int, hour: int) -> datetime:
    """A fixed UTC instant ``days_ago`` days back, at ``hour``:30 UTC."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(
        hour=hour, minute=30, second=0, microsecond=0
    )


def _expected_cell(when: datetime) -> tuple[int, int]:
    """The ``(dow, hour)`` the heatmap must report for ``when``, in UTC.

    PostgreSQL's ``EXTRACT(DOW ...)`` is 0=Sunday..6=Saturday, which is
    ``isoweekday() % 7``.
    """
    return when.isoweekday() % 7, when.hour


def _autocommit(engine):
    return engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def _refresh(engine, deadline=90.0):
    """Materialise the view's full history, retrying while a policy job holds it.

    The aggregate carries a background refresh policy and TimescaleDB refuses an
    overlapping refresh outright rather than waiting; same retry as
    ``test_usage_entity_aggregate_postgres``.
    """
    import time as _time
    end = _time.monotonic() + deadline
    while True:
        try:
            with _autocommit(engine) as conn:
                conn.execute(text(f"CALL refresh_continuous_aggregate('{VIEW}', NULL, NULL)"))
            return
        except exc.OperationalError as err:
            if "concurrent refresh" not in str(err) or _time.monotonic() > end:
                raise
            _time.sleep(1)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pg_app(pg_migrated_isolated):
    """A real Flask app bound to this module's migrated PostgreSQL database.

    Deliberately a copy of the fixture in ``test_usage_entity_aggregate_postgres``
    rather than a shared one: that module's fixtures are module-scoped against its
    own database, and this module must not refresh its aggregate. ``config.Config``
    reads ``DATABASE_URL`` at class-definition time and the root conftest's SQLite
    app has already imported it, so the attribute is patched too — otherwise this
    app quietly stays on SQLite, where every /usage endpoint returns an empty
    payload and nothing below would fail.
    """
    url = pg_migrated_isolated[0]
    previous_env = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    os.environ["CONFIG_YAML"] = TEST_CONFIG
    os.environ["BACKGROUND_WORKER"] = "false"
    from config import Config
    previous_uri = Config.SQLALCHEMY_DATABASE_URI
    Config.SQLALCHEMY_DATABASE_URI = url
    from lumen import create_app
    application = create_app()
    Config.SQLALCHEMY_DATABASE_URI = previous_uri
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        from lumen.extensions import db
        assert db.engine.dialect.name == "postgresql", (
            f"the test app is on {db.engine.dialect.name}; every assertion below "
            "would pass against the SQLite early return without touching Timescale"
        )
    yield application
    if previous_env is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous_env


def _client(pg_app, eid):
    client = pg_app.test_client()
    with client.session_transaction() as sess:
        sess["entity_id"] = eid
    return client


def _covers(pg_app, period):
    with pg_app.test_request_context():
        from lumen.blueprints.profile.routes import (
            _entity_aggregate_covers,
            _usage_period_start,
        )
        return _entity_aggregate_covers(_usage_period_start(period))


def _seed(engine, name, when):
    """One entity with exactly one request, at the absolute instant ``when``."""
    with engine.begin() as conn:
        model = conn.execute(text(
            "INSERT INTO model_configs (model_name, input_cost_per_million, "
            "output_cost_per_million) VALUES (:n, 1, 2) RETURNING id"
        ), {"n": f"tzq-model-{name}"}).scalar()
        eid = conn.execute(text(
            "INSERT INTO entities (entity_type, name, initials, active) "
            "VALUES ('user', :n, 'TZ', true) RETURNING id"
        ), {"n": f"tzq-{name}"}).scalar()
        conn.execute(text(_INSERT), {"time": when, "eid": eid, "model": model})
    return eid


def _heatmap(pg_app, eid, period):
    response = _client(pg_app, eid).get(f"/api/usage/heatmap?period={period}")
    assert response.status_code == HTTPStatus.OK
    return response.get_json()


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_heatmap_reports_the_utc_hour_and_day_not_the_servers_local_ones(
    pg_app, pg_migrated_isolated
):
    """The defect this whole module exists for, through the real endpoint.

    One request, at one known UTC instant, must land in one known cell of the
    7x24 grid. On a session left in the server's own timezone it lands in a
    different hour and a different day of the week, with no error.
    """
    engine = pg_migrated_isolated[1]

    # --- aggregate arm: seeded, then materialised, so the view reaches back
    # past the oldest raw row and _entity_aggregate_covers answers True.
    agg_when = _instant(AGG_DAYS_AGO, AGG_HOUR)
    agg_eid = _seed(engine, "agg", agg_when)
    _refresh(engine)
    assert _covers(pg_app, "week") is True, (
        "the aggregate arm is the one under test here; it was not taken"
    )
    assert _heatmap(pg_app, agg_eid, "week") == [
        {"dow": _expected_cell(agg_when)[0], "hour": _expected_cell(agg_when)[1], "count": 1}
    ]

    # --- raw arm: this row predates the aggregate's earliest bucket, which is
    # exactly the condition that sends every entity's query back to request_logs.
    raw_when = _instant(RAW_DAYS_AGO, RAW_HOUR)
    raw_eid = _seed(engine, "raw", raw_when)
    assert _covers(pg_app, "year") is False, (
        "the raw arm is the one under test here; it was not taken"
    )
    assert _heatmap(pg_app, raw_eid, "year") == [
        {"dow": _expected_cell(raw_when)[0], "hour": _expected_cell(raw_when)[1], "count": 1}
    ]


def test_every_connection_path_reports_a_utc_session(pg_app, pg_migrated_isolated):
    """The guarantee is per-connection, so check each way one gets opened.

    An engine-local ``connect_args`` would cover only the first of these; the
    listener in ``lumen/extensions.py`` is registered on the ``Engine`` class so
    that engines built outside the Flask app — the CLI's AUTOCOMMIT connections,
    ``db_pool``'s throwaway ``NullPool`` engine, anything a script creates — are
    covered by construction rather than by remembering.
    """
    url = pg_migrated_isolated[0]
    show = text("SHOW TimeZone")

    with pg_app.app_context():
        from lumen.extensions import db
        assert db.session.execute(show).scalar() == "UTC"

        # The CLI's own connection: not the request pool, and not in a transaction.
        from lumen.commands import _autocommit_connection
        with _autocommit_connection() as conn:
            assert conn.execute(show).scalar() == "UTC"

    # An unpooled engine built outside the app, as db_pool.query_max_connections does.
    throwaway = create_engine(url, poolclass=NullPool)
    try:
        with throwaway.connect() as conn:
            assert conn.execute(show).scalar() == "UTC"
    finally:
        throwaway.dispose()

    # A pool of one, held open, so the second connection is an overflow one:
    # every *new* DBAPI connection has to be set, not just the pool's first.
    small = create_engine(url, pool_size=1, max_overflow=1)
    try:
        with small.connect() as pooled, small.connect() as overflow:
            assert pooled.execute(show).scalar() == "UTC"
            assert overflow.execute(show).scalar() == "UTC"
    finally:
        small.dispose()
