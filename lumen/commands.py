import os
import re
import shutil
import tempfile
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import click
import yaml
from flask import current_app
from flask.cli import with_appcontext
from sqlalchemy import delete, select, update

from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint

from .extensions import db

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

    A bare date means midnight UTC of that day (exclusive), i.e. the model is
    usable through the end of the previous day. Unparseable values are dropped
    with a warning."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        # The admin config editor round-trips YAML dates through JSON, which
        # Flask serializes in RFC 822 form ("Thu, 31 Dec 2026 00:00:00 GMT").
        try:
            parsed = parsedate_to_datetime(str(value))
        except ValueError:
            _warn_once(("end-date", model_name), "invalid end_date '%s' on model '%s'; ignoring", value, model_name)
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
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
