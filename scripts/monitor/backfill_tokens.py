"""Backfill the monitor's token counts from Lumen's request_logs.

Fills the input and output tokens:

- `samples.{input,output}_tokens_last_minute`: tokens in the last complete
  minute before the probe, the same window Lumen's `last_minute` uses. Only NULL
  values are filled.
- `daily_stats.{input,output}_tokens` (source = lumen): tokens during the local
  day in the monitor's timezone. Only days before today, and only rows that
  already exist.

And creates the history before the monitor started, back to Lumen's first
request: for every model and local day with traffic and no lumen row yet, a
final lumen row with the exact requests and tokens and the prices fitted to
what was billed that day, plus a hardware row assuming the model ran all day
on its active endpoints. models.dev rows and uptime have no history and are
left out.

Run from scripts/monitor:

    uv run python backfill_tokens.py --lumen-db postgresql://lumen:PASS@HOST:PORT/lumen --dry-run

Pass `--day YYYY-MM-DD` (a local day in the monitor's timezone, before today) to
redo only that day's lumen and hardware rows; the per-minute samples are skipped.
"""

import argparse
import os
from datetime import date, timedelta

from sqlalchemy import bindparam, create_engine, func, or_, select, text, update

from monitor import hardware
from monitor.config import load_config
from monitor.daily import local_day
from monitor.db import make_engine, make_session_factory, utcnow
from monitor.models import DailyStat, Sample
from monitor.totals import local_midnight

MINUTE = timedelta(minutes=1)
# Below this relative determinant the day's traffic cannot tell the input and
# output price apart (e.g. every request had the same input/output ratio).
MIN_DET = 1e-6
# Each token metric and the daily_stats column holding how far it is counted.
TOKEN_METRICS = {
    "input_tokens": "input_tokens_covered_until",
    "output_tokens": "output_tokens_covered_until",
}

MINUTE_SQL = text("""
    SELECT mc.model_name, date_trunc('minute', rl.time AT TIME ZONE 'UTC') AS minute,
           sum(rl.input_tokens) AS input_tokens, sum(rl.output_tokens) AS output_tokens
    FROM request_logs rl JOIN model_configs mc ON mc.id = rl.model_config_id
    WHERE rl.time >= :start AND rl.time < :end
    GROUP BY 1, 2
""")

DAY_SQL = text("""
    SELECT mc.model_name, (rl.time AT TIME ZONE :tz)::date AS day,
           sum(rl.input_tokens) AS input_tokens, sum(rl.output_tokens) AS output_tokens
    FROM request_logs rl JOIN model_configs mc ON mc.id = rl.model_config_id
    WHERE rl.time >= :start AND rl.time < :end
    GROUP BY 1, 2
""")

# Per model and local day: traffic, plus the sums of the least-squares fit
# cost = input_price × input_tokens + output_price × output_tokens over the
# text requests (audio requests are billed per hour, not per token).
HISTORY_SQL = text("""
    SELECT mc.model_name, (rl.time AT TIME ZONE :tz)::date AS day,
           count(*) AS requests, sum(rl.input_tokens) AS input_tokens, sum(rl.output_tokens) AS output_tokens,
           sum(rl.input_tokens::float8 * rl.input_tokens) FILTER (WHERE rl.audio_seconds = 0) AS ii,
           sum(rl.input_tokens::float8 * rl.output_tokens) FILTER (WHERE rl.audio_seconds = 0) AS io,
           sum(rl.output_tokens::float8 * rl.output_tokens) FILTER (WHERE rl.audio_seconds = 0) AS oo,
           sum(rl.cost::float8 * rl.input_tokens) FILTER (WHERE rl.audio_seconds = 0) AS ci,
           sum(rl.cost::float8 * rl.output_tokens) FILTER (WHERE rl.audio_seconds = 0) AS co
    FROM request_logs rl JOIN model_configs mc ON mc.id = rl.model_config_id
    WHERE rl.time < :end AND rl.time >= COALESCE(CAST(:start AS timestamptz), '-infinity')
    GROUP BY 1, 2
""")

MODELS_SQL = text("""
    SELECT mc.model_name, mc.input_cost_per_million, mc.output_cost_per_million,
           (SELECT count(*) FROM model_endpoints me WHERE me.model_config_id = mc.id AND me.active) AS endpoints
    FROM model_configs mc
""")


def utc(naive):
    """Bind a naive UTC datetime against a timestamptz column."""
    return naive.isoformat() + "+00:00"


