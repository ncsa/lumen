from collections import Counter
from http import HTTPStatus
from urllib.parse import urlparse

import requests as http_requests
from flask import Blueprint, abort, g, render_template, request
from sqlalchemy import select

from lumen.decorators import login_required
from lumen.extensions import db
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.services import request_rates
from lumen.services.llm import _consent_satisfied, bulk_model_access_statuses, consent_rows, get_model_access_status, model_notices
from lumen.services.model_resolver import resolve_model_config

models_page_bp = Blueprint("models_page", __name__)

# ``?show=`` values the stat cards filter the list by; anything else shows every model.
SHOW_VIEWS = ("healthy", "early_access", "needs_ack")

# ``?sort=`` keys for the model list -> (values compared in order, first-click direction).
# Numbers sort high to low first, except price, which sorts cheapest first.
SORTS = {
    "name": ((), "asc"),
    "context": ((lambda c: c.context_window,), "desc"),
    "max_output": ((lambda c: c.max_output_tokens,), "desc"),
    "price": ((lambda c: c.input_cost_per_million, lambda c: c.output_cost_per_million), "asc"),
}
SORT_DIRS = ("asc", "desc")


def _sort_params(args):
    """``(sort, dir)`` from the query string; no or an unknown key or dir gives name A to Z."""
    sort, direction = args.get("sort"), args.get("dir")
    if sort not in SORTS or (direction is not None and direction not in SORT_DIRS):
        return "name", "asc"
    return sort, direction or SORTS[sort][1]


def _sort_configs(configs, sort, direction):
    """Sort models by ``sort`` in ``direction``.

    A missing value sorts last in both directions, and ties fall back to the
    model name A to Z. The inline script in models.html sorts the same way.
    """
    desc = direction == "desc"

    def key(config):
        parts = []
        for value_of in SORTS[sort][0]:
            value = value_of(config)
            parts += [value is None, 0 if value is None else (-value if desc else value)]
        return parts

    by_name = sorted(configs, key=lambda c: (c.model_name.lower(), c.model_name), reverse=desc and sort == "name")
    return by_name if sort == "name" else sorted(by_name, key=key)


def _visible_model_or_404(model_name):
    """Return an active model visible to the current entity, or hide its existence.

    ``model_name`` may be the canonical name or one of its aliases; an alias
    resolves to the canonical model so an old /models/<alias> deep link still
    shows the (renamed/upgraded) model it points at.
    """
    config = resolve_model_config(model_name) or abort(HTTPStatus.NOT_FOUND)
    access_status = get_model_access_status(g.entity_id, config.id)
    if access_status == "blocked":
        abort(HTTPStatus.NOT_FOUND)
    return config, access_status


@models_page_bp.route("/models")
@login_required
def index():
    entity_id = g.get("entity_id")
    all_configs = db.session.execute(select(ModelConfig).where(ModelConfig.active).order_by(ModelConfig.model_name)).scalars().all()
    access_statuses = bulk_model_access_statuses(entity_id, [c.id for c in all_configs])
    configs = [c for c in all_configs if access_statuses.get(c.id, "allowed") != "blocked"]
    model_ids = [c.id for c in configs]
    # Acknowledgement state for the Access & Acknowledgments column, the Needs Ack
    # card and its filter, all from ONE read of the consent rows so they always
    # agree: each requirement's own acceptance time, counted only while that
    # requirement is in force (one added after consent shows as required), and
    # whether every requirement is satisfied.
    ack_configs = [c for c in configs if access_statuses.get(c.id) == "needs_ack"]
    rows = consent_rows(entity_id, [c.id for c in ack_configs])
    ack_map = {}
    for c in ack_configs:
        notice, early_notice = model_notices(c)
        row = rows.get(c.id)
        ack_map[c.id] = {
            "consented": _consent_satisfied(row, c),
            "ack_at": row.consented_at if row and c.needs_ack else None,
            "early_access_at": row.early_access_at if row and c.early_access else None,
            "notice": notice,
            "early_notice": early_notice,
        }
    endpoints_map: dict[int, list] = {}
    for ep in db.session.execute(select(ModelEndpoint).where(ModelEndpoint.model_config_id.in_(model_ids), ModelEndpoint.active.is_(True))).scalars().all():
        endpoints_map.setdefault(ep.model_config_id, []).append(ep)

    # One status per model, shared by the stat cards and the rows so both count the same way.
    statuses = {c.id: _health_status(endpoints_map.get(c.id, [])) for c in configs}
    status_counts = Counter(statuses.values())
    stats = {
        "total": len(configs),
        # Healthy counts every model that serves requests, degraded included.
        "healthy": status_counts["ok"] + status_counts["degraded"],
        "degraded": status_counts["degraded"],
        "down": status_counts["down"],
        "no_endpoints": status_counts["no_endpoints"],
        "early_access": sum(1 for c in configs if c.early_access),
        "needs_ack": sum(1 for a in ack_map.values() if not a["consented"]),
        # Models with an acknowledgment requirement, and how many of them this user has accepted.
        "ack_total": len(ack_map),
        "acknowledged": sum(1 for a in ack_map.values() if a["consented"]),
    }
    show = request.args.get("show")
    sort, direction = _sort_params(request.args)
    configs = _sort_configs(configs, sort, direction)
    return render_template(
        "models.html",
        configs=configs,
        endpoints_map=endpoints_map,
        ack_map=ack_map,
        statuses=statuses,
        stats=stats,
        show=show if show in SHOW_VIEWS else None,
        sort=sort,
        sort_dir=direction,
        token_count=_token_count,
        modalities=_modalities,
    )


