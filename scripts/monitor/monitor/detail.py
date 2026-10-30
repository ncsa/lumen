"""Data for the model detail page: `/api/series?model=&range=`.

Every range returns the same series, so the page draws the same charts:

- `header`: the model's overview row (status, strip, uptime, instances, prices, cost)
- `strip_times`: when each strip cell starts (ISO times, or ISO dates for 1y)
- `prices`: our input/output price and the models.dev median and p5/p95; below 7d one
  point per bucket with the prices of the bucket's local day, each source carrying its
  latest earlier day forward when that day has no row (so the line steps at local
  midnight); from 7d up one point per local day
- `traffic`: below 7d one point per bucket with the average req/min and tok/min of
  the samples; from 7d up one point per local day with the day's requests and tokens,
  flagged `estimated` when the day is not exact (`estimated` or not yet `final`).
  Tokens come combined (`tokens`) and split (`input_tokens` / `output_tokens`); the
  split is None for data from before the monitor collected it
- `hardware`: one point per local day with the benefit (the day's input and output
  tokens at our own prices) and the hardware cost, both for a whole day (a day still
  being counted has its benefit projected), plus both summed over the days that have both,
  for a day still being counted only over its counted part
- `instances`: average healthy and configured instances per bucket from the samples
  (`unit: bucket`), or on 1y per local day from the daily counters (`unit: day`)
"""

from datetime import date, datetime, timedelta

from sqlalchemy import BigInteger, cast, exists, func, or_, select
from sqlalchemy.orm import Session

from monitor.buckets import Range, bucket_index, make_grid, range_days, status_rank
from monitor.config import Config
from monitor.daily import local_day
from monitor.models import DailyStat, Sample
from monitor.overview import _iso, counted_fraction, current_models, exact, model_rows, short_range_row, strip_times
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


# The price columns of each price source. The probe creates today's lumen row before
# the price run fills these, so a row with none of them set has no price yet.
PRICE_COLUMNS = {
    "lumen": ("input_price", "output_price"),
    "models.dev": tuple(f"{side}_{stat}" for side in ("input", "output") for stat in ("median", "p5", "p95")),
}


def _price_values(ours: DailyStat | None, dev: DailyStat | None) -> dict:
    values = {"input_price": ours.input_price if ours else None, "output_price": ours.output_price if ours else None}
    for side in ("input", "output"):
        for stat in ("median", "p5", "p95"):
            values[f"{side}_{stat}"] = getattr(dev, f"{side}_{stat}") if dev else None
    return values


def _price_points(rows: dict, days: list[date]) -> list[dict]:
    return [{"day": day.isoformat(), **_price_values(rows.get(("lumen", day)), rows.get(("models.dev", day)))} for day in days]


def _has_price(row: DailyStat | None, source: str) -> bool:
    return row is not None and any(getattr(row, c) is not None for c in PRICE_COLUMNS[source])


def _latest_before(session: Session, model_id: str, source: str, day: date) -> DailyStat | None:
    """The source's latest row with a price before `day`, or None."""
    priced = or_(*(getattr(DailyStat, c).is_not(None) for c in PRICE_COLUMNS[source]))
    stmt = (select(DailyStat).where(DailyStat.model == model_id, DailyStat.source == source, DailyStat.day < day, priced)
            .order_by(DailyStat.day.desc()).limit(1))
    return session.execute(stmt).scalars().first()


def _bucket_price_points(session: Session, model_id: str, rows: dict, days: list[date], grid, tz) -> list[dict]:
    """One point per bucket with the prices of the bucket's local day.

    A day without a priced row for a source carries that source's latest earlier one forward,
    starting from the latest one before the range; with none at all its values are None.
    """
    seed = {source: _latest_before(session, model_id, source, days[0]) for source in PRICE_COLUMNS}
    by_day: dict[str, dict[date, DailyStat | None]] = {}
    for source in PRICE_COLUMNS:
        row = seed[source]
        by_day[source] = {}
        for day in days:
            if _has_price(rows.get((source, day)), source):
                row = rows[(source, day)]
            by_day[source][day] = row
    points = []
    for t in grid.times():
        day = local_day(t, tz)
        ours, dev = (by_day[source].get(day, seed[source]) for source in PRICE_COLUMNS)
        points.append({"t": _iso(t), **_price_values(ours, dev)})
    return points


