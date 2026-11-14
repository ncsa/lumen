"""GET /v1/me month usage on real PostgreSQL/TimescaleDB.

A month reads ``request_counts_hourly_by_entity``. What is proven here:

1. Only that month is counted: a request at the month's start is in, one at
   its end (the next month's start) is out.
2. The current hour is counted: those rows are written after every refresh,
   so they sit above the watermark and only real-time aggregation sees them.
3. ``?period=all`` equals the lifetime totals ``/profile`` shows.
4. An empty month answers zeros and still lists every model the user can use.
5. A month older than the aggregate's 30-day refresh window, which the policy
   never materialises, is reported correctly once ``flask upgrade`` has run its
   backfill (``_backfill_missing_months``), with no manual ``backfill-aggregate``.
   The test first shows that month reading zero before the upgrade, so it would
   catch the upgrade step leaving it out.

Every background job in this module's private database is paused, so no policy
run moves a watermark under a test; the refreshes below stand in for it.
"""

import os
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

import pytest
from sqlalchemy import create_engine, text

from lumen.commands import ENTITY_AGGREGATE, upgrade_cmd
from tests.conftest import no_snapshot_refresher

from .conftest import TEST_CONFIG

pytestmark = pytest.mark.postgres


def _month(k):
    """First instant, in UTC, of the month ``k`` months before the current one."""
    now = datetime.now(timezone.utc)
    total = now.year * 12 + (now.month - 1) - k
    return datetime(total // 12, total % 12 + 1, 1, tzinfo=timezone.utc)


# The boundary month and the old month: both older than the 30-day policy window.
BOUNDARY = 3
OLD = 5


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


def _insert(conn, eid, model, when, inp=10, out=20, cost="0.25"):
    conn.execute(text(
        "INSERT INTO request_logs (time, entity_id, model_config_id, source, input_tokens, "
        "output_tokens, cost, duration) VALUES (:t, :eid, :m, 'api', :i, :o, :c, 0.1)"
    ), {"t": when, "eid": eid, "m": model, "i": inp, "o": out, "c": cost})


def _client(pg_app, eid):
    client = pg_app.test_client()
    with client.session_transaction() as sess:
        sess["entity_id"] = eid
    return client


def _me(pg_app, eid, period=None):
    url = "/v1/me" if period is None else f"/v1/me?period={period}"
    resp = _client(pg_app, eid).get(url)
    assert resp.status_code == HTTPStatus.OK, resp.get_data(as_text=True)
    return resp.get_json()


@pytest.fixture(scope="module")
def seeded(pg_app):
    """History, the refresh policy's run, ``flask upgrade``, then the current hour.

    Returns the ids, and ``before_upgrade``: what /v1/me answered for the old
    month after the policy's refresh and before the upgrade.
    """
    with pg_app.app_context():
        from lumen.extensions import db

        engine = db.engine
    with engine.begin() as conn:
        used, unused = (conn.execute(text(
            "INSERT INTO model_configs (model_name, input_cost_per_million, output_cost_per_million) "
            "VALUES (:n, 1, 2) RETURNING id"
        ), {"n": n}).scalar() for n in ("me-used", "me-unused"))
        ents = {
            key: conn.execute(text(
                "INSERT INTO entities (entity_type, name, email, initials, active) "
                "VALUES ('user', :n, :e, 'ME', true) RETURNING id"
            ), {"n": f"me-{key}", "e": f"me-{key}@example.invalid"}).scalar()
            for key in ("main", "old", "empty", "recent")
        }
        b_start, b_end = _month(BOUNDARY), _month(BOUNDARY - 1)
        # In the boundary month: its first instant and its last microsecond.
        _insert(conn, ents["main"], used, b_start, inp=1, out=2, cost="0.01")
        _insert(conn, ents["main"], used, b_end - timedelta(microseconds=1), inp=3, out=4, cost="0.02")
        # Out of it: the instant before it starts, and its end, the next month's start.
        _insert(conn, ents["main"], used, b_start - timedelta(microseconds=1), inp=100, out=100)
        _insert(conn, ents["main"], used, b_end, inp=200, out=200)
        # The old month, three requests.
        for day in (2, 10, 20):
            _insert(conn, ents["old"], used, _month(OLD) + timedelta(days=day), inp=5, out=6, cost="0.5")
        # Recent history, inside the policy's window.
        _insert(conn, ents["recent"], used, datetime.now(timezone.utc) - timedelta(days=10))
        # Lifetime figures, as the stats tables would hold them.
        conn.execute(text(
            "INSERT INTO model_stats (entity_id, model_config_id, source, requests, input_tokens, "
            "output_tokens, cost) VALUES (:e, :m, 'api', 7, 70, 140, 1.75)"
        ), {"e": ents["main"], "m": used})

    # The refresh policy's run: its 30-day window, ending three hours back. The
    # watermark moves past the old months, which it never materialises.
    now = datetime.now(timezone.utc)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(
            f"CALL refresh_continuous_aggregate('{ENTITY_AGGREGATE}', "
            f"CAST(:s AS timestamptz), CAST(:e AS timestamptz))"
        ), {"s": now - timedelta(days=30), "e": now - timedelta(hours=3)})
    before_upgrade = _me(pg_app, ents["old"], f"{_month(OLD):%Y-%m}")

    result = pg_app.test_cli_runner().invoke(upgrade_cmd, [])
    assert result.exit_code == 0, result.output
    assert "Upgrade complete." in result.output, result.output

    # The current hour, written after every refresh: above the watermark.
    with engine.begin() as conn:
        _insert(conn, ents["main"], used, datetime.now(timezone.utc), inp=11, out=22, cost="0.11")
        _insert(conn, ents["main"], used, datetime.now(timezone.utc), inp=11, out=22, cost="0.11")
    return {"entities": ents, "used": used, "unused": unused, "before_upgrade": before_upgrade}


