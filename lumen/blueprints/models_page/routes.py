from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from urllib.parse import urlparse

import requests as http_requests
from flask import Blueprint, abort, redirect, render_template, session, url_for
from sqlalchemy import func, select

from lumen.decorators import login_required
from lumen.extensions import db
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.request_log import RequestLog
from lumen.services.llm import bulk_model_access_info, get_model_access_status, has_model_consent

models_page_bp = Blueprint("models_page", __name__)


@models_page_bp.route("/models")
@login_required
def index():
    entity_id = session.get("entity_id")
    all_configs = db.session.execute(select(ModelConfig).filter_by(active=True).order_by(ModelConfig.model_name)).scalars().all()
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
    config = db.first_or_404(select(ModelConfig).filter_by(model_name=model_name, active=True))
    endpoints = config.endpoints.all()

    healthy_count = sum(1 for e in endpoints if e.healthy)
    if not endpoints or healthy_count == 0:
        status = "down"
    elif healthy_count < len(endpoints):
        status = "degraded"
    else:
        status = "ok"

    now = datetime.now(timezone.utc).replace(tzinfo=None)
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

    entity_id = session.get("entity_id")
    access_status = get_model_access_status(entity_id, config.id) if entity_id else "blocked"
    if access_status == "blocked":
        abort(HTTPStatus.NOT_FOUND)
    consent = (
        db.session.execute(
            select(EntityModelConsent).filter_by(entity_id=entity_id, model_config_id=config.id)
        ).scalar_one_or_none()
        if access_status == "graylist" and entity_id
        else None
    )

    return render_template(
        "model_detail.html",
        config=config,
        endpoints=endpoints,
        healthy_count=healthy_count,
        status=status,
        requests_last_hour=requests_last_hour,
        requests_last_day=requests_last_day,
        access_status=access_status,
        consent=consent,
    )


@models_page_bp.route("/models/<path:model_name>/consent", methods=["POST"])
@login_required
def model_consent(model_name):
    config = db.first_or_404(select(ModelConfig).filter_by(model_name=model_name, active=True))
    entity_id = session["entity_id"]
    if get_model_access_status(entity_id, config.id) != "graylist":
        abort(HTTPStatus.BAD_REQUEST)
    if not has_model_consent(entity_id, config.id):
        db.session.add(EntityModelConsent(
            entity_id=entity_id,
            model_config_id=config.id,
            consented_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.session.commit()
    return redirect(url_for("models_page.detail", model_name=model_name))


@models_page_bp.route("/models/<path:model_name>/readme")
@login_required
def model_readme(model_name):
    config = db.first_or_404(select(ModelConfig).filter_by(model_name=model_name, active=True))
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
