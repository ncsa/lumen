import os
import re
import shutil
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import click
import yaml
from flask import current_app
from flask.cli import with_appcontext
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError

from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint

from .extensions import db

# The entity-dimensioned continuous aggregate the per-entity /usage queries read.
# `enable-retention` refuses while this is empty: dropping raw chunks then destroys
# history that exists in no aggregate.
ENTITY_AGGREGATE = "request_counts_hourly_by_entity"

# Deduplicate deprecation warnings; the config watcher re-runs sync every 5s.
_warned: set = set()


def _warn_once(key, msg, *args):
    if key not in _warned:
        _warned.add(key)
        current_app.logger.warning(msg, *args)


def write_config_yaml(config_path, data):
    """Write ``data`` (a top-level config dict) to config_path as YAML.

    Each top-level key is dumped as its own document fragment (preserving key order
    and keeping blocks visually separated), the previous file is backed up to
    ``<config>.bak``, and the new content is written atomically via a temp file.
    Shared by the admin config editor and project creation.

    If you add a new secret-bearing key to the schema, add it to
    :data:`SENSITIVE_KEYS` in :mod:`lumen.services.config_watcher` so it is
    masked before being sent to the admin browser (see ``config_api_get``).
    """
    parts = [
        yaml.dump({k: v}, Dumper=yaml.SafeDumper, default_flow_style=False, allow_unicode=True, sort_keys=False)
        for k, v in data.items()
    ]
    fd, tmp_path = tempfile.mkstemp(suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(parts))
        # Back up the current config before overwriting so a partial or
        # malformed save can be recovered from <config>.bak. Best-effort:
        # in containers only config.yaml itself is bind-mounted, so the
        # directory may not be writable — a failed backup must not block
        # the save.
        if os.path.exists(config_path):
            try:
                shutil.copy2(config_path, config_path + ".bak")
            except OSError as e:
                current_app.logger.warning(
                    "write_config_yaml: could not write backup %s.bak: %s", config_path, e
                )
        shutil.copyfile(tmp_path, config_path)
    finally:
        os.unlink(tmp_path)


def _normalize_model_url(url):
    """Expand a bare HuggingFace repo id (`org/name`) to a full huggingface.co URL.

    Common HuggingFace host variants (huggingface.com, www.) are rewritten to
    huggingface.co so the README lookup recognizes them; any other URL is used
    as given."""
    if not url:
        return None
    if "://" not in url and re.fullmatch(r"[\w.-]+/[\w.-]+", url):
        return f"https://huggingface.co/{url}"
    parsed = urlparse(url)
    if parsed.netloc.lower() in ("huggingface.com", "www.huggingface.co", "www.huggingface.com"):
        url = parsed._replace(netloc="huggingface.co").geturl()
    return url


