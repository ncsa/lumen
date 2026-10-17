"""Per-model request rates read from ``request_metrics_1m`` on real Timescale.

``/v1/models`` and the model detail page used to count raw ``request_logs``
rows; on PostgreSQL they now sum the real-time one-minute aggregate. Proven
here:

1. Rows for one model split across two ``source`` values are summed, and the
   windows land on the same whole-minute edges the SQLite path uses, for the
   request count and the input and output token sums alike.
2. A row in the current minute shows in ``last_hour``/``last_day`` with no
   refresh — it is above the watermark, so only real-time aggregation sees it —
   and not in ``last_minute``, which is the previous whole minute only.
3. Materialising everything with ``refresh_continuous_aggregate`` changes no
   count.
4. Neither call site selects from ``request_logs``.

The clock is pinned to the database's ``now()`` (``request_rates._now``) so the
row ages below are exact. This module runs against ``pg_migrated_isolated`` —
a database of its own — because it refreshes the aggregate, which moves its
watermark for every later module that shares the database.
"""

import os
from datetime import timedelta, timezone
from http import HTTPStatus

import pytest
from sqlalchemy import event, exc, text

from .conftest import TEST_CONFIG

VIEW = "request_metrics_1m"
MONITOR = "monitor-rates-pg"


def _refresh(engine, deadline=90.0):
    """Materialise the whole view, retrying while its one-minute policy job holds it."""
    import time as _time
    end = _time.monotonic() + deadline
    while True:
        try:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text(f"CALL refresh_continuous_aggregate('{VIEW}', NULL, NULL)"))
            return
        except exc.OperationalError as err:
            if "concurrent refresh" not in str(err) or _time.monotonic() > end:
                raise
            _time.sleep(1)


@pytest.fixture(scope="module")
def pg_migrated(pg_migrated_isolated):
    return pg_migrated_isolated[1]


@pytest.fixture(scope="module")
def pg_app(pg_migrated_isolated):
    """A real Flask app bound to the migrated PostgreSQL database.

    ``config.Config`` reads DATABASE_URL at class-definition time and the root
    conftest's SQLite app has already imported it, so the URI is swapped on the
    class for the duration of ``create_app`` and restored afterwards.
    """
    pg_url = pg_migrated_isolated[0]
    previous_env = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_url
    os.environ["CONFIG_YAML"] = TEST_CONFIG
    os.environ["BACKGROUND_WORKER"] = "false"
    from config import Config
    previous_uri = Config.SQLALCHEMY_DATABASE_URI
    Config.SQLALCHEMY_DATABASE_URI = pg_url
    from lumen import create_app
    application = create_app()
    Config.SQLALCHEMY_DATABASE_URI = previous_uri
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    application.config["YAML_DATA"] = {**application.config.get("YAML_DATA", {}),
                                       "api": {"monitoring": {"token": MONITOR}}}
    with application.app_context():
        from lumen.extensions import db
        assert db.engine.dialect.name == "postgresql", (
            f"the test app is on {db.engine.dialect.name}; every assertion below "
            "would exercise the SQLite path"
        )
    yield application
    if previous_env is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous_env


@pytest.fixture(scope="module")
def now(pg_migrated):
    with pg_migrated.connect() as conn:
        return conn.execute(text("SELECT now()")).scalar().astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch, now):
    from lumen.blueprints.api import routes as api_routes
    from lumen.services import request_rates
    monkeypatch.setattr(request_rates, "_now", lambda: now)
    monkeypatch.setitem(api_routes._rates_cache, "data", {})
    monkeypatch.setitem(api_routes._rates_cache, "expires_at", 0.0)


def _log(model_id, when, source, input_tokens=0, output_tokens=0):
    from lumen.extensions import db
    from lumen.models.request_log import RequestLog
    db.session.add(RequestLog(time=when, model_config_id=model_id, source=source,
                              input_tokens=input_tokens, output_tokens=output_tokens))


