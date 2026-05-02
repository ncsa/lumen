from datetime import datetime

import requests as http_requests
from flask import Blueprint, abort, redirect, render_template, session, url_for

from lumen.decorators import login_required
from lumen.extensions import db
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.model_config import ModelConfig
from lumen.services.llm import get_model_access_status, has_model_consent
from sqlalchemy import text as sa_text

models_page_bp = Blueprint("models_page", __name__)


@models_page_bp.route("/models")
@login_required
def index():
    entity_id = session.get("entity_id")
    all_configs = ModelConfig.query.filter_by(active=True).order_by(ModelConfig.model_name).all()
    configs = [c for c in all_configs if get_model_access_status(entity_id, c.id) != "blocked"]
    return render_template("models.html", configs=configs)


@models_page_bp.route("/models/<path:model_name>")
@login_required
def detail(model_name):
    config = ModelConfig.query.filter_by(model_name=model_name, active=True).first_or_404()
    endpoints = config.endpoints.all()

    healthy_count = sum(1 for e in endpoints if e.healthy)
    if not endpoints or healthy_count == 0:
        status = "down"
    elif healthy_count < len(endpoints):
        status = "degraded"
    else:
        status = "ok"

    sql = sa_text("""
        SELECT
            COUNT(*) FILTER (WHERE time > NOW() - INTERVAL '1 hour')  AS last_hour,
            COUNT(*) FILTER (WHERE time > NOW() - INTERVAL '1 day')   AS last_day
        FROM request_logs
        WHERE time > NOW() - INTERVAL '1 day'
          AND model_config_id = :model_id
    """)
    row = db.session.execute(sql, {"model_id": config.id}).fetchone()
    requests_last_hour = row.last_hour if row else 0
    requests_last_day = row.last_day if row else 0

    entity_id = session.get("entity_id")
    access_status = get_model_access_status(entity_id, config.id) if entity_id else "blocked"
    if access_status == "blocked":
        abort(404)
    consent = (
        EntityModelConsent.query.filter_by(entity_id=entity_id, model_config_id=config.id).first()
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
    config = ModelConfig.query.filter_by(model_name=model_name, active=True).first_or_404()
    entity_id = session["entity_id"]
    if get_model_access_status(entity_id, config.id) != "graylist":
        abort(400)
    if not has_model_consent(entity_id, config.id):
        db.session.add(EntityModelConsent(
            entity_id=entity_id,
            model_config_id=config.id,
            consented_at=datetime.utcnow(),
        ))
        db.session.commit()
    return redirect(url_for("models_page.detail", model_name=model_name))


@models_page_bp.route("/models/<path:model_name>/readme")
@login_required
def model_readme(model_name):
    config = ModelConfig.query.filter_by(model_name=model_name, active=True).first_or_404()
    if not config.url or "huggingface.co" not in config.url:
        return "", 404
    parts = config.url.replace("https://huggingface.co/", "").split("/")[:2]
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
        return text, 200, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception:
        return "", 502