def _normalize_end_date(value, model_name=None):
    """Coerce a config end_date (date, datetime, or ISO string) to naive-UTC datetime.

    A bare date is stored as midnight UTC at the start of the following day, so
    the model remains usable throughout the named date. Explicit datetimes stay
    exact. Unparseable values are dropped with a warning."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day) + timedelta(days=1)
    text_value = str(value)
    bare_iso_date = re.fullmatch(r"\d{4}-\d{2}-\d{2}", text_value) is not None
    parsed_rfc822_date = False
    try:
        parsed = datetime.fromisoformat(text_value)
    except ValueError:
        # The admin config editor round-trips YAML dates through JSON, which
        # Flask serializes in RFC 822 form ("Thu, 31 Dec 2026 00:00:00 GMT").
        try:
            parsed = parsedate_to_datetime(text_value)
            parsed_rfc822_date = parsed.hour == parsed.minute == parsed.second == parsed.microsecond == 0
        except ValueError:
            _warn_once(("end-date", model_name), "invalid end_date '%s' on model '%s'; ignoring", value, model_name)
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    if bare_iso_date or parsed_rfc822_date:
        parsed += timedelta(days=1)
    return parsed


def _normalize_knowledge_cutoff(value, model_name=None):
    """Clamp a knowledge cutoff to YYYY-MM (the column is String(7)).

    models.dev sometimes reports YYYY-MM-DD; keep just the year-month. Other
    values longer than 7 characters are dropped with a warning."""
    if not value:
        return None
    value = str(value)
    if re.match(r"^\d{4}-\d{2}", value):
        return value[:7]
    if len(value) > 7:
        _warn_once(("knowledge-cutoff", model_name),
                   "invalid knowledge_cutoff '%s' on model '%s'; expected YYYY-MM, ignoring", value, model_name)
        return None
    return value


def _apply_model_fields(config, model_def):
    config.input_cost_per_million = model_def["input_cost_per_million"]
    config.output_cost_per_million = model_def["output_cost_per_million"]
    if "audio_cost_per_hour" in model_def:
        config.audio_cost_per_hour = model_def["audio_cost_per_hour"]
    elif "audio_cost_per_minute" in model_def:
        # Legacy per-minute pricing -> per-hour (×60).
        legacy = model_def.get("audio_cost_per_minute")
        _warn_once(("audio-cost", model_def.get("name")),
                   "deprecated 'audio_cost_per_minute' on model '%s'; use 'audio_cost_per_hour' instead", model_def.get("name"))
        config.audio_cost_per_hour = (legacy * 60) if legacy is not None else None
    else:
        config.audio_cost_per_hour = None
    _apply_model_access(config, model_def)
    config.description = model_def.get("description") or None
    config.url = _normalize_model_url(model_def.get("url"))
    config.supports_function_calling = model_def.get("supports_function_calling")
    config.input_modalities = model_def.get("input_modalities") or None
    config.output_modalities = model_def.get("output_modalities") or None
    config.context_window = model_def.get("context_window") or None
    config.max_output_tokens = model_def.get("max_output_tokens") or None
    config.supports_reasoning = model_def.get("supports_reasoning")
    config.knowledge_cutoff = _normalize_knowledge_cutoff(model_def.get("knowledge_cutoff"), model_def.get("name"))
    config.notice = model_def.get("notice") or None
    config.ack_message = model_def.get("ack_message") or None
    config.end_date = _normalize_end_date(model_def.get("end_date"), model_def.get("name"))


def _apply_model_access(config, model_def):
    """Set config.needs_ack / early_access / disabled from a model definition.

    Bridges the legacy `active:` boolean (active: false -> disabled) with a
    deprecation warning. Never touches owner_entity_id or group grants — model
    ownership is DB-managed via the admin UI, not config.yaml.
    """
    config.needs_ack = bool(model_def.get("needs_ack", False))
    config.early_access = bool(model_def.get("early_access", False))

    disabled = model_def.get("disabled")
    if disabled is None and model_def.get("active") is False:
        _warn_once(("active", model_def.get("name")),
                   "deprecated 'active: false' on model '%s'; use 'disabled: true' instead", model_def.get("name"))
        disabled = True
    config.disabled = bool(disabled)


def _reconcile_endpoints(config, model_def):
    yaml_urls = {ep_def["url"] for ep_def in model_def.get("endpoints", [])}
    # Build dict once to avoid O(n²) iteration over the endpoints collection
    existing_by_url = {ep.url: ep for ep in config.endpoints}

    for ep in list(existing_by_url.values()):
        if ep.url not in yaml_urls:
            db.session.delete(ep)

    for ep_def in model_def.get("endpoints", []):
        if ep_def["url"] not in existing_by_url:
            db.session.add(ModelEndpoint(
                model_config_id=config.id,
                url=ep_def["url"],
                api_key=ep_def["api_key"],
                model_name=ep_def.get("model") or None,
                healthy=False,
            ))
        else:
            existing_ep = existing_by_url[ep_def["url"]]
            existing_ep.api_key = ep_def["api_key"]
            existing_ep.model_name = ep_def.get("model") or None


def _deactivate_removed_models(yaml_model_names):
    stmt = select(ModelConfig)
    if yaml_model_names:
        stmt = stmt.where(ModelConfig.model_name.notin_(yaml_model_names))
    deactivated_ids = [c.id for c in db.session.execute(stmt).scalars().all()]
    if not deactivated_ids:
        return
    db.session.execute(
        delete(ModelEndpoint).where(ModelEndpoint.model_config_id.in_(deactivated_ids))
    )
    db.session.execute(
        update(ModelConfig).where(ModelConfig.id.in_(deactivated_ids)).values(disabled=True)
    )
    db.session.expire_all()


def sync_models_from_yaml(yaml_data):
    """Upsert ModelConfig and ModelEndpoint rows from yaml_data. Must run inside an app context."""
    for model_def in yaml_data.get("models", []):
        config = db.session.execute(select(ModelConfig).filter_by(model_name=model_def["name"])).scalar_one_or_none()
        if not config:
            config = ModelConfig(model_name=model_def["name"])
            db.session.add(config)
        _apply_model_fields(config, model_def)
        db.session.flush()  # ensure config.id exists before reconciling endpoints
        _reconcile_endpoints(config, model_def)

    _deactivate_removed_models({m["name"] for m in yaml_data.get("models", [])})
    db.session.commit()


@click.command("init-db")
@with_appcontext
def init_db_cmd():
    """Sync ModelConfig and ModelEndpoint from config.yaml."""
    yaml_data = current_app.config["YAML_DATA"]
    sync_models_from_yaml(yaml_data)
    click.echo("Database synced from config.yaml.")


@click.command("reassign-model")
@click.argument("from_id", type=int)
@click.argument("to_id", type=int)
@with_appcontext
def reassign_model_cmd(from_id, to_id):
    """Move all conversations and stats from one model to another.

    FROM_ID and TO_ID are model_configs.id values.
    """
    from lumen.models.conversation import Conversation
    from lumen.models.model_stat import ModelStat
    from lumen.models.request_log import RequestLog

    src = db.session.get(ModelConfig, from_id)
    dst = db.session.get(ModelConfig, to_id)
    if not src:
        click.echo(f"Error: model_config id {from_id} not found.")
        raise SystemExit(1)
    if not dst:
        click.echo(f"Error: model_config id {to_id} not found.")
        raise SystemExit(1)

    click.echo(f"Reassigning from '{src.model_name}' (id={from_id}) to '{dst.model_name}' (id={to_id})")

    result = db.session.execute(update(Conversation).where(Conversation.model == src.model_name).values(model=dst.model_name))
    conv_count = result.rowcount
    click.echo(f"  conversations updated: {conv_count}")

    # For model_stats, merge rows that might collide on the unique constraint
    existing_dst_stats = {
        (s.entity_id, s.source): s
        for s in db.session.execute(select(ModelStat).filter_by(model_config_id=to_id)).scalars().all()
    }
    src_stats = db.session.execute(select(ModelStat).filter_by(model_config_id=from_id)).scalars().all()
    stats_merged = 0
    stats_moved = 0
    for stat in src_stats:
        key = (stat.entity_id, stat.source)
        if key in existing_dst_stats:
            dst_stat = existing_dst_stats[key]
            dst_stat.requests += stat.requests
            dst_stat.input_tokens += stat.input_tokens
            dst_stat.output_tokens += stat.output_tokens
            dst_stat.cost += stat.cost
            if stat.last_used_at and (not dst_stat.last_used_at or stat.last_used_at > dst_stat.last_used_at):
                dst_stat.last_used_at = stat.last_used_at
            db.session.delete(stat)
            stats_merged += 1
        else:
            stat.model_config_id = to_id
            stats_moved += 1
    click.echo(f"  model_stats moved: {stats_moved}, merged: {stats_merged}")

    result = db.session.execute(update(RequestLog).where(RequestLog.model_config_id == from_id).values(model_config_id=to_id))
    log_count = result.rowcount
    click.echo(f"  request_logs updated: {log_count}")

    db.session.commit()
    click.echo("Done.")


# --------------------------------------------------------------------------- #
# TimescaleDB lifecycle (Phase 8): aggregate backfill and retention.           #
# --------------------------------------------------------------------------- #

def _autocommit_connection():
    """Open a connection that is *not* inside a transaction block.

    ``CALL refresh_continuous_aggregate`` cannot run inside a transaction, and a
    ``flask`` command using ``db.session`` is already in one — the CALL then fails
    with "cannot run inside a transaction block" in the middle of an operator's
    maintenance window. Same technique as ``seed_analytics.py``.
    """
    return db.engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def _continuous_aggregates(conn):
    """Names of every continuous aggregate in this database, alphabetically."""
    return conn.execute(text(
        "SELECT view_name FROM timescaledb_information.continuous_aggregates ORDER BY view_name"
    )).scalars().all()


def _bucket_column(conn, view_name):
    """Name of the aggregate's ``time_bucket`` column, which is always its first."""
    return conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :v AND ordinal_position = 1"
    ), {"v": view_name}).scalar()


