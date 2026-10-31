"""Data for the overview page: the Lumen card, the status cards and the model table.

The current model list, descriptions, instance counts and Lumen's own
request/token windows come from the live snapshot of the last successful
probe. Before the first probe after a restart they come from the samples of
the last successful probe in the database, without descriptions or windows.
Strips, uptime and peak req/min come from `probes`/`samples` (1y: the daily
rows), with `strip_times` giving when each strip cell starts; prices and
hardware cost from the latest daily rows.

A day's totals are exact only when its lumen row is `final` and not
`estimated`. A value built from any other day is flagged `partial`.
"""

from datetime import date, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from monitor.buckets import (
    DOWN,
    OK,
    STATUS_PRIORITY,
    UNKNOWN,
    DayGrid,
    Range,
    bucket_index,
    daily_status,
    lumen_daily_status,
    lumen_uptime,
    make_grid,
    model_uptime,
    probe_rank,
    range_days,
    rank_status,
    slot_start,
    status_rank,
    utc_slot,
)
from monitor.config import Config
from monitor.daily import local_day, local_midnight
from monitor.models import DailyStat, Probe, Sample
from monitor.probe import LiveSnapshot, lumen_state

MINUTES_PER_DAY = 24 * 60
# Model table sort keys: the row field they sort by and whether it is numeric
# (numeric columns sort descending first, text ascending).
SORT_KEYS = {
    "model": ("id", False),
    "uptime": ("uptime", True),
    "instances": ("healthy", True),
    "price": ("input_price", True),
    "modelsdev": ("modelsdev_input_median", True),
    "hw_day": ("hw_daily_cost", True),
    "hw_mtok": ("hw_per_mtok", True),
    "peak": ("peak_rpm", True),
    "requests": ("requests_24h", True),
    "tokens": ("tokens_24h", True),
}
DEFAULT_SORT = "model"


def _iso(at: datetime | None) -> str | None:
    """Naive UTC as ISO 8601 with a `Z`, for the browser to show in local time."""
    return None if at is None else at.isoformat(timespec="seconds") + "Z"


def strip_times(grid) -> list[str]:
    """When each status strip cell starts: ISO dates for 1y, ISO times (UTC) for the short ranges."""
    if isinstance(grid, DayGrid):
        return [d.isoformat() for d in grid.days]
    return [_iso(t) for t in grid.times()]


def _window(windows: dict | None, key: str) -> float | None:
    value = (windows or {}).get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _sum(values) -> float | None:
    """Sum of the known values; None when none is known."""
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def exact(row: DailyStat | None) -> bool:
    """A lumen row whose totals no longer change and were all counted."""
    return row is not None and row.final and not row.estimated


def current_models(session: Session, snapshot: LiveSnapshot | None) -> tuple[datetime | None, list[dict]]:
    """`(probe time, models)` from the live snapshot, or else from the last successful probe's samples."""
    snap = snapshot.get() if snapshot else None
    if snap is not None:
        return snap.polled_at, snap.models
    last = session.execute(select(func.max(Sample.sampled_at))).scalar_one_or_none()
    if last is None:
        return None, []
    rows = session.execute(select(Sample).where(Sample.sampled_at == last).order_by(Sample.model)).scalars().all()
    return last, [
        {"id": s.model, "description": None, "status": s.status, "configured": s.configured, "healthy": s.healthy,
         "input_price": None, "output_price": None, "requests": None, "tokens": None}
        for s in rows
    ]


# --- Lumen card ----------------------------------------------------------------


