"""Data for the model detail page: `/api/series?model=&range=`.

Every range returns the same series, so the page draws the same charts:

- `header`: the model's overview row (status, strip, uptime, instances, prices, cost)
- `prices`: one point per local day with our input/output price and the models.dev
  median and p5/p95
- `traffic`: below 7d one point per bucket with the average req/min and tok/min of
  the samples; from 7d up one point per local day with the day's requests and tokens,
  flagged `estimated` when the day is not exact (`estimated` or not yet `final`)
- `hardware`: one point per local day with the hardware $/day and its $/M tokens,
  or $/request on a day without token counts, plus the same over the whole range
- `instances`: average healthy and configured instances per bucket from the samples
  (`unit: bucket`), or on 1y per local day from the daily counters (`unit: day`)
"""

from datetime import date, datetime, timedelta

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from monitor.buckets import Range, make_grid, range_days
from monitor.config import Config
from monitor.models import DailyStat, Sample
from monitor.overview import _iso, current_models, exact, hardware_unit_cost, model_rows
from monitor.probe import LiveSnapshot

# From this span up, traffic is shown per local day instead of per bucket.
DAILY_FROM = timedelta(days=7)


def _mean(values) -> float | None:
    known = [v for v in values if v is not None]
    return sum(known) / len(known) if known else None


def is_daily(rng: Range) -> bool:
    return rng.span >= DAILY_FROM


def find_model(session: Session, snapshot: LiveSnapshot | None, model_id: str) -> dict | None:
    """The model as the overview knows it, a bare entry for a model with only history, or None."""
    _, models = current_models(session, snapshot)
    for m in models:
        if m["id"] == model_id:
            return m
    known = session.execute(select(or_(
        exists().where(Sample.model == model_id),
        exists().where(DailyStat.model == model_id),
    ))).scalar()
    return {"id": model_id, "description": None, "status": None, "configured": None, "healthy": None} if known else None


def _rows_by_source(session: Session, model_id: str, days: list[date]) -> dict[tuple[str, date], DailyStat]:
    stmt = select(DailyStat).where(DailyStat.model == model_id, DailyStat.day >= days[0], DailyStat.day <= days[-1])
    return {(r.source, r.day): r for r in session.execute(stmt).scalars().all()}


def _price_points(rows: dict, days: list[date]) -> list[dict]:
    points = []
    for day in days:
        ours, dev = rows.get(("lumen", day)), rows.get(("models.dev", day))
        point = {"day": day.isoformat(), "input_price": ours.input_price if ours else None, "output_price": ours.output_price if ours else None}
        for side in ("input", "output"):
            for stat in ("median", "p5", "p95"):
                point[f"{side}_{stat}"] = getattr(dev, f"{side}_{stat}") if dev else None
        points.append(point)
    return points


def _hardware_points(rows: dict, model_id: str, days: list[date], tz) -> tuple[list[dict], dict]:
    # hardware_unit_cost keys its rows by (source, model, day), as the overview does.
    keyed = {(source, model_id, day): row for (source, day), row in rows.items()}
    points = []
    for day in days:
        hw = rows.get(("hardware", day))
        points.append({"day": day.isoformat(), "daily_cost": hw.daily_cost if hw else None, **hardware_unit_cost(keyed, model_id, [day], tz)})
    return points, hardware_unit_cost(keyed, model_id, days, tz)


def _bucket_samples(session: Session, model_id: str, grid, start: datetime, now: datetime) -> list[list]:
    """The model's samples per bucket, as plain rows with only the columns the charts use."""
    stmt = select(
        Sample.sampled_at, Sample.healthy, Sample.configured, Sample.requests_last_minute, Sample.tokens_last_minute,
    ).where(Sample.model == model_id, Sample.sampled_at > start, Sample.sampled_at <= now)
    buckets: list[list] = [[] for _ in range(grid.count)]
    for s in session.execute(stmt).all():
        i = grid.index(s.sampled_at)
        if i is not None:
            buckets[i].append(s)
    return buckets


def series(session: Session, config: Config, snapshot: LiveSnapshot | None, model_id: str, rng: Range, now: datetime) -> dict | None:
    """Everything `/api/series` returns, or None for a model the monitor has never seen."""
    model = find_model(session, snapshot, model_id)
    if model is None:
        return None
    tz = config.timezone
    days = range_days(rng, now, tz)
    grid = make_grid(rng, config.refresh.web, now, tz)
    times = [_iso(t) for t in grid.times()]
    (header,) = model_rows(session, config, rng, now, [model])
    rows = _rows_by_source(session, model_id, days)
    hardware, hardware_range = _hardware_points(rows, model_id, days, tz)

    if rng.daily:
        lumen = [rows.get(("lumen", d)) for d in days]
        instances = {"unit": "day", "points": [
            {"t": d.isoformat(), "healthy": r.healthy_sum / r.polls if r and r.polls else None, "configured": r.configured if r else None}
            for d, r in zip(days, lumen)
        ]}
    else:
        buckets = _bucket_samples(session, model_id, grid, now - rng.span, now)
        instances = {"unit": "bucket", "points": [
            {"t": t, "healthy": _mean(s.healthy for s in b), "configured": _mean(s.configured for s in b)}
            for t, b in zip(times, buckets)
        ]}

    if is_daily(rng):
        points = []
        for day in days:
            r = rows.get(("lumen", day))
            points.append({
                "t": day.isoformat(),
                "requests": r.requests if r else None,
                "tokens": r.tokens if r else None,
                "estimated": r is not None and not exact(r),
            })
        traffic = {"unit": "day", "points": points}
    else:
        traffic = {"unit": "minute", "points": [
            {"t": t, "requests": _mean(s.requests_last_minute for s in b), "tokens": _mean(s.tokens_last_minute for s in b), "estimated": False}
            for t, b in zip(times, buckets)
        ]}
    traffic["average"] = {
        key: _mean(p[key] for p in traffic["points"]) for key in ("requests", "tokens")
    }

    return {
        "range": rng.key,
        "model": model_id,
        "generated_at": _iso(now),
        "daily": is_daily(rng),
        "header": header,
        "prices": _price_points(rows, days),
        "traffic": traffic,
        "hardware": {"points": hardware, "range": hardware_range},
        "instances": instances,
    }