def _materialization_hypertable(conn, view_name):
    """The physical table holding an aggregate's *materialised* rows.

    Not the view. With ``materialized_only = false`` the view unions materialised rows
    with a live scan of the raw rows above the watermark, so ``MIN(bucket)`` read through
    the view reports how far back *raw* goes for as long as the watermark is still at
    -infinity — which is exactly the freshly-migrated state where no backfill has run.
    Only the materialisation hypertable answers what would still be there once retention
    has dropped the raw chunks.
    """
    return conn.execute(text(
        "SELECT materialization_hypertable_schema || '.' || materialization_hypertable_name "
        "FROM timescaledb_information.continuous_aggregates WHERE view_name = :v"
    ), {"v": view_name}).scalar()


def _retention_drop_after(conn):
    """The retention policy's interval on request_logs, or None if retention is off."""
    return conn.execute(text(
        "SELECT config->>'drop_after' FROM timescaledb_information.jobs "
        "WHERE proc_name = 'policy_retention' AND hypertable_name = 'request_logs'"
    )).scalar()


def _fmt(dt):
    """Format a timestamp for the terminal. Always UTC — this is an operator tool."""
    if dt is None:
        return "none"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _next_month(dt):
    year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return dt.replace(year=year, month=month, day=1)


# TimescaleDB refuses an *overlapping* refresh outright instead of waiting for it: while
# a background continuous-aggregate policy job is mid-refresh, the CALL raises SQLSTATE
# 55P03 (lock_not_available) immediately -- measured at 0.02s on 2.27.2. request_metrics_1m
# has a policy firing every minute and the entity aggregate one every hour, so a 13-month
# month-by-month backfill will collide at least once. Without a retry that aborts the whole
# command partway and leaves the operator with a half-backfilled aggregate.
_LOCK_NOT_AVAILABLE = "55P03"
_REFRESH_RETRY_BUDGET = 60.0
_REFRESH_RETRY_SLEEP = 2.0


