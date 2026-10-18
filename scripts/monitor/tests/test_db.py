from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from monitor.db import make_engine, make_session_factory, utcnow
from monitor.migrate import upgrade
from monitor.models import DailyStat, Probe, Sample


@pytest.fixture
def sqlite_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'monitor.db'}"
    upgrade(url)
    return url


def test_sqlite_uses_wal(sqlite_url):
    engine = make_engine(sqlite_url)
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
    engine.dispose()


def test_utcnow_is_naive():
    assert utcnow().tzinfo is None


def test_rows_round_trip(sqlite_url):
    engine = make_engine(sqlite_url)
    Session = make_session_factory(engine)
    now = utcnow()
    with Session() as session:
        session.add(DailyStat(day=date(2026, 10, 17), model="m", source="lumen", requests=12.5))
        session.add(Probe(polled_at=now, success=True, response_time_ms=180.0, model_count=2, models_up=2))
        session.add(Sample(model="m", sampled_at=now, status="ok", configured=1, healthy=1, requests_last_minute=3))
        session.commit()
    with Session() as session:
        row = session.execute(select(DailyStat)).scalar_one()
        assert (row.polls, row.polls_ok, row.healthy_sum, row.estimated, row.final) == (0, 0, 0, False, False)
        assert row.last_updated is not None and row.last_updated.tzinfo is None
        assert session.execute(select(Probe)).scalar_one().polled_at == now
        assert session.execute(select(Sample)).scalar_one().tokens_last_minute is None
    engine.dispose()


def test_daily_stats_unique_per_day_model_source(sqlite_url):
    engine = make_engine(sqlite_url)
    Session = make_session_factory(engine)
    with Session() as session:
        day = date(2026, 10, 17)
        session.add_all([DailyStat(day=day, model="m", source=s) for s in ("models.dev", "lumen", "hardware")])
        session.commit()
        session.add(DailyStat(day=day, model="m", source="lumen"))
        with pytest.raises(IntegrityError):
            session.commit()
    engine.dispose()


def test_daily_stats_rejects_unknown_source(sqlite_url):
    engine = make_engine(sqlite_url)
    Session = make_session_factory(engine)
    with Session() as session:
        session.add(DailyStat(day=date(2026, 10, 17), model="m", source="other"))
        with pytest.raises(IntegrityError):
            session.commit()
    engine.dispose()
