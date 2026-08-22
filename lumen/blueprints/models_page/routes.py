from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from urllib.parse import urlparse

import requests as http_requests
from flask import Blueprint, abort, render_template, session
from sqlalchemy import func, select

from lumen.decorators import is_admin, login_required
from lumen.extensions import db
from lumen.models.entity import Entity
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.group import Group
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.model_group_access import ModelGroupAccess
from lumen.models.request_log import RequestLog
from lumen.services.llm import _consent_satisfied, bulk_model_access_info, get_model_access_status, model_notices

models_page_bp = Blueprint("models_page", __name__)


def _visible_model_or_404(model_name):
    """Return an active model visible to the current entity, or hide its existence."""
    config = db.first_or_404(
        select(ModelConfig).where(ModelConfig.model_name == model_name, ModelConfig.active)
    )
    access_status = get_model_access_status(session["entity_id"], config.id)
    if access_status == "blocked":
        abort(HTTPStatus.NOT_FOUND)
    return config, access_status


@models_page_bp.route("/models")
@login_required
def index():
    entity_id = session.get("entity_id")
    all_configs = db.session.execute(select(ModelConfig).where(ModelConfig.active).order_by(ModelConfig.model_name)).scalars().all()
    access_statuses, _ = bulk_model_access_info(entity_id, [c.id for c in all_configs])
    configs = [c for c in all_configs if access_statuses.get(c.id, "allowed") != "blocked"]
    model_ids = [c.id for c in configs]
    endpoints_map: dict[int, list] = {}
    for ep in db.session.execute(select(ModelEndpoint).where(ModelEndpoint.model_config_id.in_(model_ids))).scalars().all():
        endpoints_map.setdefault(ep.model_config_id, []).append(ep)
    return render_template("models.html", configs=configs, endpoints_map=endpoints_map)


@models_page_bp.route("/models/<path:model_name>")
@login_required
def detail(model_name):
    config, access_status = _visible_model_or_404(model_name)
    endpoints = list(config.endpoints)

    healthy_count = sum(1 for e in endpoints if e.healthy)
    if not endpoints or healthy_count == 0:
        status = "down"
    elif healthy_count < len(endpoints):
        status = "degraded"
    else:
        status = "ok"

    now = datetime.now(timezone.utc)
    requests_last_hour = db.session.scalar(
        select(func.count()).select_from(RequestLog).where(
            RequestLog.model_config_id == config.id,
            RequestLog.time >= now - timedelta(hours=1),
        )
    )
    requests_last_day = db.session.scalar(
        select(func.count()).select_from(RequestLog).where(
            RequestLog.model_config_id == config.id,
            RequestLog.time >= now - timedelta(days=1),
        )
    )

    entity_id = session["entity_id"]
    consent = (
        db.session.execute(
            select(EntityModelConsent).filter_by(entity_id=entity_id, model_config_id=config.id)
        ).scalar_one_or_none()
        if access_status == "needs_ack" and entity_id
        else None
    )

    ack_notice, early_access_notice = model_notices(config)
    effective_notice = ack_notice if access_status == "needs_ack" else config.notice
    # Consent is satisfied only when every acknowledgement requirement has its
    # timestamp; a requirement added later re-shows the acknowledge button.
    consented = _consent_satisfied(consent, config.needs_ack, config.early_access)
    consent_display_at = None
    if consented and consent is not None:
        times = [t for t in (consent.consented_at, consent.early_access_at) if t is not None]
        consent_display_at = max(times) if times else None

    # Shown in the admin-only Access card; the edit dialog fetches fresh data on open.
    granted_group_names = []
    viewer = db.session.get(Entity, entity_id) if entity_id else None
    if viewer and is_admin(viewer):
        granted_group_names = [
            name for (name,) in db.session.execute(
                select(Group.name)
                .join(ModelGroupAccess, ModelGroupAccess.group_id == Group.id)
                .where(ModelGroupAccess.model_config_id == config.id)
                .order_by(Group.name)
            ).all()
        ]
    return render_template(
        "model_detail.html",
        config=config,
        endpoints=endpoints,
        healthy_count=healthy_count,
        status=status,
        requests_last_hour=requests_last_hour,
        requests_last_day=requests_last_day,
        access_status=access_status,
        consented=consented,
        consent_display_at=consent_display_at,
        effective_notice=effective_notice,
        early_access_notice=early_access_notice if access_status == "needs_ack" else None,
        granted_group_names=granted_group_names,
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