def split(input_tokens, output_tokens) -> dict:
    """The token metrics from an input / output token pair."""
    return {"input_tokens": int(input_tokens), "output_tokens": int(output_tokens)}


def backfill_samples(lumen, session) -> int:
    missing = or_(Sample.input_tokens_last_minute.is_(None), Sample.output_tokens_last_minute.is_(None))
    first, last = session.execute(select(func.min(Sample.sampled_at), func.max(Sample.sampled_at)).where(missing)).one()
    if first is None:
        return 0
    with lumen.connect() as conn:
        rows = conn.execute(MINUTE_SQL, {"start": utc(first - 2 * MINUTE), "end": utc(last)}).all()
    tokens = {(model, minute): split(i, o) for model, minute, i, o in rows}
    zero = split(0, 0)

    stmt = select(Sample.id, Sample.model, Sample.sampled_at).where(missing)
    values = [
        {"sid": sid, **tokens.get((model, at.replace(second=0, microsecond=0) - MINUTE), zero)}
        for sid, model, at in session.execute(stmt).all()
    ]
    if values:
        table = Sample.__table__
        session.connection().execute(
            update(table).where(table.c.id == bindparam("sid")).values({
                f"{metric}_last_minute": func.coalesce(table.c[f"{metric}_last_minute"], bindparam(metric))
                for metric in TOKEN_METRICS
            }),
            values,
        )
    return len(values)


def backfill_days(lumen, session, tz, only_day=None) -> int:
    today = local_day(utcnow(), tz)
    stmt = select(DailyStat).where(DailyStat.source == "lumen", DailyStat.day < today)
    if only_day is not None:
        stmt = stmt.where(DailyStat.day == only_day)
    rows = session.execute(stmt).scalars().all()
    if not rows:
        return 0
    first = min(r.day for r in rows)
    with lumen.connect() as conn:
        found = conn.execute(DAY_SQL, {"tz": tz.key, "start": utc(local_midnight(first, tz)), "end": utc(local_midnight(today, tz))}).all()
    tokens = {(model, day): split(i, o) for model, day, i, o in found}
    zero = split(0, 0)

    # Only move a token watermark for models the live collector already counts
    # that metric for; a new watermark would hold back closing later days.
    watermarked = watermarked_models(session)

    for row in rows:
        day_end = local_midnight(row.day + timedelta(days=1), tz)
        for metric, column in TOKEN_METRICS.items():
            setattr(row, metric, tokens.get((row.model, row.day), zero)[metric])
            if row.model in watermarked[metric]:
                setattr(row, column, max(getattr(row, column) or day_end, day_end))
        row.last_updated = utcnow()
    return len(rows)


def watermarked_models(session) -> dict[str, set[str]]:
    """Per token metric, the models whose lumen rows have a watermark for it."""
    return {
        metric: set(session.execute(
            select(DailyStat.model).where(DailyStat.source == "lumen", getattr(DailyStat, column).is_not(None)).distinct()
        ).scalars())
        for metric, column in TOKEN_METRICS.items()
    }


def fit_prices(ii, io, oo, ci, co) -> tuple[float, float] | None:
    """The `(input, output)` $/M tokens that best explain a day's billed cost, or None when it cannot tell."""
    if not ii or not oo:
        return None
    det = ii * oo - io * io
    if det <= MIN_DET * ii * oo:
        return None
    price_in = (ci * oo - co * io) / det * 1e6
    price_out = (co * ii - ci * io) / det * 1e6
    if price_in < 0 or price_out < 0:
        return None
    return round(price_in, 6), round(price_out, 6)