def _refresh_window(conn, name, start, end):
    """Refresh one window, waiting out a concurrent refresh. Returns the retry count.

    Matches the SQLSTATE rather than the message text: the message is not part of any
    stability guarantee, and matching it would turn a version bump into a silent loss
    of the retry.
    """
    deadline = time.monotonic() + _REFRESH_RETRY_BUDGET
    retries = 0
    while True:
        try:
            conn.execute(text("CALL refresh_continuous_aggregate(:n, :s, :e)"),
                         {"n": name, "s": start, "e": end})
            return retries
        except DBAPIError as exc:
            if getattr(exc.orig, "pgcode", None) != _LOCK_NOT_AVAILABLE:
                raise
            if time.monotonic() + _REFRESH_RETRY_SLEEP > deadline:
                raise
            retries += 1
            time.sleep(_REFRESH_RETRY_SLEEP)


@click.command("backfill-aggregate")
@click.option("--name", default=ENTITY_AGGREGATE, show_default=True,
              help="Continuous aggregate to materialise.")
@click.option("--from", "from_month", metavar="YYYY-MM", default=None,
              help="First month to refresh. Defaults to the month of the oldest request_logs row.")
@click.option("--force", is_flag=True,
              help="Refresh months starting before the retention boundary anyway. Read the refusal first.")
