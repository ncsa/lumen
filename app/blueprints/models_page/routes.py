from flask import Blueprint, render_template

from app.decorators import login_required
from app.models.model_config import ModelConfig

models_page_bp = Blueprint("models_page", __name__)


@models_page_bp.route("/models")
@login_required
def index():
    configs = ModelConfig.query.order_by(ModelConfig.model_name).all()
    return render_template("models.html", configs=configs)
