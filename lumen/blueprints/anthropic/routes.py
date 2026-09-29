"""Anthropic Messages API (ncsa/lumen#61): /v1/messages and /v1/messages/count_tokens.

A second protocol over the OpenAI routes' machinery, kept in its own blueprint.
Authentication, model access, consent, coin budget, rate limiting, endpoint
selection, live-state admission, billing and RequestLog all run through the
functions in ``blueprints/api/routes.py``, called as they are: requests are
translated to OpenAI before them, responses translated back after them, and
nothing in that module knows this blueprint exists. RequestLog.source stays
"api". The translation itself lives in ``services/anthropic_protocol.py``.
"""
import json
import logging
from functools import wraps
from http import HTTPStatus

from flask import Blueprint, Response, current_app, g, jsonify, request

from lumen.blueprints.api.routes import (
    _api_key_id,
    _api_limit,
    _complete_and_bill,
    _do_chat,
    api_key_required,
)
from lumen.extensions import limiter
from lumen.services.anthropic_protocol import (
    ERROR_STATUS,
    AnthropicError,
    StreamTranslator,
    check_headers,
    count_tokens,
    error_body,
    error_type_for,
    translate_request,
    translate_response,
)
from lumen.services.crypto import cache_salt_for_entity
from lumen.services.llm import get_effective_limit
from lumen.services.model_resolver import resolve_model_config

logger = logging.getLogger(__name__)

anthropic_bp = Blueprint("anthropic", __name__, url_prefix="/v1/messages")


@anthropic_bp.record_once
def _exempt_from_csrf(state):
    # API clients send no CSRF token; the OpenAI blueprint is exempted in
    # create_app. CSRFProtect registers itself on app.extensions before any
    # blueprint is registered.
    state.app.extensions["csrf"].exempt(anthropic_bp)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

def _anthropic_err(message: str, err_type: str = "invalid_request_error"):
    # The refusal reason is logged because this surface answers a client that
    # cannot show it: Claude Code retries a refused request and then reports its
    # own generic failure, so the access log's bare "400 -" is all an operator
    # would otherwise have to work from.
    logger.info("anthropic request refused (%s): %s", err_type, message)
    return jsonify(error_body(message, err_type)), ERROR_STATUS[err_type]