def lumen_card(session: Session, config: Config, rng: Range, now: datetime) -> dict:
    """State, response-time stats, uptime and the response-time strip over `rng`.

    Each probe's status comes from `probe_status` with `lumen.down_above`, so a
    probe where most models are down is down and lowers the uptime. There are no
    daily Lumen rows, so 1y buckets the probes that are still kept (`retention`)
    per local day; older days are `unknown`. A 1y day's status comes from
    `lumen_daily_status` and the uptime is `Σ probes up / Σ probes`.

    The database groups the probes: per bucket for the short ranges, and for 1y
    per 15-minute `utc_slot()`, which Python adds up per local day.
    """
    grid = make_grid(rng, config.refresh.web, now, config.timezone)
    in_range = (Probe.polled_at > now - rng.span, Probe.polled_at <= now)
    key = utc_slot(Probe.polled_at) if rng.daily else bucket_index(Probe.polled_at, grid)
    probes = select(
        key.label("key"),
        Probe.response_time_ms.label("rt"),
        probe_rank(Probe.success, Probe.model_count, Probe.models_up, config.down_above).label("rank"),
    ).where(*in_range).subquery()
    rank = probes.c.rank
    groups = session.execute(
        select(
            probes.c.key, func.count(),
            func.sum(case((rank != STATUS_PRIORITY[DOWN], 1), else_=0)),
            func.sum(case((rank == STATUS_PRIORITY[OK], 1), else_=0)),
            func.max(probes.c.rt), func.max(rank),
        ).group_by(probes.c.key)
    ).all()

    max_rt: list[float | None] = [None] * grid.count
    polls, polls_up, polls_ok = [0] * grid.count, [0] * grid.count, [0] * grid.count
    ranks: list[int | None] = [None] * grid.count
    for key, count, up, ok, rt, rank in groups:
        i = grid.index(slot_start(key)) if rng.daily else key if key < grid.count else None
        if i is None:
            continue
        polls[i] += count
        polls_up[i] += up
        polls_ok[i] += ok
        if rt is not None:
            max_rt[i] = max(max_rt[i] or 0.0, rt)
        ranks[i] = max(ranks[i] or 0, rank)
    if rng.daily:
        statuses = [lumen_daily_status(n, up, ok) for n, up, ok in zip(polls, polls_up, polls_ok)]
        uptime = 100 * sum(polls_up) / sum(polls) if sum(polls) else None
    else:
        statuses = [rank_status(r) for r in ranks]
        uptime = lumen_uptime(statuses)

    ms = Probe.response_time_ms
    min_ms, avg_ms, max_ms = session.execute(select(func.min(ms), func.avg(ms), func.max(ms)).where(*in_range)).one()
    last = session.execute(
        select(Probe.polled_at, ms).where(*in_range).order_by(Probe.polled_at.desc(), Probe.id.desc()).limit(1)
    ).one_or_none()
    return {
        "state": lumen_state(session, config.down_above),
        "last_ms": last.response_time_ms if last else None,
        "last_at": _iso(last.polled_at) if last else None,
        "min_ms": min_ms,
        "avg_ms": avg_ms,
        "max_ms": max_ms,
        "uptime": uptime,
        "buckets": [{"t": _iso(t), "status": s, "rt": rt} for t, s, rt in zip(grid.times(), statuses, max_rt)],
    }


# --- Daily rows ------------------------------------------------------------------


def _daily_rows(session: Session, days: list[date], models: list[str]) -> dict[tuple[str, str, date], DailyStat]:
    """lumen and hardware rows of `models` on `days`, keyed by `(source, model, day)`."""
    if not days or not models:
        return {}
    stmt = select(DailyStat).where(
        DailyStat.day >= days[0], DailyStat.day <= days[-1],
        DailyStat.source.in_(("lumen", "hardware")), DailyStat.model.in_(models),
    )
    return {(r.source, r.model, r.day): r for r in session.execute(stmt).scalars().all()}


def _latest(session: Session, source: str, column, models: list[str]) -> dict[str, DailyStat]:
    """Per model, the most recent `source` row where `column` is set."""
    if not models:
        return {}
    newest = (
        select(DailyStat.model, func.max(DailyStat.day).label("day"))
        .where(DailyStat.source == source, column.is_not(None), DailyStat.model.in_(models))
        .group_by(DailyStat.model)
        .subquery()
    )
    stmt = select(DailyStat).join(newest, (DailyStat.model == newest.c.model) & (DailyStat.day == newest.c.day)).where(DailyStat.source == source)
    return {r.model: r for r in session.execute(stmt).scalars().all()}


def counted_fraction(row: DailyStat, column: str, tz) -> float:
    """How much of the row's day its metric is counted for: 1 once final, else up to the watermark in `column`."""
    if row.final:
        return 1.0
    start = local_midnight(row.day, tz)
    length = (local_midnight(row.day + timedelta(days=1), tz) - start).total_seconds()
    watermark = getattr(row, column)
    if watermark is None:
        return 0.0
    return min(max((watermark - start).total_seconds() / length, 0.0), 1.0)