def backfill_history(lumen, session, config, only_day=None) -> tuple[int, int, int]:
    """Create lumen and hardware rows for the days before the monitor; returns (lumen, hardware, fitted)."""
    tz = config.timezone
    today = local_day(utcnow(), tz)
    with lumen.connect() as conn:
        window = {
            "start": utc(local_midnight(only_day, tz)) if only_day else None,
            "end": utc(local_midnight(only_day + timedelta(days=1) if only_day else today, tz)),
        }
        days = conn.execute(HISTORY_SQL, {"tz": tz.key, **window}).all()
        models = {m.model_name: m for m in conn.execute(MODELS_SQL).all()}

    # Every monitor row, loaded once; rows created below are added so a key is never added twice.
    existing = {(r.model, r.day, r.source): r for r in session.execute(select(DailyStat)).scalars()}
    # See backfill_days: only models the live collector counts a metric for get its watermark.
    watermarked = watermarked_models(session)

    created = hw_created = fitted = 0
    skipped = set()          # models with traffic but no model_configs row
    no_hardware = set()      # models with no GPU entry in the monitor config
    for d in days:
        model = models.get(d.model_name)
        if model is None:
            skipped.add(d.model_name)
            continue
        day_end = local_midnight(d.day + timedelta(days=1), tz)
        prices = fit_prices(d.ii, d.io, d.oo, d.ci, d.co)
        fitted += prices is not None
        price_in, price_out = prices or (float(model.input_cost_per_million), float(model.output_cost_per_million))

        existing_lumen = existing.get((d.model_name, d.day, "lumen"))
        tokens = split(d.input_tokens, d.output_tokens)
        if existing_lumen is None:
            existing[d.model_name, d.day, "lumen"] = row = DailyStat(
                day=d.day, model=d.model_name, source="lumen",
                input_price=price_in, output_price=price_out,
                requests=d.requests, **tokens,
                covered_until=day_end,
                **{column: day_end if d.model_name in watermarked[metric] else None for metric, column in TOKEN_METRICS.items()},
                estimated=False, final=True, configured=model.endpoints,
            )
            session.add(row)
            created += 1
        elif existing_lumen.estimated or not existing_lumen.final:
            # Replace an estimated/partial row with the exact, final backfilled values.
            existing_lumen.input_price = price_in
            existing_lumen.output_price = price_out
            existing_lumen.requests = d.requests
            existing_lumen.covered_until = day_end
            for metric, column in TOKEN_METRICS.items():
                setattr(existing_lumen, metric, tokens[metric])
                if d.model_name in watermarked[metric]:
                    setattr(existing_lumen, column, day_end)
            existing_lumen.estimated = False
            existing_lumen.final = True
            existing_lumen.configured = model.endpoints
            existing_lumen.last_updated = utcnow()
            created += 1

        hw = config.hardware_for(d.model_name)
        if hw is None:
            no_hardware.add(d.model_name)
        elif config.kwh is not None and (d.model_name, d.day, "hardware") not in existing:
            gpu, gpus = hw
            entry = config.model_entry(d.model_name)
            existing[d.model_name, d.day, "hardware"] = row = DailyStat(
                day=d.day, model=d.model_name, source="hardware",
                daily_cost=hardware.daily_cost(gpu, gpus, config.kwh, model.endpoints),
                gpu=entry.gpu, gpus=gpus, gpu_price=gpu.price, energy=gpu.energy, kwh=config.kwh,
                avg_healthy=float(model.endpoints),
            )
            session.add(row)
            hw_created += 1
    if skipped:
        print("skipped (no model_configs row): " + ", ".join(sorted(skipped)))
    if no_hardware:
        print("no hardware cost (not in monitor config): " + ", ".join(sorted(no_hardware)))
    return created, hw_created, fitted


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config.yaml", help="monitor config (default: config.yaml)")
    parser.add_argument("--lumen-db", default=os.environ.get("LUMEN_DATABASE_URL"), help="Lumen PostgreSQL URL (env LUMEN_DATABASE_URL)")
    parser.add_argument("--day", type=date.fromisoformat, help="only backfill this local day (YYYY-MM-DD, before today)")
    parser.add_argument("--dry-run", action="store_true", help="compute everything, then roll back")
    args = parser.parse_args()
    if not args.lumen_db:
        parser.error("--lumen-db or LUMEN_DATABASE_URL is required")

    config = load_config(args.config)
    if args.day and args.day >= local_day(utcnow(), config.timezone):
        parser.error("--day must be before today")
    lumen = create_engine(args.lumen_db)
    with make_session_factory(make_engine(config.database_url))() as session:
        samples = 0 if args.day else backfill_samples(lumen, session)
        days = backfill_days(lumen, session, config.timezone, args.day)
        history, hw, fitted = backfill_history(lumen, session, config, args.day)
        if args.dry_run:
            session.rollback()
        else:
            session.commit()
    print(f"{'would update' if args.dry_run else 'updated'} {samples} samples and {days} daily rows")
    print(f"{'would create' if args.dry_run else 'created'} {history} history lumen rows "
          f"({fitted} with prices fitted from billed cost, the rest at today's price) and {hw} hardware rows")


if __name__ == "__main__":
    main()
