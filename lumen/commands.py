import math
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
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from flask import current_app
from flask.cli import with_appcontext
from flask_migrate import upgrade as migrate_upgrade
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from lumen.models.model_alias import ModelAlias
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint

from .extensions import db

# The entity-dimensioned continuous aggregate the per-entity /usage queries read.
# `check-aggregate` reports whether it covers every request_logs row. request_logs
# itself is never pruned (#167), so this is about chart speed, not about saving data.
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

    models.dev sometimes reports YYYY-MM-DD; keep just the year-month. Values
    that are not YYYY-MM or YYYY-MM-DD are dropped with a warning."""
    if not value:
        return None
    value = str(value)
    if re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])(?:-\d{2})?", value):
        return value[:7]
    _warn_once(("knowledge-cutoff", model_name),
               "invalid knowledge_cutoff '%s' on model '%s'; expected YYYY-MM, ignoring", value, model_name)
    return None


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
    deprecation warning. Never touches owner_entity_id — model
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
    # Keep every row, including duplicate URLs. Prefer a currently active row
    # before reusing a retired one, preserving IDs and historical references.
    existing_by_url = {}
    for ep in sorted(config.endpoints, key=lambda ep: (not ep.active, ep.id)):
        existing_by_url.setdefault(ep.url, []).append(ep)

    for ep_def in model_def.get("endpoints", []):
        candidates = existing_by_url.get(ep_def["url"], [])
        if not candidates:
            db.session.add(ModelEndpoint(
                model_config_id=config.id,
                url=ep_def["url"],
                api_key=ep_def["api_key"],
                model_name=ep_def.get("model") or None,
                active=True,
                healthy=False,
            ))
        else:
            existing_ep = candidates.pop(0)
            if not existing_ep.active:
                existing_ep.healthy = False  # require a fresh probe on reactivation
            existing_ep.active = True
            existing_ep.api_key = ep_def["api_key"]
            existing_ep.model_name = ep_def.get("model") or None

    for remaining in existing_by_url.values():
        for ep in remaining:
            ep.active = False
            ep.healthy = False  # retired rows are never probed again; drop stale health


def _deactivate_removed_models(yaml_model_names):
    stmt = select(ModelConfig)
    if yaml_model_names:
        stmt = stmt.where(ModelConfig.model_name.notin_(yaml_model_names))
    deactivated_ids = [c.id for c in db.session.execute(stmt).scalars().all()]
    if not deactivated_ids:
        return
    db.session.execute(
        update(ModelEndpoint).where(ModelEndpoint.model_config_id.in_(deactivated_ids)).values(active=False, healthy=False)
    )
    db.session.execute(
        update(ModelConfig).where(ModelConfig.id.in_(deactivated_ids)).values(disabled=True)
    )
    db.session.expire_all()


# Maximum length of an alias/ID (must match the model_aliases.alias column).
_ALIAS_MAX_LEN = 128
# POST /v1/models/<id>/acknowledge would be ambiguous for an id ending in this.
RESERVED_MODEL_ID_SUFFIX = "/acknowledge"


def model_alias_config_errors(models) -> list[str]:
    """Validate the ``aliases`` of every model definition.

    Names are exact and case-sensitive. Neither a model name nor an alias may
    end in ``/acknowledge``: ``POST /v1/models/<id>/acknowledge`` would be
    ambiguous for it. Returns human-readable error strings
    (empty when valid). The canonical names are collected from the whole list
    first so an alias can never collide with a model name that appears later.

    Shared by the startup watcher, the CLI sync, and the admin config editor so
    an invalid alias configuration is rejected everywhere before any writes.
    """
    errors: list[str] = []
    canonical_names = {
        m.get("name") for m in models
        if isinstance(m, dict) and isinstance(m.get("name"), str) and m.get("name")
    }
    seen_aliases: set[str] = set()
    for i, model in enumerate(models):
        if not isinstance(model, dict):
            continue
        name = model.get("name")
        label = name if isinstance(name, str) and name else f"models[{i}]"
        if isinstance(name, str) and name.endswith(RESERVED_MODEL_ID_SUFFIX):
            errors.append(f"{label}: model name cannot end in '{RESERVED_MODEL_ID_SUFFIX}' (reserved for the acknowledge endpoint)")
        aliases = model.get("aliases")
        if aliases is None:
            continue
        if not isinstance(aliases, list):
            errors.append(f"{label}: 'aliases' must be a list")
            continue
        for j, alias in enumerate(aliases):
            if not isinstance(alias, str) or not alias.strip():
                errors.append(f"{label}: aliases[{j}] must be a non-empty string")
                continue
            if len(alias) > _ALIAS_MAX_LEN:
                errors.append(f"{label}: alias '{alias}' exceeds {_ALIAS_MAX_LEN} characters")
                continue
            if alias.endswith(RESERVED_MODEL_ID_SUFFIX):
                errors.append(f"{label}: alias '{alias}' cannot end in '{RESERVED_MODEL_ID_SUFFIX}' (reserved for the acknowledge endpoint)")
                continue
            if alias == name:
                errors.append(f"{label}: alias '{alias}' cannot equal the model's own name")
                continue
            if alias in canonical_names:
                errors.append(f"{label}: alias '{alias}' collides with model name '{alias}'")
                continue
            if alias in seen_aliases:
                errors.append(f"{label}: duplicate alias '{alias}'")
            seen_aliases.add(alias)
    return errors


def validate_model_aliases(data):
    """Raise ``ValueError`` when the config's model aliases are invalid.

    Central validation for the CLI sync and the config watcher, both of which
    call :func:`sync_models_from_yaml` and must reject a bad config before any
    writes. See :func:`model_alias_config_errors`.
    """
    models = data.get("models", []) if isinstance(data, dict) else []
    errors = model_alias_config_errors(models)
    if errors:
        raise ValueError("; ".join(errors))


def _reconcile_aliases(config, model_def):
    """Upsert the model_aliases rows for ``config`` from ``model_def['aliases']``.

    Removes aliases no longer declared, adds newly declared ones, and retargets
    an alias that moved from another model (the alias is unique, so a move must
    update the existing row rather than insert a duplicate).
    """
    desired = set(model_def.get("aliases", []) or [])
    existing = {a.alias: a for a in config.aliases}
    for alias in list(existing):
        if alias not in desired:
            db.session.delete(existing[alias])
    for alias in desired:
        row = db.session.execute(select(ModelAlias).filter_by(alias=alias)).scalar_one_or_none()
        if row is None:
            db.session.add(ModelAlias(alias=alias, model_config_id=config.id))
        elif row.model_config_id != config.id:
            row.model_config_id = config.id


def sync_models_from_yaml(yaml_data):
    """Upsert ModelConfig and ModelEndpoint rows from yaml_data. Must run inside an app context."""
    validate_model_aliases(yaml_data)
    for model_def in yaml_data.get("models", []):
        config = db.session.execute(select(ModelConfig).filter_by(model_name=model_def["name"])).scalar_one_or_none()
        if not config:
            config = ModelConfig(model_name=model_def["name"])
            db.session.add(config)
        _apply_model_fields(config, model_def)
        db.session.flush()  # ensure config.id exists before reconciling endpoints
        _reconcile_endpoints(config, model_def)
        _reconcile_aliases(config, model_def)

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
    """Move all conversations, stats and request history from one model to another.

    FROM_ID and TO_ID are model_configs.id values. The FROM row in model_configs is
    left as it is.

    Not atomic on PostgreSQL: request_logs moves one chunk per transaction, so a
    failure partway leaves its history split between FROM and TO. It is safe to
    re-run: the same command picks up where the last one stopped. Run it off-hours
    for a model with a large history, and retire FROM in config.yaml first so no new
    requests are logged against it while it runs.
    """
    from lumen.models.conversation import Conversation
    from lumen.models.model_stat import ModelStat
    from lumen.models.request_log import RequestLog

    if from_id == to_id:
        click.echo(f"Error: FROM and TO are the same model_config id ({from_id}).")
        raise SystemExit(1)
    src = db.session.get(ModelConfig, from_id)
    dst = db.session.get(ModelConfig, to_id)
    if not src:
        click.echo(f"Error: model_config id {from_id} not found.")
        raise SystemExit(1)
    if not dst:
        click.echo(f"Error: model_config id {to_id} not found.")
        raise SystemExit(1)
    from_name, to_name = src.model_name, dst.model_name
    # End the read transaction: on PostgreSQL the chunk walk below can run for hours,
    # and a session left idle in transaction that long is reaped by the server's
    # idle_in_transaction_session_timeout before the conversations step uses it.
    db.session.commit()

    click.echo(f"Reassigning from '{from_name}' (id={from_id}) to '{to_name}' (id={to_id})")

    postgres = db.engine.dialect.name == "postgresql"
    if postgres:
        with db.engine.connect() as conn:
            retention = _retention_job(conn)
            if retention is not None:
                click.echo(f"Error: request_logs has a retention policy (job {retention[0]}, "
                           f"config {retention[1]}).\n"
                           f"  Refreshing the aggregates over windows whose raw chunks were\n"
                           f"  dropped would erase their history. Run 'flask check-aggregate'.")
                raise SystemExit(1)
            boundaries = {name: _retention_boundary(conn, name) for name in _continuous_aggregates(conn)}
            # Read before anything moves: afterwards FROM has no raw rows left to date it.
            start = _reassign_start(conn, from_id, boundaries)
        log_count = _move_request_logs(from_id, to_id)

    result = db.session.execute(update(Conversation).where(Conversation.model == from_name).values(model=to_name))
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

    if not postgres:
        # SQLite has no chunks and no aggregates: one UPDATE in the same transaction.
        result = db.session.execute(update(RequestLog).where(RequestLog.model_config_id == from_id).values(model_config_id=to_id))
        log_count = result.rowcount
    click.echo(f"  request_logs updated: {log_count}")

    db.session.commit()

    if postgres:
        if start is None:
            click.echo("  no request_logs rows for FROM; aggregates unchanged")
        else:
            _refresh_reassigned(start, boundaries)
    click.echo("Done.")


def _move_request_logs(from_id, to_id):
    """Move FROM's request_logs rows to TO one chunk per transaction, oldest first.

    Also recompresses every chunk that is flagged compressed but has rows in its
    uncompressed heap, whether this run moved rows in it or not, so a re-run repairs
    a chunk left half-done by an interrupted run. Returns the number of rows moved.
    """
    done = []
    moved_total = 0
    with db.engine.connect() as conn:
        chunks = conn.execute(text(
            "SELECT format('%I.%I', chunk_schema, chunk_name), range_start, range_end, is_compressed "
            "FROM timescaledb_information.chunks WHERE hypertable_name = 'request_logs' "
            "ORDER BY range_start"
        )).all()
        conn.rollback()
        for chunk, range_start, range_end, compressed in chunks:
            try:
                moved = _move_chunk(conn, from_id, to_id, range_start, range_end)
            except DBAPIError as exc:
                click.echo(f"Error: moving request_logs rows in {chunk} failed: {exc.orig}")
                click.echo(f"  Chunks done: {', '.join(done)}" if done else "  No chunk was done.")
                click.echo("  History is split between FROM and TO until the same command is re-run.")
                raise SystemExit(1)
            if moved:
                moved_total += moved
                click.echo(f"  {chunk}: {moved} request_logs rows moved")
            if compressed:
                try:
                    if _recompress_chunk(conn, chunk):
                        click.echo(f"  {chunk}: recompressed")
                except DBAPIError as exc:
                    click.echo(f"Error: recompressing {chunk} failed: {exc.orig}")
                    click.echo("  Re-running the same command finishes it.")
                    raise SystemExit(1)
            done.append(chunk)
    return moved_total


def _move_chunk(conn, from_id, to_id, range_start, range_end):
    """Move FROM's rows inside one chunk's time range in one transaction. Returns the row count."""
    params = {"f": from_id, "t": to_id, "s": range_start, "e": range_end}
    with conn.begin():
        if not conn.execute(text(
            "SELECT EXISTS (SELECT 1 FROM request_logs "
            "WHERE model_config_id = :f AND time >= :s AND time < :e)"
        ), params).scalar():
            return 0
        # model_config_id is a compression segmentby column, so this UPDATE decompresses
        # every batch of FROM in the chunk, and past the per-transaction decompression
        # limit (100k by default) TimescaleDB aborts it. SET LOCAL lifts the limit for
        # this one transaction only, and COMMIT puts it back. Never a plain SET: it
        # outlives the transaction and leaks an unlimited guard to the next user of this
        # pooled connection. Never on _autocommit_connection() either: there SET LOCAL
        # has no transaction block to apply to and fails.
        conn.execute(text("SET LOCAL timescaledb.max_tuples_decompressed_per_dml_transaction = 0"))
        return conn.execute(text(
            "UPDATE request_logs SET model_config_id = :t "
            "WHERE model_config_id = :f AND time >= :s AND time < :e"
        ), params).rowcount


def _recompress_chunk(conn, chunk):
    """Recompress a compressed chunk that has rows in its uncompressed heap.

    An UPDATE on a compressed chunk writes the rows it touched back uncompressed and
    leaves the chunk flagged compressed, so the compression policy never revisits it.
    ``recompress => true`` folds the heap back in (measured on 2.27.2: heap 0 after);
    ``if_not_compressed => true`` alone would skip the chunk because of that flag.
    Returns whether it recompressed.
    """
    with conn.begin():
        # chunk is format('%I.%I') from the catalog, so it is already quoted.
        if not conn.execute(text(f"SELECT EXISTS (SELECT 1 FROM ONLY {chunk})")).scalar():
            return False
        conn.execute(text("SELECT compress_chunk(CAST(:c AS regclass), recompress => true)"), {"c": chunk})
    return True


def _retention_boundary(conn, name):
    """Start of the hour holding ``now() - drop_after`` of a retention policy on ``name``.

    None without a policy. Floored to an hour, in UTC, so refresh windows starting
    there stay aligned to every aggregate's buckets.
    """
    boundary = conn.execute(text(
        "SELECT now() - CAST(config->>'drop_after' AS interval) FROM timescaledb_information.jobs "
        "WHERE proc_name = 'policy_retention' AND hypertable_name = :n"
    ), {"n": name}).scalar()
    if boundary is None:
        return None
    return boundary.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _reassign_start(conn, from_id, boundaries):
    """Earliest time FROM still shows up, or None if it shows up nowhere.

    Checks raw request_logs and the materialisation hypertable of every aggregate
    (inside that aggregate's retention), so a re-run after an interrupted refresh
    still refreshes the months whose buckets are materialised under FROM.
    """
    found = [conn.execute(text(
        "SELECT MIN(time) FROM request_logs WHERE model_config_id = :f"
    ), {"f": from_id}).scalar()]
    for name, boundary in boundaries.items():
        bucket = _bucket_column(conn, name)
        found.append(conn.execute(text(
            f'SELECT MIN("{bucket}") FROM {_materialization_hypertable(conn, name)} '
            f'WHERE model_config_id = :f AND (CAST(:b AS timestamptz) IS NULL OR "{bucket}" >= :b)'
        ), {"f": from_id, "b": boundary}).scalar())
    found = [t for t in found if t is not None]
    return min(found) if found else None


def _refresh_reassigned(start, boundaries):
    """Refresh every aggregate month by month from the month of ``start`` to now.

    The policies only reprocess their own short windows, so without this every older
    bucket keeps FROM forever. An aggregate with its own retention starts at that
    boundary instead: refreshing older windows rebuilds rows retention drops again.
    """
    month = start.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    now = datetime.now(timezone.utc)
    with _autocommit_connection() as conn:
        for name, boundary in boundaries.items():
            first = max(month, boundary) if boundary is not None else month
            click.echo(f"  refreshing '{name}' from {first:%Y-%m-%d} to {now:%Y-%m-%d}, month by month")
            try:
                _refresh_months(conn, name, first, now)
            except _RefreshFailed as failed:
                click.echo(f"Error: refreshing '{name}' for {failed.month:%Y-%m} failed: {failed.error.orig}")
                click.echo("  request_logs is moved; re-run the same command to finish the refresh.")
                raise SystemExit(1)


# --------------------------------------------------------------------------- #
# TimescaleDB lifecycle (Phase 8): aggregate backfill and coverage check.      #
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


def _retention_job(conn):
    """``(job_id, config)`` of any retention policy on request_logs, or None.

    Matches on the job's existence, not on its interval: a policy added with
    drop_created_before stores that key instead of drop_after. Shared by
    ``check-aggregate`` and the startup tripwire in ``create_app``.
    """
    return conn.execute(text(
        "SELECT job_id, config::text FROM timescaledb_information.jobs "
        "WHERE proc_name = 'policy_retention' AND hypertable_name = 'request_logs'"
    )).first()


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

    Matches the SQLSTATE (psycopg's ``sqlstate``) rather than the message text: the
    message is not part of any stability guarantee, and matching it would turn a version
    bump into a silent loss of the retry.
    """
    deadline = time.monotonic() + _REFRESH_RETRY_BUDGET
    retries = 0
    while True:
        try:
            # psycopg 3 binds server-side and the window arguments are polymorphic
            # ("any"), so the server takes whatever type the driver sends: a naive
            # datetime would arrive as timestamp, not the timestamptz every aggregate
            # here buckets on. The casts pin the types in the SQL instead.
            conn.execute(text("CALL refresh_continuous_aggregate("
                              "CAST(:n AS regclass), CAST(:s AS timestamptz), CAST(:e AS timestamptz))"),
                         {"n": name, "s": start, "e": end})
            return retries
        except DBAPIError as exc:
            if exc.orig.sqlstate != _LOCK_NOT_AVAILABLE:
                raise
            if time.monotonic() + _REFRESH_RETRY_SLEEP > deadline:
                raise
            retries += 1
            time.sleep(_REFRESH_RETRY_SLEEP)


class _RefreshFailed(Exception):
    """A month of ``_refresh_months`` failed; carries what is needed to resume."""

    def __init__(self, month, refreshed, error):
        super().__init__(f"refreshing {month:%Y-%m} failed: {error.orig}")
        self.month = month
        self.refreshed = refreshed
        self.error = error


def _refresh_months(conn, name, start, now):
    """Refresh aggregate ``name`` month by month from ``start`` up to ``now``.

    Prints one line per month and returns the months refreshed as ``YYYY-MM``
    strings. Raises ``_RefreshFailed`` with the failing month and the months already
    refreshed. ``conn`` must be an autocommit connection (``_autocommit_connection``).
    """
    bucket = _bucket_column(conn, name)
    refreshed = []
    month = start
    while month <= now:
        try:
            _refresh_month(conn, name, bucket, month, now)
        except DBAPIError as exc:
            raise _RefreshFailed(month, refreshed, exc) from exc
        refreshed.append(f"{month:%Y-%m}")
        month = _next_month(month)
    return refreshed


def _refresh_month(conn, name, bucket, month, now):
    """Refresh ``name`` from ``month`` to the next month or ``now``, whichever is first.

    Prints one line with the row counts either side. Raises ``DBAPIError`` once the
    lock retries of ``_refresh_window`` run out. Shared by ``_refresh_months`` and the
    missing-month backfill of ``flask upgrade``.
    """
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
    except DBAPIError:
        click.echo("FAILED")
        raise
    aggregated = conn.execute(text(
        f'SELECT COUNT(*) FROM "{name}" WHERE "{bucket}" >= :s AND "{bucket}" < :e'
    ), {"s": month, "e": nxt}).scalar()
    click.echo(f"{aggregated} aggregate rows OK"
               + (f" (after {retries} lock retries)" if retries else ""))


def _month_start(dt):
    """First instant, in UTC, of the month holding ``dt``."""
    return dt.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _missing_months(conn, name, now):
    """Months whose earliest ``request_logs`` row ``name`` has not materialised.

    Returns ``(start, first_raw, first_bucket)`` per missing month, oldest first, from
    the month of the oldest raw row through the current one. ``start`` is the month's
    first instant, or the aggregate's own retention boundary inside that boundary's
    month; months before the boundary are left out, as ``reassign-model`` does.

    Two indexed probes per month: the earliest raw row and the earliest materialised
    bucket. A month is missing when it has raw rows and no bucket, or when its first
    bucket is later than the hour of its first raw row. Whole-month refreshes plus the
    policy's trailing window can only leave a month uncovered at its head, so this
    finds every gap they can leave; a hole inside a month is not detected.
    """
    oldest = conn.execute(text("SELECT MIN(time) FROM request_logs")).scalar()
    if oldest is None:
        return []
    bucket = _bucket_column(conn, name)
    table = _materialization_hypertable(conn, name)
    boundary = _retention_boundary(conn, name)
    # A refresh ends at now rounded down to a whole bucket, so the hour holding now is
    # never materialised; a month whose only rows are in it is not missing anything.
    horizon = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    missing = []
    month = _month_start(oldest)
    while month < horizon:
        nxt = _next_month(month)
        start = max(month, boundary) if boundary is not None else month
        if start < nxt:
            first_raw, first_bucket = conn.execute(text(
                "SELECT (SELECT time FROM request_logs "
                "        WHERE time >= :s AND time < :e ORDER BY time LIMIT 1), "
                f'       (SELECT "{bucket}" FROM {table} '
                f'        WHERE "{bucket}" >= :s AND "{bucket}" < :e ORDER BY "{bucket}" LIMIT 1)'
            ), {"s": start, "e": min(nxt, horizon)}).one()
            # The entity aggregate buckets by the hour, aligned to UTC.
            first_hour = None if first_raw is None else \
                first_raw.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
            if first_raw is not None and (first_bucket is None or first_bucket > first_hour):
                missing.append((start, first_raw, first_bucket))
        month = nxt
    return missing


@click.command("backfill-aggregate")
@click.option("--name", default=ENTITY_AGGREGATE, show_default=True,
              help="Continuous aggregate to materialise.")
@click.option("--from", "from_month", metavar="YYYY-MM", default=None,
              help="First month to refresh. Defaults to the month of the oldest request_logs row.")
@click.option("--force", is_flag=True,
              help="Refresh months before a retention boundary anyway, erasing those months' "
                   "aggregate rows. Never touches raw request_logs rows. Read the refusal first.")
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
        # as EMPTY and DELETES the materialised rows, with no error at all. Lumen never
        # adds a retention policy to request_logs (#167), so this is a defensive no-op
        # unless someone added one by hand in SQL — in which case refuse rather than
        # erase aggregate history on top of the raw history already lost.
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
        try:
            refreshed = _refresh_months(conn, name, start, now)
        except _RefreshFailed as failed:
            click.echo(f"Error: refreshing {failed.month:%Y-%m} failed: {failed.error.orig}")
            if failed.refreshed:
                click.echo(f"  Refreshed OK: {failed.refreshed[0]} .. {failed.refreshed[-1]} "
                           f"({len(failed.refreshed)} months).")
            else:
                click.echo("  No month was refreshed.")
            click.echo(f"  Resume with: flask backfill-aggregate "
                       f"--name {name} --from {failed.month:%Y-%m}")
            raise SystemExit(1)

    if refreshed:
        click.echo(f"Backfill complete: {len(refreshed)} months refreshed "
                   f"({refreshed[0]} .. {refreshed[-1]}).")
    else:
        click.echo("Backfill complete: no months to refresh.")


@click.command("check-aggregate")
@with_appcontext
def check_aggregate_cmd():
    """Report whether the entity aggregate covers all of request_logs. Read-only.

    ``request_logs`` is never pruned (#167). Exits 1 if a retention policy exists on
    it, if the aggregate does not reach back to its oldest row, or if any month is
    missing or only partly covered (the rule ``flask upgrade`` backfills by); the
    per-user /usage charts, which read only the aggregate, then miss that history
    until ``flask upgrade`` or ``flask backfill-aggregate`` is run.
    """
    if db.engine.dialect.name != "postgresql":
        click.echo(f"check-aggregate requires PostgreSQL/TimescaleDB; nothing to do on "
                   f"{db.engine.dialect.name}.")
        return

    with db.engine.connect() as conn:
        failed = False
        retention = _retention_job(conn)
        if retention is not None:
            click.echo(f"Error: request_logs has a retention policy (job {retention[0]}, "
                       f"config {retention[1]}).\n"
                       f"  request_logs must never be pruned. Remove it with\n"
                       f"  SELECT remove_retention_policy('request_logs');")
            failed = True

        if ENTITY_AGGREGATE not in _continuous_aggregates(conn):
            click.echo(f"Error: '{ENTITY_AGGREGATE}' does not exist. Run 'flask db upgrade' first.")
            raise SystemExit(1)
        # Read the materialisation hypertable, never the view: with
        # materialized_only = false the view answers from raw above the watermark, so a
        # never-refreshed aggregate would look fully covered through it. The question is
        # whether the per-entity charts, which read only the aggregate, see all of
        # history: is its earliest materialised bucket at or before the oldest raw row?
        earliest = conn.execute(text(
            f'SELECT MIN("{_bucket_column(conn, ENTITY_AGGREGATE)}") '
            f'FROM {_materialization_hypertable(conn, ENTITY_AGGREGATE)}'
        )).scalar()
        raw_oldest = conn.execute(text("SELECT MIN(time) FROM request_logs")).scalar()
        click.echo(f"earliest materialised bucket: {_fmt(earliest)}")
        click.echo(f"oldest request_logs row:      {_fmt(raw_oldest)}")

        covered = True
        if raw_oldest is None:
            click.echo("request_logs is empty; nothing to cover.")
        elif earliest is None or earliest > raw_oldest:
            state = "is empty" if earliest is None else "does not cover request_logs"
            click.echo(f"Error: '{ENTITY_AGGREGATE}' {state}. Run "
                       f"'flask backfill-aggregate --from {raw_oldest.astimezone(timezone.utc):%Y-%m}'.")
            covered = False

        # The same per-month rule 'flask upgrade' backfills by, so a month the earliest
        # bucket alone cannot see (an interior month, or one only the refresh policy's
        # trailing window has partly materialised) is reported too.
        missing = _missing_months(conn, ENTITY_AGGREGATE, datetime.now(timezone.utc))
        if missing:
            click.echo(f"Error: these months of '{ENTITY_AGGREGATE}' are missing or partly covered:")
            for start, first_raw, first_bucket in missing:
                state = "missing" if first_bucket is None else "partly covered"
                click.echo(f"  {start:%Y-%m}: {state} (first request_logs row {_fmt(first_raw)}, "
                           f"first materialised bucket {_fmt(first_bucket)})")
            click.echo(f"  Run 'flask upgrade', or 'flask backfill-aggregate --from "
                       f"{missing[0][0]:%Y-%m}'.")
            covered = False
        if raw_oldest is not None and covered:
            click.echo(f"{ENTITY_AGGREGATE} covers request_logs.")
        failed = failed or not covered

    if failed:
        raise SystemExit(1)


# --------------------------------------------------------------------------- #
# Migration gate: the serve step waits for the upgrade step. Never migrates.   #
# --------------------------------------------------------------------------- #

WAIT_RETRY_SECONDS = 2
WAIT_HINT_SECONDS = 30
# Bounds one PostgreSQL connection attempt (libpq ``connect_timeout``, whole seconds,
# minimum 2) and the read of alembic_version, so a database host that accepts TCP but
# never answers cannot hold the gate past --timeout or silence the upgrade hint.
WAIT_CONNECT_TIMEOUT_SECONDS = 10
UPGRADE_HINT = ("Run the upgrade step first: the image with the 'upgrade' argument, the "
                "'lumen-upgrade' Compose service, or the Helm migration Job.")


def _reject_nan(ctx, param, value):
    # Every comparison with NaN is False, so a NaN timeout would silently never fire.
    if value is not None and math.isnan(value):
        raise click.BadParameter("must be a number of seconds, not NaN.")
    return value


@click.command("wait-for-migrations")
@click.option("--timeout", type=click.FloatRange(min=0), default=None, metavar="SECONDS",
              callback=_reject_nan,
              help="Stop retrying once this many seconds have passed (exit 1); the check in "
                   "progress can add up to 10 seconds. Default: wait forever.")
@with_appcontext
def wait_for_migrations_cmd(timeout):
    """Wait until the database is at this image's migration head. Read-only.

    Exits 0 at head. While the database is behind, empty or unreachable it retries
    every 2 seconds, until --timeout runs out (exit 1) or forever without one. A
    revision this image does not know means a newer image migrated the database:
    that exits 1 at once, since waiting cannot fix it. Each PostgreSQL check gives an
    unresponsive server up to 10 seconds, so the command can exit that much after
    --timeout. Connections made while the app loads are not covered: see
    PGCONNECT_TIMEOUT in the README.
    """
    # Flask-Migrate's config and engine, so the script location and the database URL
    # resolve exactly as they do for ``flask db upgrade``.
    migrate = current_app.extensions["migrate"]
    script = ScriptDirectory.from_config(migrate.migrate.get_config())
    expected = tuple(sorted(script.get_heads()))
    known = {rev.revision for rev in script.walk_revisions()}
    engine = migrate.db.engine
    if engine.url.get_backend_name() == "postgresql":
        # Same URL, but every check opens a fresh connection that cannot block forever.
        engine = create_engine(engine.url, poolclass=NullPool, connect_args=_gate_connect_args(engine.url))
    try:
        _wait_for_heads(engine, expected, known, timeout)
    finally:
        if engine is not migrate.db.engine:
            engine.dispose()


def _gate_connect_args(url):
    """libpq arguments that bound one check without changing what it connects to.

    ``connect_args`` replaces a key of the same name from the URL's query string, and
    an explicit ``options`` also replaces ``PGOPTIONS``. So the statement timeout is
    appended to whatever options the URL or the environment already set: dropping a
    ``-c search_path=...`` there would read another schema's alembic_version than the
    one ``flask db upgrade`` writes.

    It goes last on purpose: libpq applies the last value of a repeated ``-c``, so an
    operator's own ``statement_timeout`` is overridden, but only for this one
    read-only query.
    """
    options = url.query.get("options", os.environ.get("PGOPTIONS", ""))
    if isinstance(options, tuple):
        options = " ".join(options)
    return {
        "connect_timeout": WAIT_CONNECT_TIMEOUT_SECONDS,
        "options": f"{options} -c statement_timeout={WAIT_CONNECT_TIMEOUT_SECONDS * 1000}".strip(),
    }


def _wait_for_heads(engine, expected, known, timeout):
    """The retry loop of ``wait-for-migrations``: returns at head, exits 1 otherwise."""
    start = time.monotonic()
    last_hint = start
    last_state = None
    while True:
        try:
            with engine.connect() as conn:
                current = tuple(sorted(MigrationContext.configure(conn).get_current_heads()))
        except DBAPIError as e:
            err = e.orig if e.orig is not None else e
            state = f"database unreachable ({type(err).__name__}: {' '.join(str(err).split())})"
        else:
            unknown = [rev for rev in current if rev not in known]
            if unknown:
                click.echo(f"Error: database is at revision {', '.join(unknown)}, which this image "
                           f"does not know (expected {', '.join(expected)}). A newer image has "
                           f"migrated it; run that image or a newer one.")
                raise SystemExit(1)
            if current == expected:
                click.echo(f"Database is at migration head {', '.join(expected)}.")
                return
            state = (f"database is at revision {', '.join(current) or 'none'}, "
                     f"expected {', '.join(expected)}")

        now = time.monotonic()
        if timeout is not None and now - start >= timeout:
            click.echo(f"Error: timed out after {timeout:g}s waiting for migrations: {state}. "
                       f"{UPGRADE_HINT}")
            raise SystemExit(1)
        if state != last_state:
            click.echo(f"Waiting for migrations: {state}.")
            last_state = state
        if now - last_hint >= WAIT_HINT_SECONDS:
            click.echo(f"Still waiting: {state}. {UPGRADE_HINT}")
            last_hint = now
        time.sleep(WAIT_RETRY_SECONDS)


# --------------------------------------------------------------------------- #
# Upgrade step: every migration, then the post-migration data steps. One-shot. #
# --------------------------------------------------------------------------- #

# Shared with the startup tripwire (``_warn_if_request_logs_has_retention``), so an
# operator searching the logs for it finds both.
REQUEST_LOGS_RETENTION_ERROR = (
    "request_logs has a retention policy (job %s, config %s), but request_logs "
    "must never be pruned. Remove it with "
    "SELECT remove_retention_policy('request_logs'); then run 'flask check-aggregate'."
)


def _backfill_missing_months():
    """Refresh every month of the entity aggregate that ``_missing_months`` reports.

    Oldest first, each over its whole month up to now. Safe to re-run: it probes again
    and resumes at the first month still missing, and re-refreshing a month recomputes
    it from raw rows. Exits 1 when a month still fails after the lock retries.
    """
    if db.engine.dialect.name != "postgresql":
        click.echo(f"Aggregate backfill: skipped, needs PostgreSQL/TimescaleDB "
                   f"(database is {db.engine.dialect.name}).")
        return
    with _autocommit_connection() as conn:
        if not conn.execute(text(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')"
        )).scalar():
            click.echo("Aggregate backfill: skipped, TimescaleDB is not installed.")
            return
        # Defensive: Lumen never adds one. Refreshing a window whose raw chunks were
        # dropped would erase its aggregate rows, so leave the aggregate alone, and do
        # not fail the upgrade over a policy only an operator can remove.
        retention = _retention_job(conn)
        if retention is not None:
            current_app.logger.error(REQUEST_LOGS_RETENTION_ERROR, retention[0], retention[1])
            click.echo("Aggregate backfill: skipped, request_logs has a retention policy.")
            return

        now = datetime.now(timezone.utc)
        missing = _missing_months(conn, ENTITY_AGGREGATE, now)
        if not missing:
            click.echo(f"Aggregate backfill: no missing months in '{ENTITY_AGGREGATE}'.")
            return
        click.echo(f"Aggregate backfill: refreshing {len(missing)} missing or partly covered "
                   f"months of '{ENTITY_AGGREGATE}'.")
        bucket = _bucket_column(conn, ENTITY_AGGREGATE)
        for start, _, _ in missing:
            try:
                _refresh_month(conn, ENTITY_AGGREGATE, bucket, start, now)
            except DBAPIError as exc:
                click.echo(f"Error: refreshing {start:%Y-%m} failed: {exc.orig}")
                click.echo("  Re-run 'flask upgrade' to resume at the first month still missing.")
                raise SystemExit(1)


# Run in order after the migrations, each only once the previous one succeeded. Every
# step must be safe to re-run and finish what an earlier, failed run left undone.
POST_MIGRATION_STEPS = (_backfill_missing_months,)


@click.command("upgrade")
@with_appcontext
def upgrade_cmd():
    """Bring the database up to date for this image, then exit.

    Applies every migration (``flask db upgrade`` to head), then runs the
    post-migration data steps: today, the backfill of the entity aggregate's missing
    months. Stops at the first failure with exit 1; running it again resumes. This is
    the one-shot upgrade step that runs before the app starts.
    """
    click.echo("Applying migrations ...")
    migrate_upgrade()
    for step in POST_MIGRATION_STEPS:
        step()
    click.echo("Upgrade complete.")