@with_appcontext
def backfill_aggregate_cmd(name, from_month, force):
    """Materialise a continuous aggregate's full history, month by month.

    A policy-created aggregate holds only its ``start_offset`` window, so per-user
    "All Time" charts stay near-empty until this is run. One
    ``CALL refresh_continuous_aggregate(NULL, NULL)`` over 13 months of a production
    hypertable is a single long transaction with unbounded memory, so this walks
    month by month, oldest first, and prints the row counts either side of each call.
    """
    if db.engine.dialect.name != "postgresql":
        click.echo(f"backfill-aggregate requires PostgreSQL/TimescaleDB; nothing to do on "
                   f"{db.engine.dialect.name}.")
        return

    with _autocommit_connection() as conn:
        if name not in _continuous_aggregates(conn):
            click.echo(f"Error: '{name}' is not a continuous aggregate in this database.")
            raise SystemExit(1)
        bucket = _bucket_column(conn, name)

        if from_month:
            try:
                start = datetime.strptime(from_month, "%Y-%m").replace(tzinfo=timezone.utc)
            except ValueError:
                click.echo(f"Error: --from must be YYYY-MM, got '{from_month}'.")
                raise SystemExit(1)
        else:
            start = conn.execute(text(
                "SELECT date_trunc('month', MIN(time) AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' "
                "FROM request_logs"
            )).scalar()
            if start is None:
                click.echo("request_logs is empty; nothing to backfill.")
                return

        # Contract (d): refreshing a window whose raw chunks were dropped recomputes it
        # as EMPTY and DELETES the materialised rows, with no error at all. A backfill
        # run a year after retention was enabled would erase every user's pre-retention
        # history in one call, which is strictly worse than the truncation the whole
        # phase exists to avoid — so refuse rather than trust the operator's arithmetic.
        drop_after = _retention_drop_after(conn)
        if drop_after is not None:
            boundary = conn.execute(text("SELECT now() - CAST(:d AS interval)"),
                                    {"d": drop_after}).scalar().astimezone(timezone.utc)
            if start < boundary:
                erased = conn.execute(text(
                    f'SELECT COUNT(*) FROM "{name}" WHERE "{bucket}" >= :s AND "{bucket}" < :b'
                ), {"s": start, "b": boundary}).scalar()
                if not force:
                    # The boundary's own month still starts before the boundary, so the
                    # hint has to name the month after it or the re-run is refused too.
                    safe = _next_month(boundary.replace(day=1, hour=0, minute=0,
                                                        second=0, microsecond=0))
                    click.echo(
                        f"Error: refusing to refresh '{name}' from {start:%Y-%m}.\n"
                        f"  retention on request_logs is '{drop_after}', so raw chunks before\n"
                        f"  {_fmt(boundary)} may already have been dropped, and every one that\n"
                        f"  has not will be. Refreshing that window recomputes it as\n"
                        f"  EMPTY and DELETES the materialised rows, with no error: {erased} rows\n"
                        f"  of '{name}' would be erased.\n"
                        f"  Re-run with --from {safe:%Y-%m} to stay inside retention, or --force."
                    )
                    raise SystemExit(1)
                click.echo(
                    f"WARNING: --force given; months before {_fmt(boundary)} are outside retention "
                    f"and their {erased} materialised rows will be erased."
                )

        now = datetime.now(timezone.utc)
        click.echo(f"Backfilling '{name}' from {start:%Y-%m} to {now:%Y-%m}, month by month.")
        refreshed = []
        month = start
        while month <= now:
            nxt = _next_month(month)
            # Never refresh past "now". A window ending in the future materialises the
            # current bucket and moves the aggregate's watermark beyond it, which turns
            # real-time aggregation OFF for that bucket: every request logged after the
            # backfill stays invisible on /usage until a scheduled refresh catches up.
            # Measured on 2.27.2 — with the month end (future) the next insert was
            # missing; with now() the watermark stayed two buckets back and it showed.
            end = min(nxt, now)
            raw = conn.execute(text(
                "SELECT COUNT(*) FROM request_logs WHERE time >= :s AND time < :e"
            ), {"s": month, "e": nxt}).scalar()
            click.echo(f"  {month:%Y-%m}: {raw} request_logs rows ... ", nl=False)
            try:
                retries = _refresh_window(conn, name, month, end)
            except DBAPIError as exc:
                click.echo("FAILED")
                click.echo(f"Error: refreshing {month:%Y-%m} failed: {exc.orig}")
                if refreshed:
                    click.echo(f"  Refreshed OK: {refreshed[0]} .. {refreshed[-1]} "
                               f"({len(refreshed)} months).")
                else:
                    click.echo("  No month was refreshed.")
                click.echo(f"  Resume with: flask backfill-aggregate "
                           f"--name {name} --from {month:%Y-%m}")
                raise SystemExit(1)
            aggregated = conn.execute(text(
                f'SELECT COUNT(*) FROM "{name}" WHERE "{bucket}" >= :s AND "{bucket}" < :e'
            ), {"s": month, "e": nxt}).scalar()
            click.echo(f"{aggregated} aggregate rows OK"
                       + (f" (after {retries} lock retries)" if retries else ""))
            refreshed.append(f"{month:%Y-%m}")
            month = nxt

    if refreshed:
        click.echo(f"Backfill complete: {len(refreshed)} months refreshed "
                   f"({refreshed[0]} .. {refreshed[-1]}).")
    else:
        click.echo("Backfill complete: no months to refresh.")


