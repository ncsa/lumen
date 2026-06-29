import re

from flask import Blueprint, current_app, render_template, request, session
from sqlalchemy import select

from lumen.extensions import db
from lumen.models.model_config import ModelConfig
from lumen.services.llm import bulk_model_access_info

connect_bp = Blueprint("connect", __name__)


def _accessible_models(entity_id):
    """Return active models the entity may use, as serialisable dicts.

    Mirrors the access filtering on the models dashboard (active configs minus
    those the entity is blocked from). Returns [] when not logged in.
    """
    if not entity_id:
        return []
    configs = db.session.execute(
        select(ModelConfig).where(ModelConfig.active).order_by(ModelConfig.model_name)
    ).scalars().all()
    statuses, _ = bulk_model_access_info(entity_id, [c.id for c in configs])
    return [
        {
            "id": c.model_name,
            "name": c.model_name,
            "input_modalities": c.input_modalities or [],
            "output_modalities": c.output_modalities or [],
        }
        for c in configs
        if statuses.get(c.id, "allowed") != "blocked"
    ]


@connect_bp.route("/connect")
def index():
    models = _accessible_models(session.get("entity_id"))

    requested = request.args.get("model")
    selected = requested if any(m["id"] == requested for m in models) else ""

    app_name = current_app.config["APP_NAME"]
    provider = re.sub(r"[^a-z0-9]", "", app_name.lower()) or "lumen"

    return render_template(
        "connect.html",
        models=models,
        selected_model=selected,
        base_url=request.url_root.rstrip("/") + "/v1",
        provider=provider,
        provider_name=app_name,
    )
