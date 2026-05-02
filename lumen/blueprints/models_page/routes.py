import requests as http_requests
from flask import Blueprint, render_template

from lumen.decorators import login_required
from lumen.extensions import db
from lumen.models.model_config import ModelConfig
from sqlalchemy import text as sa_text

models_page_bp = Blueprint("models_page", __name__)


@models_page_bp.route("/models")
@login_required
def index():
    configs = ModelConfig.query.order_by(ModelConfig.model_name).all()
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

    return render_template(
        "model_detail.html",
        config=config,
        endpoints=endpoints,
        healthy_count=healthy_count,
        status=status,
        requests_last_hour=requests_last_hour,
        requests_last_day=requests_last_day,
    )


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