@click.command("enable-retention")
@click.option("--window", default="13 months", show_default=True,
              help="Drop request_logs chunks older than this interval.")
@click.option("--dry-run/--force", "dry_run", default=True,
              help="Dry run (the default) only reports; --force adds the retention policy.")
@with_appcontext
def enable_retention_cmd(window, dry_run):
    """Report — and only with --force, enable — the request_logs retention policy.

    Deliberately an operator command rather than a migration: ``entrypoint.sh`` runs
    ``flask db upgrade`` at container start, so a migration calling
    ``add_retention_policy`` would begin deleting data on the next deploy and make the
    "dry run first" gate unenforceable.
    """
    if db.engine.dialect.name != "postgresql":
        click.echo(f"enable-retention requires PostgreSQL/TimescaleDB; nothing to do on "
                   f"{db.engine.dialect.name}.")
        return

    with _autocommit_connection() as conn:
        existing = _retention_drop_after(conn)
        if existing is not None:
            click.echo(f"request_logs already has a retention policy (drop_after = {existing}); "
                       f"no change made. The requested --window '{window}' was NOT applied — "
                       f"this command never edits an existing policy. To change the window run "
                       f"SELECT remove_retention_policy('request_logs') and then re-run.")
            return

        try:
            conn.execute(text("SELECT CAST(:w AS interval)"), {"w": window}).scalar()
        except DBAPIError:
            click.echo(f"Error: --window '{window}' is not a valid PostgreSQL interval.")
            raise SystemExit(1)

        click.echo(f"Retention window: {window}")
        dropped, oldest, newest = conn.execute(text(
            "SELECT COUNT(*), MIN(time), MAX(time) FROM request_logs "
            "WHERE time < now() - CAST(:w AS interval)"
        ), {"w": window}).one()
        click.echo(f"request_logs rows that would eventually be dropped: {dropped}"
                   + (f" ({_fmt(oldest)} .. {_fmt(newest)})" if dropped else ""))

        aggregates = _continuous_aggregates(conn)
        for view in aggregates:
            earliest = conn.execute(text(
                f'SELECT MIN("{_bucket_column(conn, view)}") FROM "{view}"'
            )).scalar()
            start_offset = conn.execute(text(
                "SELECT config->>'start_offset' FROM timescaledb_information.jobs "
                "WHERE proc_name = 'policy_refresh_continuous_aggregate' AND hypertable_name = :v"
            ), {"v": view}).scalar()
            click.echo(f"  {view}: earliest bucket {_fmt(earliest)}, "
                       f"refresh start_offset {start_offset or 'none'}")
            if earliest is not None and start_offset is not None:
                floor_, wider = conn.execute(text(
                    "SELECT now() - CAST(:o AS interval), "
                    "CAST(:o AS interval) > CAST(:w AS interval)"
                ), {"o": start_offset, "w": window}).one()
                click.echo("      earliest bucket is "
                           + ("outside" if earliest < floor_ else "inside")
                           + f" the refresh window (start_offset floor {_fmt(floor_)})")
                if wider:
                    # Contract (d): a scheduled refresh reaching past the retention
                    # boundary recomputes those buckets as empty and deletes them.
                    click.echo(f"      WARNING: start_offset ({start_offset}) is wider than the "
                               f"retention window ({window}); the refresh policy would reach into "
                               f"dropped chunks and silently erase materialised rows.")

        if ENTITY_AGGREGATE not in aggregates:
            click.echo(f"Error: '{ENTITY_AGGREGATE}' does not exist. Retention would destroy "
                       f"per-entity history that is in no aggregate. Run 'flask db upgrade' first.")
            raise SystemExit(1)
        # Retention deletes raw chunks, so everything it deletes has to be materialised
        # already. "Not empty" cannot answer that: the aggregate is created WITH NO DATA
        # but carries materialized_only = false and an hourly refresh policy with a
        # 30-day start_offset, so ordinary traffic makes it non-empty within an hour of
        # deploy — a COUNT(*) guard passes forever after while no historical backfill has
        # ever run. The question that decides whether history survives is the one
        # _entity_aggregate_covers asks of this same aggregate before the per-entity
        # charts are allowed to read it: is its earliest materialised bucket at or before
        # the oldest raw row that still exists? If it is, everything retention can drop is
        # held twice. If it is not, the gap between those two timestamps lives only in the
        # chunks retention is about to delete.
        earliest = conn.execute(text(
            f'SELECT MIN("{_bucket_column(conn, ENTITY_AGGREGATE)}") '
            f'FROM {_materialization_hypertable(conn, ENTITY_AGGREGATE)}'
        )).scalar()
        raw_oldest = conn.execute(text("SELECT MIN(time) FROM request_logs")).scalar()
        if earliest is None:
            click.echo(f"Error: '{ENTITY_AGGREGATE}' is empty — the backfill has not been run. "
                       f"Enabling retention now would destroy per-entity history that is in no "
                       f"aggregate. Run 'flask backfill-aggregate' first.")
            raise SystemExit(1)
        if raw_oldest is not None and earliest > raw_oldest:
            hint = raw_oldest.astimezone(timezone.utc)
            click.echo(
                f"Error: '{ENTITY_AGGREGATE}' does not cover the history retention would drop.\n"
                f"  earliest materialised bucket:      {_fmt(earliest)}\n"
                f"  oldest surviving request_logs row: {_fmt(raw_oldest)}\n"
                f"  Everything between those two timestamps is held only in raw chunks, and\n"
                f"  retention deletes them. Run 'flask backfill-aggregate --from {hint:%Y-%m}'\n"
                f"  first, then re-run this command."
            )
            raise SystemExit(1)
        click.echo(f"{ENTITY_AGGREGATE} covers request_logs back to {_fmt(earliest)} "
                   f"(oldest raw row {_fmt(raw_oldest)}).")

        if dry_run:
            click.echo("Dry run: no policy added. Re-run with --force to enable retention.")
            return

        job_id = conn.execute(text(
            "SELECT add_retention_policy('request_logs', drop_after => CAST(:w AS interval))"
        ), {"w": window}).scalar()
        click.echo(f"Retention enabled on request_logs (drop_after = {window}, job {job_id}).")