def daily_benefit(lumen: DailyStat | None) -> float | None:
    """The day's input and output tokens at our own prices, or None without the split counts or a price."""
    if lumen is None:
        return None
    values = (lumen.input_tokens, lumen.input_price, lumen.output_tokens, lumen.output_price)
    if any(v is None for v in values):
        return None
    return lumen.input_tokens / 1e6 * lumen.input_price + lumen.output_tokens / 1e6 * lumen.output_price


def _whole_day_benefit(lumen: DailyStat, tz) -> float | None:
    """The day's benefit projected to the whole day: input and output each divided by the part of the day
    its own watermark covers, since the two are counted separately. None while either side covers nothing."""
    total = 0.0
    for side in ("input", "output"):
        fraction = counted_fraction(lumen, f"{side}_tokens_covered_until", tz)
        if fraction <= 0:
            return None
        total += getattr(lumen, f"{side}_tokens") / 1e6 * getattr(lumen, f"{side}_price") / fraction
    return total


def _hardware_points(rows: dict, days: list[date], tz) -> tuple[list[dict], dict]:
    """Benefit and cost per day, both for a whole day, so a day still being counted (`partial`) compares
    like the others: its cost is the full day's and its benefit is projected from the counted part.

    The range totals add up the days that have both a benefit and a cost (`days` of them). A partial day
    adds only the part of the day both input and output are counted for (the shorter watermark): its
    projected benefit and its cost, each scaled to that part.
    """
    points = []
    total = {"benefit": 0.0, "cost": 0.0, "partial": False, "days": 0}
    for day in days:
        hw, lumen = rows.get(("hardware", day)), rows.get(("lumen", day))
        benefit = daily_benefit(lumen)
        cost = hw.daily_cost if hw else None
        partial = lumen is not None and not lumen.final
        fraction = 1.0
        if partial and benefit is not None:
            benefit = _whole_day_benefit(lumen, tz)
            fraction = min(counted_fraction(lumen, f"{side}_tokens_covered_until", tz) for side in ("input", "output"))
        estimated = lumen is not None and bool(lumen.estimated)
        points.append({"day": day.isoformat(), "benefit": benefit, "cost": cost, "partial": partial, "estimated": estimated})
        if benefit is not None and cost is not None:
            total["days"] += 1
            total["benefit"] += benefit * fraction
            total["cost"] += cost * fraction
            total["partial"] = total["partial"] or partial or estimated
    if not total["days"]:
        total = {"benefit": None, "cost": None, "partial": False, "days": 0}
    return points, total


# The sample columns the charts average per bucket; `tokens` is input + output, None when either is.
MEANS = ("healthy", "configured", "requests", "tokens", "input_tokens", "output_tokens")


