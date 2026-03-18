from flask import Blueprint, render_template, request, jsonify, session

from app.decorators import login_required
from app.models.model_config import ModelConfig
from app.services.llm import check_and_deduct_tokens, send_message

chat_bp = Blueprint("chat", __name__)


@chat_bp.route("/chat")
@login_required
def chat_page():
    active_models = (
        ModelConfig.query.filter_by(active=True).order_by(ModelConfig.model_name).all()
    )
    return render_template("chat.html", active_models=active_models)


@chat_bp.route("/chat/send", methods=["POST"])
@login_required
def chat_send():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    messages = data.get("messages", [])
    model = data.get("model")

    if not messages or not model:
        return jsonify({"error": "Missing messages or model"}), 400

    entity_id = session["entity_id"]

    model_config = ModelConfig.query.filter_by(model_name=model, active=True).first()
    if not model_config:
        return jsonify({"error": f"Unknown model: {model}"}), 400

    ok, code, msg = check_and_deduct_tokens(entity_id, model_config.id)
    if not ok:
        return jsonify({"error": msg}), code

    try:
        result = send_message(messages, model, entity_id=entity_id, source="chat")
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify(result)