def hardware_unit_cost(rows: dict, model: str, days: list[date], tz) -> dict:
    """Hardware $ per million tokens over `days`, or $ per request when tokens are unknown.

    `Σ daily_cost / Σ tokens` over the days that have both. A day still being
    counted contributes its cost only for the part of the day its traffic covers,
    so a morning's tokens are not set against a whole day of hardware.
    """
    result = {"hw_per_mtok": None, "hw_per_request": None, "hw_partial": False}
    for metric, column, key, scale in (
        ("tokens", "tokens_covered_until", "hw_per_mtok", 1e6),
        ("requests", "covered_until", "hw_per_request", 1),
    ):
        cost = amount = 0.0
        partial = False
        for day in days:
            hw = rows.get(("hardware", model, day))
            lumen = rows.get(("lumen", model, day))
            if hw is None or hw.daily_cost is None or lumen is None or getattr(lumen, metric) is None:
                continue
            cost += hw.daily_cost * counted_fraction(lumen, column, tz)
            amount += getattr(lumen, metric)
            partial = partial or not exact(lumen)
        if amount > 0:
            result[key] = cost / (amount / scale)
            result["hw_partial"] = partial
            return result
    return result


def _day_minutes(row: DailyStat, tz) -> float:
    return counted_fraction(row, "covered_until", tz) * MINUTES_PER_DAY


# --- Model table ----------------------------------------------------------------


def _short_range_stats(session: Session, grid, start: datetime, now: datetime, models: list[str]) -> dict[str, dict]:
    """Strip, uptime and peak req/min per model from one query that returns a row per (model, bucket).

    The peak covers every sample in `(start, now]`, also one at `now`, which ends
    the grid and so is in no bucket.
    """
    rows = (
        select(
            Sample.model,
            bucket_index(Sample.sampled_at, grid).label("i"),
            status_rank(Sample.status).label("rank"),
            Sample.requests_last_minute.label("rpm"),
        )
        .where(Sample.sampled_at > start, Sample.sampled_at <= now, Sample.model.in_(models))
        .subquery()
    )
    stmt = select(rows.c.model, rows.c.i, func.max(rows.c.rank), func.max(rows.c.rpm)).group_by(rows.c.model, rows.c.i)
    ranks: dict[str, list[int | None]] = {m: [None] * grid.count for m in models}
    peaks: dict[str, int | None] = {m: None for m in models}
    for model, i, rank, peak in session.execute(stmt).all():
        if 0 <= i < grid.count:
            ranks[model][i] = rank
        if peak is not None:
            peaks[model] = max(peaks[model] or 0, peak)
    return {m: short_range_row(ranks[m], peaks[m]) for m in models}


def short_range_row(ranks: list[int | None], peak: int | None) -> dict:
    """A model's strip, uptime and peak req/min from its `MAX(status_rank())` per bucket and its busiest sample."""
    strip = [rank_status(r) for r in ranks]
    return {"strip": strip, "uptime": model_uptime(strip), "peak_rpm": peak}


def _yearly_stats(rows: dict, days: list[date], models: list[str], tz) -> dict[str, dict]:
    """1y: day status from the availability counters, uptime `Σ polls_ok / Σ polls`, peak as the busiest day's req/min."""
    stats = {}
    for m in models:
        day_rows = [rows.get(("lumen", m, d)) for d in days]
        strip = [daily_status(r.polls, r.polls_ok) if r else UNKNOWN for r in day_rows]
        polls = sum(r.polls or 0 for r in day_rows if r)
        polls_ok = sum(r.polls_ok or 0 for r in day_rows if r)
        rates = [r.requests / minutes for r in day_rows if r and r.requests is not None and (minutes := _day_minutes(r, tz)) > 0]
        stats[m] = {"strip": strip, "uptime": 100 * polls_ok / polls if polls else None, "peak_rpm": max(rates) if rates else None}
    return stats