def _bucket_stats(session: Session, model_id: str, grid, start: datetime, now: datetime) -> tuple[list[dict], dict]:
    """The model's sample means per bucket, and its strip, uptime and peak, from one grouped query.

    The database returns one row per bucket with the worst status, the busiest minute,
    and the sum and count of the known values of each averaged column; Python divides,
    so a mean is exactly what averaging the samples in Python gave. As in the model
    table, the peak also covers a sample at `now`, which ends the grid and so is in no bucket.
    """
    columns = {
        "healthy": Sample.healthy,
        "configured": Sample.configured,
        "requests": Sample.requests_last_minute,
        "tokens": cast(Sample.input_tokens_last_minute, BigInteger) + Sample.output_tokens_last_minute,
        "input_tokens": Sample.input_tokens_last_minute,
        "output_tokens": Sample.output_tokens_last_minute,
    }
    rows = select(
        bucket_index(Sample.sampled_at, grid).label("i"),
        status_rank(Sample.status).label("rank"),
        *(c.label(key) for key, c in columns.items()),
    ).where(Sample.model == model_id, Sample.sampled_at > start, Sample.sampled_at <= now).subquery()
    stmt = select(
        rows.c.i, func.max(rows.c.rank), func.max(rows.c.requests),
        *(f(rows.c[key]) for key in MEANS for f in (func.sum, func.count)),
    ).group_by(rows.c.i)
    means: list[dict] = [dict.fromkeys(MEANS) for _ in range(grid.count)]
    ranks: list[int | None] = [None] * grid.count
    peak = None
    for i, rank, busiest, *sums in session.execute(stmt).all():
        if busiest is not None:
            peak = max(peak or 0, busiest)
        if i >= grid.count:
            continue
        ranks[i] = rank
        for k, key in enumerate(MEANS):
            total, count = sums[2 * k], sums[2 * k + 1]
            means[i][key] = int(total) / count if count else None
    return means, short_range_row(ranks, peak)


def series(session: Session, config: Config, snapshot: LiveSnapshot | None, model_id: str, rng: Range, now: datetime) -> dict | None:
    """Everything `/api/series` returns, or None for a model the monitor has never seen."""
    model = find_model(session, snapshot, model_id)
    if model is None:
        return None
    tz = config.timezone
    days = range_days(rng, now, tz)
    grid = make_grid(rng, config.refresh.web, now, tz)
    times = [_iso(t) for t in grid.times()]
    if rng.daily:
        (header,) = model_rows(session, config, rng, now, [model])
    else:
        buckets, stats = _bucket_stats(session, model_id, grid, now - rng.span, now)
        (header,) = model_rows(session, config, rng, now, [model], {model_id: stats})
    rows = _rows_by_source(session, model_id, days)
    hardware, hardware_range = _hardware_points(rows, days, tz)

    if rng.daily:
        lumen = [rows.get(("lumen", d)) for d in days]
        instances = {"unit": "day", "points": [
            {"t": d.isoformat(), "healthy": r.healthy_sum / r.polls if r and r.polls else None, "configured": r.configured if r else None}
            for d, r in zip(days, lumen)
        ]}
    else:
        instances = {"unit": "bucket", "points": [
            {"t": t, "healthy": b["healthy"], "configured": b["configured"]} for t, b in zip(times, buckets)
        ]}

    if is_daily(rng):
        points = []
        for day in days:
            r = rows.get(("lumen", day))
            points.append({
                "t": day.isoformat(),
                "requests": r.requests if r else None,
                "tokens": r.tokens if r else None,
                "input_tokens": r.input_tokens if r else None,
                "output_tokens": r.output_tokens if r else None,
                "estimated": r is not None and not exact(r),
            })
        traffic = {"unit": "day", "points": points}
    else:
        traffic = {"unit": "minute", "points": [
            {"t": t, "requests": b["requests"], "tokens": b["tokens"], "input_tokens": b["input_tokens"],
             "output_tokens": b["output_tokens"], "estimated": False}
            for t, b in zip(times, buckets)
        ]}
    traffic["average"] = {
        key: _mean(p[key] for p in traffic["points"]) for key in ("requests", "tokens", "input_tokens", "output_tokens")
    }

    return {
        "range": rng.key,
        "model": model_id,
        "generated_at": _iso(now),
        "daily": is_daily(rng),
        "header": header,
        "strip_times": strip_times(grid),
        "prices": _price_points(rows, days) if is_daily(rng) else _bucket_price_points(session, model_id, rows, days, grid, tz),
        "traffic": traffic,
        "hardware": {"points": hardware, "range": hardware_range},
        "instances": instances,
    }
