"""The question this schema exists to answer.

"How many distinct users were waiting for model X at 09:05?" — asked after a
class-start incident, when nobody was watching a live dashboard at the time.

It is a pure interval-overlap count over one hypertable, with no extra writes
and no live state, *provided* each row carries an absolute arrival time. The
tests below also pin down why it has to be stored rather than reconstructed:
``test_derivation_from_duration_is_wrong`` builds a row whose preflight and
billing time are non-zero and shows the old ``time - duration - queue_wait``
formula landing in the wrong place.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.postgres

# A request is "waiting" from the moment it arrives until the first thing the
# user can see appears: queueing for a worker, preflight, then the model's own
# time-to-first-visible-token. Everything after that is the response streaming,
# which is not waiting.
WAITING_AT = text("""
    SELECT COUNT(DISTINCT entity_id)
    FROM request_logs
    WHERE model_config_id = :model
      AND started_at <= :t
      AND started_at
          + make_interval(secs => COALESCE(queue_wait, 0)
                                 + COALESCE(preflight, 0)
                                 + COALESCE(ttft_visible, 0)) >= :t
""")


def _insert(conn, *, entity, model, started_at, queue_wait, preflight, ttft_visible,
            duration=5.0):
    conn.execute(text("""
        INSERT INTO request_logs
            (time, entity_id, model_config_id, source, duration,
             started_at, queue_wait, preflight, ttft_visible, outcome)
        VALUES
            (:completed, :entity, :model, 'api', :duration,
             :started_at, :qw, :pf, :ttft_v, 'ok')
    """), {
        "completed": started_at + timedelta(seconds=queue_wait + preflight + ttft_visible + duration),
        "entity": entity, "model": model, "duration": duration,
        "started_at": started_at, "qw": queue_wait, "pf": preflight, "ttft_v": ttft_visible,
    })


def _seed_refs(conn):
    """entity_id and model_config_id are real foreign keys; give them targets."""
    conn.execute(text("DELETE FROM request_logs"))
    for eid in (1, 2, 3, 4, 9):
        conn.execute(text("""
            INSERT INTO entities (id, entity_type, name, initials, active)
            VALUES (:id, 'user', :name, 'XX', true)
            ON CONFLICT (id) DO NOTHING
        """), {"id": eid, "name": f"user{eid}"})
    for mid in (1, 2):
        conn.execute(text("""
            INSERT INTO model_configs
                (id, model_name, input_cost_per_million, output_cost_per_million)
            VALUES (:id, :name, 0, 0)
            ON CONFLICT (id) DO NOTHING
        """), {"id": mid, "name": f"model{mid}"})


@pytest.fixture
def seeded(pg_migrated):
    """Three students hit model 1 at a class start; one hits model 2."""
    base = datetime(2026, 9, 1, 9, 5, 0, tzinfo=timezone.utc)
    with pg_migrated.begin() as conn:
        _seed_refs(conn)
        # Alice: arrives 09:04:50, waits 20s total -> waiting across 09:05:00.
        _insert(conn, entity=1, model=1, started_at=base - timedelta(seconds=10),
                queue_wait=12.0, preflight=3.0, ttft_visible=5.0)
        # Bob: same, arrives a little later, still waiting at 09:05:00.
        _insert(conn, entity=2, model=1, started_at=base - timedelta(seconds=2),
                queue_wait=8.0, preflight=1.0, ttft_visible=4.0)
        # Bob again — a second concurrent request. Must NOT double-count.
        _insert(conn, entity=2, model=1, started_at=base - timedelta(seconds=1),
                queue_wait=9.0, preflight=1.0, ttft_visible=4.0)
        # Carol: finished waiting well before 09:05:00.
        _insert(conn, entity=3, model=1, started_at=base - timedelta(seconds=120),
                queue_wait=1.0, preflight=0.5, ttft_visible=0.5)
        # Dave: waiting at 09:05:00 but on a different model.
        _insert(conn, entity=4, model=2, started_at=base - timedelta(seconds=5),
                queue_wait=10.0, preflight=1.0, ttft_visible=2.0)
    return base


def test_counts_distinct_users_waiting_at_an_instant(pg_migrated, seeded):
    with pg_migrated.connect() as conn:
        n = conn.execute(WAITING_AT, {"model": 1, "t": seeded}).scalar()
    # Alice and Bob. Bob's two concurrent requests count once; Carol had already
    # been served; Dave was waiting on another model.
    assert n == 2


def test_user_with_two_concurrent_requests_counts_once(pg_migrated, seeded):
    """The failure mode a naive live counter has, checked against the durable data."""
    with pg_migrated.connect() as conn:
        distinct = conn.execute(WAITING_AT, {"model": 1, "t": seeded}).scalar()
        rows = conn.execute(text("""
            SELECT COUNT(*) FROM request_logs
            WHERE model_config_id = 1 AND started_at <= :t
              AND started_at + make_interval(secs => queue_wait + preflight + ttft_visible) >= :t
        """), {"t": seeded}).scalar()
    assert rows == 3, "three in-flight requests"
    assert distinct == 2, "but only two distinct users"


def test_nobody_is_waiting_long_before_the_burst(pg_migrated, seeded):
    with pg_migrated.connect() as conn:
        n = conn.execute(WAITING_AT, {"model": 1, "t": seeded - timedelta(minutes=10)}).scalar()
    assert n == 0


def test_derivation_from_duration_is_wrong(pg_migrated, seeded):
    """Why started_at is stored and not reconstructed.

    ``time - duration - queue_wait`` assumes preflight and the billing commit
    take zero time. They do not, and both grow under load — so the reconstructed
    arrival drifts later exactly when the numbers matter, and the request looks
    like it started after it really did.
    """
    with pg_migrated.connect() as conn:
        real, derived, pf = conn.execute(text("""
            SELECT started_at,
                   time - make_interval(secs => duration + queue_wait),
                   preflight
            FROM request_logs
            WHERE entity_id = 1 AND model_config_id = 1
        """)).one()

    drift = (derived - real).total_seconds()
    assert pf > 0
    # The derived value lands late by at least the unmeasured preflight.
    assert drift >= pf - 0.001, (
        f"derived arrival drifted {drift}s from the real one; this is the error "
        "the stored column removes"
    )


def test_null_timing_columns_do_not_break_the_query(pg_migrated):
    """Pre-migration rows, and requests that never passed through the bridge.

    They must be excluded rather than counted at an invented time — which is
    what a backfilled zero would have caused.
    """
    with pg_migrated.begin() as conn:
        _seed_refs(conn)
        conn.execute(text("""
            INSERT INTO request_logs (time, entity_id, model_config_id, source, duration)
            VALUES (now(), 9, 1, 'api', 2.0)
        """))
    with pg_migrated.connect() as conn:
        n = conn.execute(WAITING_AT, {"model": 1, "t": datetime.now(timezone.utc)}).scalar()
    assert n == 0