def model_rows(session: Session, config: Config, rng: Range, now: datetime, models: list[dict],
               stats: dict[str, dict] | None = None) -> list[dict]:
    """One table row per current model; see the module docstring for where each value comes from.

    `stats` gives each model's strip, uptime and peak when the caller has already read them.
    """
    ids = [m["id"] for m in models]
    tz = config.timezone
    days = range_days(rng, now, tz)
    rows = _daily_rows(session, days, ids)
    if stats is None and rng.daily:
        stats = _yearly_stats(rows, days, ids, tz)
    elif stats is None:
        stats = _short_range_stats(session, make_grid(rng, config.refresh.web, now, tz), now - rng.span, now, ids)
    prices = _latest(session, "lumen", DailyStat.input_price, ids)
    modelsdev = _latest(session, "models.dev", DailyStat.input_median, ids)
    hardware = _latest(session, "hardware", DailyStat.daily_cost, ids)

    table = []
    for m in models:
        model_id = m["id"]
        price = prices.get(model_id)
        dev = modelsdev.get(model_id)
        hw = hardware.get(model_id)
        input_price = m.get("input_price") if m.get("input_price") is not None else (price.input_price if price else None)
        output_price = m.get("output_price") if m.get("output_price") is not None else (price.output_price if price else None)
        table.append({
            "id": model_id,
            "description": m.get("description"),
            "status": m.get("status") or UNKNOWN,
            **stats[model_id],
            "healthy": m.get("healthy"),
            "configured": m.get("configured"),
            "input_price": input_price,
            "output_price": output_price,
            "modelsdev_input_median": dev.input_median if dev else None,
            "modelsdev_input_p5": dev.input_p5 if dev else None,
            "modelsdev_input_p95": dev.input_p95 if dev else None,
            "modelsdev_output_median": dev.output_median if dev else None,
            "modelsdev_output_p5": dev.output_p5 if dev else None,
            "modelsdev_output_p95": dev.output_p95 if dev else None,
            "modelsdev_providers": dev.providers if dev else None,
            "hw_configured": config.hardware_for(model_id) is not None,
            "hw_daily_cost": hw.daily_cost if hw else None,
            **hardware_unit_cost(rows, model_id, days, tz),
            "requests_24h": _window(m.get("requests"), "last_day"),
            "tokens_24h": _window(m.get("tokens"), "last_day"),
        })
    return table


def sort_rows(rows: list[dict], sort: str | None, direction: str | None) -> tuple[list[dict], str, str]:
    """Sort the table; returns `(rows, sort, direction)` as applied.

    Unknown sort keys fall back to the model id. Without a direction, numeric
    columns sort descending and text ascending. Missing values always go last, and
    equal values keep the id order.
    """
    if sort not in SORT_KEYS:
        sort = DEFAULT_SORT
    field, numeric = SORT_KEYS[sort]
    if direction not in ("asc", "desc"):
        direction = "desc" if numeric else "asc"
    by_id = sorted(rows, key=lambda r: r["id"])
    known = [r for r in by_id if r[field] is not None]
    missing = [r for r in by_id if r[field] is None]
    # A stable sort, also when reversed: equal values stay in id order.
    known.sort(key=lambda r: r[field] if numeric else str(r[field]).lower(), reverse=direction == "desc")
    return known + missing, sort, direction


# --- Status cards ---------------------------------------------------------------


def status_cards(session: Session, config: Config, now: datetime, models: list[dict]) -> dict:
    """Active models, healthy/configured instances and requests/tokens in the last hour and today."""
    today = local_day(now, config.timezone)
    ids = [m["id"] for m in models]
    today_rows = []
    if ids:
        stmt = select(DailyStat).where(DailyStat.day == today, DailyStat.source == "lumen", DailyStat.model.in_(ids))
        today_rows = session.execute(stmt).scalars().all()
    return {
        "active_models": len(models),
        "healthy": _sum(m.get("healthy") for m in models),
        "configured": _sum(m.get("configured") for m in models),
        "requests_last_hour": _sum(_window(m.get("requests"), "last_hour") for m in models),
        "requests_today": _sum(r.requests for r in today_rows),
        "tokens_last_hour": _sum(_window(m.get("tokens"), "last_hour") for m in models),
        "tokens_today": _sum(r.tokens for r in today_rows),
        "today_estimated": any(r.estimated for r in today_rows),
    }


def overview(session: Session, config: Config, snapshot: LiveSnapshot | None, rng: Range, now: datetime,
             sort: str | None = None, direction: str | None = None) -> dict:
    """Everything `/api/overview` returns."""
    probed_at, models = current_models(session, snapshot)
    rows, sort, direction = sort_rows(model_rows(session, config, rng, now, models), sort, direction)
    return {
        "range": rng.key,
        "generated_at": _iso(now),
        "probed_at": _iso(probed_at),
        "cards": status_cards(session, config, now, models),
        "models": rows,
        "strip_times": strip_times(make_grid(rng, config.refresh.web, now, config.timezone)),
        "sort": sort,
        "direction": direction,
    }