@anthropic_bp.after_app_request
def _anthropic_error_envelope(response):
    """Re-shape any error under /v1/messages into Anthropic's envelope.

    Errors on this path mostly come from shared code that answers in Lumen's
    OpenAI envelope: api_key_required, _preflight, the upstream call, the
    limiter's 429 handler, the app's 404/500 handlers. Routing errors (an
    unknown path or method) never reach a blueprint, hence an app-wide hook
    filtered by path rather than a blueprint one. Status and headers
    (Retry-After) are kept; only the body and the error's name change.
    """
    if response.status_code < HTTPStatus.BAD_REQUEST or not request.path.startswith("/v1/messages"):
        return response
    body = response.get_json(silent=True) if response.is_json else None
    if isinstance(body, dict) and body.get("type") == "error":
        return response  # already Anthropic-shaped
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message, openai_type = error.get("message") or "Request failed", error.get("type")
    else:
        # Werkzeug's HTML page (405) or a non-envelope body.
        message, openai_type = HTTPStatus(response.status_code).phrase, None
    status = HTTPStatus(response.status_code)
    response.set_data(json.dumps(error_body(message, error_type_for(openai_type, status))))
    response.mimetype = "application/json"
    return response


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def anthropic_auth_required(f):
    """Accept the key in ``x-api-key`` (what Anthropic clients send) or as a bearer.

    ``api_key_required`` reads only ``Authorization: Bearer``, so an
    ``x-api-key`` is presented to it in that form and the check itself is the
    OpenAI surface's, unchanged. If both headers are present they must agree —
    a mismatch is refused rather than resolved by precedence, because silently
    picking one bills a key the caller did not intend to use.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        header_key = (request.headers.get("x-api-key") or "").strip()
        auth_header = request.headers.get("Authorization", "")
        scheme, _, credentials = auth_header.partition(" ")
        # The scheme is case-insensitive (RFC 7235), so "bearer x" is still a key
        # that must agree with x-api-key.
        bearer = credentials.strip() if scheme.lower() == "bearer" else ""
        if header_key and bearer and header_key != bearer:
            return _anthropic_err(
                "x-api-key and Authorization headers disagree; send only one.",
                "authentication_error")
        if not header_key and not bearer:
            return _anthropic_err(
                "Missing API key. Send it in the x-api-key header.",
                "authentication_error")
        # Lumen keys are ASCII. Anything else is refused here: the monitor-token
        # comparison inside api_key_required raises on a non-ASCII str.
        if not (header_key or bearer).isascii():
            return _anthropic_err("Invalid or inactive API key", "authentication_error")
        # api_key_required reads "Authorization: Bearer <key>" exactly.
        request.environ["HTTP_AUTHORIZATION"] = f"Bearer {header_key or bearer}"
        return api_key_required(f)(*args, **kwargs)

    return decorated


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _anthropic_request(require_max_tokens: bool = True):
    """Validate the protocol headers, parse the body and translate it.

    Raises AnthropicError, which both views answer with a 400.
    """
    check_headers(request.headers)

    if request.mimetype != "application/json":
        raise AnthropicError("content-type must be application/json")

    try:
        data = request.get_json(silent=True)
    except RecursionError:
        # silent=True only swallows a parse error; deeply nested JSON exhausts
        # the stack instead, and that escapes to Flask as an unhandled
        # exception.
        raise AnthropicError("Request body is nested too deeply") from None
    if not data:
        raise AnthropicError("Invalid request body")

    return translate_request(data, require_max_tokens=require_max_tokens)


def _translate_stream(openai_events, translator: StreamTranslator):
    """Re-render the OpenAI route's SSE stream as Anthropic events.

    ``openai_events`` is the generator behind the OpenAI streaming response;
    admission, billing, disconnect and abort accounting all happen inside it.
    It is drained to the end so its own cleanup runs, and closed if this
    generator is closed first, so a client going away reaches it the same way
    it would on the OpenAI route. Its terminal ``[DONE]`` comes after billing,
    and the Anthropic terminal events are sent only then.
    """
    failed = False
    try:
        for event in openai_events:
            data = event.removeprefix("data: ").strip()
            if data == "[DONE]":
                if not failed:
                    yield from translator.finish()
                continue
            payload = json.loads(data)
            error = payload.get("error")
            if isinstance(error, dict):
                failed = True
                yield from translator.error(error.get("message", "Upstream error"),
                                            error.get("type", "api_error"))
                continue
            yield from translator.chunk(payload)
    finally:
        openai_events.close()


@anthropic_bp.route("", methods=["POST"])
@anthropic_auth_required
@limiter.limit(_api_limit, key_func=_api_key_id)
def messages():
    try:
        model_name, msgs, stream, kwargs, extra_body = _anthropic_request()
    except AnthropicError as exc:
        return _anthropic_err(str(exc))

    # Same prefix-cache isolation as the OpenAI surface (ncsa/lumen#36); this
    # path builds its own extra_body because none of the client's fields reach
    # it verbatim.
    extra_body["cache_salt"] = cache_salt_for_entity(g.entity.id)
    kwargs["extra_body"] = extra_body

    if stream:
        result = _do_chat(model_name, msgs, True, **kwargs)
        if not isinstance(result, Response):
            # A _preflight rejection: nothing has been streamed yet, so it is an
            # ordinary error response (re-enveloped on the way out).
            return result
        return Response(_translate_stream(result.response, StreamTranslator(model_name)),
                        content_type="text/event-stream")

    response, err = _complete_and_bill(model_name, msgs, **kwargs)
    if err:
        return err
    return jsonify(translate_response(response.model_dump(), model_name))


@anthropic_bp.route("/count_tokens", methods=["POST"])
@anthropic_auth_required
@limiter.limit(_api_limit, key_func=_api_key_id)
def count_tokens_view():
    """Estimate the prompt tokens of a Messages request.

    Claude Code calls this to show context usage. Lumen has no tokenizer for the
    backends it fronts, so the answer is the same character-based estimate the
    abort accounting uses — documented as an estimate. Model access is still
    checked, nothing is sent upstream, and nothing is billed.
    """
    try:
        model_name, msgs, _, kwargs, _ = _anthropic_request(require_max_tokens=False)
    except AnthropicError as exc:
        return _anthropic_err(str(exc))

    # Same rule as GET /v1/models/<id>: a model the caller may not use answers
    # as a missing one, so an owned model's existence is not discoverable.
    config = resolve_model_config(model_name)
    consent_required = current_app.config.get("API_REQUIRE_MODEL_CONSENT", True)
    if not config or get_effective_limit(g.entity.id, config.id, require_consent=consent_required) is None:
        return _anthropic_err(f"Model '{model_name}' not found", "not_found_error")

    return jsonify({"input_tokens": count_tokens(msgs, kwargs.get("tools"))})