@pytest.fixture(scope="module")
def seeded(pg_app, now):
    """History only — no row in the current minute yet.

    Model ``a`` has rows in both ``api`` and ``chat`` in the previous whole
    minute, 30 minutes ago and 5 hours ago, plus one 2 days ago; model ``b``
    has one row 30 minutes ago; one row has no model.
    """
    previous_minute = now.replace(second=0, microsecond=0) - timedelta(seconds=30)
    with pg_app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.model_config import ModelConfig
        a = ModelConfig(model_name="rates-a", input_cost_per_million=1, output_cost_per_million=2)
        b = ModelConfig(model_name="rates-b", input_cost_per_million=1, output_cost_per_million=2)
        user = Entity(entity_type="user", email="rates@example.com", name="Rates", initials="RA", active=True)
        db.session.add_all([a, b, user])
        db.session.flush()
        for when, source, input_tokens, output_tokens in [
            (previous_minute, "api", 1, 2),
            (previous_minute, "chat", 10, 20),
            (now - timedelta(minutes=30), "api", 100, 200),
            (now - timedelta(minutes=30), "chat", 1000, 2000),
            (now - timedelta(minutes=30), "chat", 1000, 2000),
            (now - timedelta(hours=5), "api", 10000, 20000),
            (now - timedelta(days=2), "chat", 100000, 200000),
        ]:
            _log(a.id, when, source, input_tokens, output_tokens)
        _log(b.id, now - timedelta(minutes=30), "api", 5, 6)
        _log(None, previous_minute, "api")
        db.session.commit()
        return {"a": a.id, "b": b.id, "user": user.id}


def _rates(pg_app, model_config_id=None):
    from lumen.services import request_rates
    with pg_app.app_context():
        return request_rates.rates(model_config_id)


def _windows(minute, hour, day):
    return {"last_minute": minute, "last_hour": hour, "last_day": day}


B_RATES = {
    "requests": _windows(0, 1, 1),
    "input_tokens": _windows(0, 5, 5),
    "output_tokens": _windows(0, 6, 6),
}


@pytest.mark.postgres
def test_rates_sum_sources_and_honour_the_windows(pg_app, seeded):
    result = _rates(pg_app)
    assert result == {
        seeded["a"]: {
            "requests": _windows(2, 5, 6),
            "input_tokens": _windows(11, 2111, 12111),
            "output_tokens": _windows(22, 4222, 24222),
        },
        seeded["b"]: B_RATES,
    }
    # The aggregate's sums are numeric; the reader hands back plain ints.
    assert all(type(v) is int for m in result.values() for w in m.values() for v in w.values())
    assert _rates(pg_app, seeded["b"]) == {seeded["b"]: B_RATES}


@pytest.mark.postgres
def test_current_minute_counts_without_a_refresh_and_survives_one(pg_app, pg_migrated, seeded, now):
    """The current-minute row is reached only through real-time aggregation.

    History is materialised first, which moves the watermark to the start of
    the current minute; the row written afterwards sits above it.
    """
    _refresh(pg_migrated)
    with pg_app.app_context():
        from lumen.extensions import db
        _log(seeded["a"], now, "api", 7, 9)
        db.session.commit()

    expected = {
        "requests": _windows(2, 6, 7),
        "input_tokens": _windows(11, 2118, 12118),
        "output_tokens": _windows(22, 4231, 24231),
    }
    assert _rates(pg_app, seeded["a"]) == {seeded["a"]: expected}

    _refresh(pg_migrated)
    assert _rates(pg_app, seeded["a"]) == {seeded["a"]: expected}


@pytest.mark.postgres
def test_call_sites_never_select_from_request_logs(pg_app, seeded):
    from lumen.extensions import db

    client = pg_app.test_client()
    with client.session_transaction() as sess:
        sess["entity_id"] = seeded["user"]
        sess["entity_name"] = "Rates"
        sess["initials"] = "RA"

    statements = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with pg_app.app_context():
        engine = db.engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        auth = {"Authorization": f"Bearer {MONITOR}"}
        listed = client.get("/v1/models", headers=auth)
        assert listed.status_code == HTTPStatus.OK
        one = client.get("/v1/models/rates-a", headers=auth)
        assert one.status_code == HTTPStatus.OK
        page = client.get("/models/rates-a")
        assert page.status_code == HTTPStatus.OK
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    models = {m["id"]: m["requests"] for m in listed.get_json()["data"]}
    assert models["rates-a"]["last_hour"] >= 5
    assert one.get_json()["requests"] == models["rates-a"]
    # /v1/models fills the per-worker cache and get_model reuses it, so the
    # aggregate is read twice: once for the list and once for the detail page.
    assert sum(VIEW in st for st in statements) == 2, statements
    assert not [st for st in statements if "request_logs" in st], statements