def _usage(body, model="me-used"):
    [row] = [u for u in body["model_usage"] if u["model_name"] == model]
    return row


def test_only_that_month_is_counted(pg_app, seeded):
    body = _me(pg_app, seeded["entities"]["main"], f"{_month(BOUNDARY):%Y-%m}")
    assert body["period"] == {
        "name": f"{_month(BOUNDARY):%Y-%m}",
        "start": _month(BOUNDARY).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": _month(BOUNDARY - 1).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "usage_available": True,
    }
    # The start row and the last-microsecond row; not the rows on either side.
    assert body["totals"] == {"requests": 2, "input_tokens": 4, "output_tokens": 6,
                              "total_tokens": 10, "cost": pytest.approx(0.03)}
    assert (_usage(body)["requests"], _usage(body)["total_tokens"]) == (2, 10)


def test_a_request_at_the_end_belongs_to_the_next_month(pg_app, seeded):
    body = _me(pg_app, seeded["entities"]["main"], f"{_month(BOUNDARY - 1):%Y-%m}")
    assert (body["totals"]["requests"], body["totals"]["total_tokens"]) == (1, 400)


def test_the_current_hour_is_counted(pg_app, seeded):
    # The default period is the current UTC month.
    body = _me(pg_app, seeded["entities"]["main"])
    assert body["period"]["name"] == f"{datetime.now(timezone.utc):%Y-%m}"
    assert body["totals"]["requests"] == 2
    assert body["totals"]["total_tokens"] == 66
    assert body["totals"]["cost"] == pytest.approx(0.22)


def test_all_equals_the_lifetime_totals(pg_app, seeded):
    eid = seeded["entities"]["main"]
    body = _me(pg_app, eid, "all")
    assert body["period"] == {"name": "all", "start": None, "end": None, "usage_available": True}
    assert body["totals"] == {"requests": 7, "input_tokens": 70, "output_tokens": 140,
                              "total_tokens": 210, "cost": pytest.approx(1.75)}
    with pg_app.test_request_context():
        from lumen.blueprints.profile.routes import _get_profile_data

        status = _get_profile_data(eid, live_tail=True)["status"]
    assert body["totals"]["total_tokens"] == status["total_tokens_used"]
    assert body["totals"]["cost"] == pytest.approx(float(status["total_cost"]))


def test_an_empty_month_gives_zeros_and_lists_accessible_models(pg_app, seeded):
    body = _me(pg_app, seeded["entities"]["empty"])
    assert body["totals"] == {"requests": 0, "input_tokens": 0, "output_tokens": 0,
                              "total_tokens": 0, "cost": 0}
    names = {u["model_name"] for u in body["model_usage"]}
    assert {"me-used", "me-unused"} <= names
    assert all(u["requests"] == 0 and u["total_tokens"] == 0 for u in body["model_usage"])


def test_old_month_is_reported_after_the_upgrade_backfill(pg_app, seeded):
    period = f"{_month(OLD):%Y-%m}"
    # The policy's run left the old month behind the watermark: invisible.
    assert seeded["before_upgrade"]["totals"]["requests"] == 0
    body = _me(pg_app, seeded["entities"]["old"], period)
    assert body["totals"] == {"requests": 3, "input_tokens": 15, "output_tokens": 18,
                              "total_tokens": 33, "cost": pytest.approx(1.5)}
    assert _usage(body)["requests"] == 3