def _health_status(endpoints):
    """``ok`` / ``degraded`` / ``down`` / ``no_endpoints`` from a model's active endpoints."""
    if not endpoints:
        return "no_endpoints"
    healthy = sum(1 for e in endpoints if e.healthy)
    if healthy == len(endpoints):
        return "ok"
    return "degraded" if healthy else "down"


def _token_count(n):
    """Short token count: 128000 and 131072 both read ``128K``, 1048576 reads ``1M``; ``—`` when unset.

    Round decimal counts use 1000s, power-of-two counts use 1024s.
    """
    if not n:
        return "\u2014"
    for unit, decimal, binary in (("M", 1_000_000, 1024 * 1024), ("K", 1_000, 1024)):
        if n >= decimal:
            value = n / binary if n % binary == 0 and n % decimal else n / decimal
            return f"{value:.1f}".removesuffix(".0") + unit
    return str(n)


_DASH = "\u2014"


def _modality_names(value):
    """``"Text, Image"`` from a stored modality list, or ``None`` when unknown.

    The JSON column is not schema-checked, so a bare string counts as one
    modality and any other shape (a number, an object) or non-string item is
    treated as unknown rather than iterated.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None
    return ", ".join(v.strip().capitalize() for v in value if isinstance(v, str) and v.strip()) or None


def _modalities(config):
    """``Text, Image → Text``; a side the model does not publish shows ``—``."""
    inputs, outputs = _modality_names(config.input_modalities), _modality_names(config.output_modalities)
    if inputs is None and outputs is None:
        return _DASH
    return f"{inputs or _DASH} \u2192 {outputs or _DASH}"


@models_page_bp.route("/models/<path:model_name>")
@login_required
def detail(model_name):
    config, access_status = _visible_model_or_404(model_name)
    endpoints = [ep for ep in config.endpoints if ep.active]

    healthy_count = sum(1 for e in endpoints if e.healthy)
    if not endpoints or healthy_count == 0:
        status = "down"
    elif healthy_count < len(endpoints):
        status = "degraded"
    else:
        status = "ok"

    counts = request_rates.rates(config.id).get(config.id, {})
    requests_last_hour = counts.get("last_hour", 0)
    requests_last_day = counts.get("last_day", 0)

    entity_id = g.entity_id
    consent = (
        db.session.execute(
            select(EntityModelConsent).filter_by(entity_id=entity_id, model_config_id=config.id)
        ).scalar_one_or_none()
        if access_status == "needs_ack" and entity_id
        else None
    )

    ack_notice, early_access_notice = model_notices(config)
    # The model's own notice always shows; the acknowledgement text only while
    # the entity still needs to acknowledge the model (#492).
    ack_notice = ack_notice if access_status == "needs_ack" else None
    # Consent is satisfied only when every acknowledgement requirement has its
    # timestamp; a requirement added later re-shows the acknowledge button.
    consented = _consent_satisfied(consent, config)
    consent_display_at = None
    if consented and consent is not None:
        times = [t for t in (consent.consented_at, consent.early_access_at) if t is not None]
        consent_display_at = max(times) if times else None

    return render_template(
        "model_detail.html",
        config=config,
        aliases=sorted(a.alias for a in config.aliases),
        endpoints=endpoints,
        healthy_count=healthy_count,
        status=status,
        requests_last_hour=requests_last_hour,
        requests_last_day=requests_last_day,
        access_status=access_status,
        consented=consented,
        consent_display_at=consent_display_at,
        model_notice=config.notice,
        ack_notice=ack_notice,
        early_access_notice=early_access_notice if access_status == "needs_ack" else None,
    )



@models_page_bp.route("/models/<path:model_name>/readme")
@login_required
def model_readme(model_name):
    config, _ = _visible_model_or_404(model_name)
    parsed = urlparse(config.url or "")
    if parsed.netloc != "huggingface.co":
        return "", HTTPStatus.NOT_FOUND
    parts = parsed.path.strip("/").split("/")[:2]
    if len(parts) < 2:
        return "", HTTPStatus.NOT_FOUND
    raw_url = f"https://huggingface.co/{'/'.join(parts)}/raw/main/README.md"
    try:
        r = http_requests.get(raw_url, timeout=10)
        r.raise_for_status()
        text = r.text
        # Strip YAML front-matter (---\n...\n---\n)
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                text = text[end + 4:].lstrip("\n")
        return text, HTTPStatus.OK, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception:
        return "", HTTPStatus.BAD_GATEWAY
