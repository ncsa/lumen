"""OAuth key-request flows: device flow (CLI) and authorization code + PKCE (web).

Both flows end at the same place: the user approves a request on a shared
consent page, and Lumen mints the API key at claim time inside
``POST /oauth/token`` — the plaintext key exists only inside that response.
No key material is ever accepted from a client or persisted outside the
hash/hint columns, and the one-time secrets used to reach that response (the
device code, the authorization code together with its PKCE verifier) travel in
POST bodies only. See the security model in the issue #67 plan.
"""

import base64
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from datetime import timedelta
from http import HTTPStatus
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from flask import Blueprint, g, jsonify, make_response, redirect, render_template, request, session, url_for
from sqlalchemy import case, delete, select, update
from sqlalchemy.exc import IntegrityError

from lumen.blueprints.profile.routes import key_secret_fields, set_key_secret
from lumen.decorators import login_required
from lumen.extensions import _chat_entity_id, _chat_limit, db, limiter
from lumen.models.api_key import KEY_TYPE_NORMAL, APIKey
from lumen.models.auth_request import AuthRequest
from lumen.models.entity import Entity
from lumen.models.entity_manager import EntityManager, get_project_role
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.model_config import ModelConfig
from lumen.services.key_expiry import key_expires_at, key_ttl_choices, resolve_key_ttl
from lumen.services.llm import _consent_satisfied, bulk_model_access_statuses, model_notices
from lumen.timeutils import utcnow

logger = logging.getLogger(__name__)

oauth_bp = Blueprint("oauth", __name__)

# Module constants rather than config keys: these are protocol timings, not
# deployment tuning knobs, and config.yaml entries would need hot-reload and
# helm-values plumbing that a fixed security parameter does not warrant.
DEVICE_EXPIRY = 600
POLL_INTERVAL = 5
AUTHORIZE_EXPIRY = 600
AUTH_CODE_TTL = 60
CLAIM_WINDOW = 60
CLEANUP_GRACE = 300

_LABEL_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
_VERIFIER_RE = re.compile(r"[A-Za-z0-9_-]{43,128}")
_TTL_RE = re.compile(r"[0-9]{1,10}")
PROJECT_MAX_LENGTH = 256
# Ambiguity-free alphabet: no I/O/0/1/L that users mistype from a terminal.
_USER_CODE_ALPHABET = "BCDFGHJKMPQRTVWXY23456789"


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _gen_user_code() -> str:
    rand = secrets.SystemRandom()
    chars = "".join(rand.choice(_USER_CODE_ALPHABET) for _ in range(8))
    return f"{chars[:4]}-{chars[4:]}"


def _normalize_user_code(raw: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()
    if len(compact) != 8:
        return ""
    return f"{compact[:4]}-{compact[4:]}"


def _parse_ttl(raw: str | None) -> int | None:
    """A key TTL in seconds from a request or form value; None when absent.

    Raises ValueError when it is not a whole number of seconds or not allowed
    under the api.keys.ttl cap (resolve_key_ttl).
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if not _TTL_RE.fullmatch(raw):
        raise ValueError("ttl must be a whole number of seconds, 0 or more")
    return resolve_key_ttl(int(raw))


def _parse_project(raw: str | None) -> str | None:
    """The raw ``project`` request value (a project UUID or name); None when absent.

    Only the format is checked here: the value is resolved once a user is
    logged in, so an anonymous request cannot probe which projects exist.
    Raises ValueError when it is too long.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if len(raw) > PROJECT_MAX_LENGTH:
        raise ValueError(f"project must be at most {PROJECT_MAX_LENGTH} characters")
    return raw


def _resolve_project(raw: str) -> tuple[Entity | None, bool]:
    """The project named by ``raw``, and whether the name was ambiguous.

    A value that parses as a UUID matches ``Entity.uuid`` only; any other value
    matches the name of project entities. Names are unique only by app check,
    so a name that matches more than one project resolves to nothing.
    """
    try:
        cond = Entity.uuid == UUID(raw)
    except ValueError:
        cond = Entity.name == raw
    matches = db.session.execute(
        select(Entity).where(Entity.entity_type == "project", cond).limit(2)
    ).scalars().all()
    if len(matches) > 1:
        return None, True
    return (matches[0] if matches else None), False


def _access_denial(requested_project: str, ambiguous: bool = False) -> str:
    # Built only from the value the requester sent, never from the project row,
    # so the text reveals nothing the requester did not already know.
    reason = (f"the approving account is not a member of project '{requested_project}', "
              "or the project does not exist or is inactive")
    return reason + "; use the project UUID" if ambiguous else reason


def _project_key_denial(entity_id: int, project: Entity | None, requested_project: str,
                        requested_name: str, lock: bool = False) -> str | None:
    """Why ``entity_id`` may not get a key named ``requested_name`` in ``project``; None if allowed.

    The project must exist and be active, and the approver must be a member
    with any role; admin status gives no exception. A member with the user
    role (who is not the owner) can hold one active normal key they created in
    the project: another name is refused, the same name goes on to the
    overwrite flow. With ``lock``, the approver's membership row is locked
    first (as in create_project_key) so two concurrent claims cannot both pass
    the one-key check; the lock is a no-op on SQLite.
    """
    if project is None:
        return _access_denial(requested_project)
    stmt = select(EntityManager.role).filter_by(user_entity_id=entity_id, project_entity_id=project.id)
    if lock:
        stmt = stmt.with_for_update()
    role = db.session.scalar(stmt)
    if lock:
        # Owner and active flag as of after the lock, not an earlier read.
        db.session.refresh(project)
    if role is None or not project.active:
        return _access_denial(requested_project)
    if role == "user" and project.owner_entity_id != entity_id:
        other_key = db.session.scalar(
            select(APIKey.id).where(
                APIKey.entity_id == project.id,
                APIKey.created_by_entity_id == entity_id,
                APIKey.key_type == KEY_TYPE_NORMAL,
                APIKey.revoked_at.is_(None),
                APIKey.name != requested_name,
            ).limit(1)
        )
        if other_key is not None:
            return (f"members with the user role can hold one key in project '{requested_project}'; "
                    "request it with the name of your existing key to replace it")
    return None


def _check_project_request(entity_id: int, requested_project: str,
                           requested_name: str) -> tuple[Entity | None, str | None]:
    """Resolve a requested project and apply the access rule: (project, denial reason)."""
    project, ambiguous = _resolve_project(requested_project)
    if ambiguous:
        return None, _access_denial(requested_project, ambiguous=True)
    reason = _project_key_denial(entity_id, project, requested_project, requested_name)
    return (None, reason) if reason else (project, None)


def _deny_request(req: AuthRequest, reason: str, from_status: str = "pending") -> bool:
    """Set a request to denied with its reason in one guarded update; commits. False if it moved on."""
    result = db.session.execute(
        update(AuthRequest)
        .where(AuthRequest.id == req.id, AuthRequest.status == from_status)
        .values(status="denied", denial_reason=reason)
    )
    if result.rowcount == 0:
        db.session.rollback()
        return False
    db.session.commit()
    return True


def _oauth_error(error: str, description: str = "", status: HTTPStatus = HTTPStatus.BAD_REQUEST):
    body = {"error": error}
    if description:
        body["error_description"] = description
    resp = jsonify(body)
    resp.status_code = status
    return _no_store(resp)


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Pragma"] = "no-cache"
    return resp


def _valid_redirect_uri(uri: str) -> bool:
    """https, or http only to a loopback host; no fragment, userinfo, or bloat."""
    if not uri or len(uri) > 2048:
        return False
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if not parts.scheme or not parts.hostname:
        return False
    if parts.fragment or parts.username or parts.password:
        return False
    host = parts.hostname.lower()
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and host in ("localhost", "127.0.0.1", "::1")


def cleanup_expired_auth_requests():
    """Delete auth requests older than their expiry plus a grace window.

    Single rule, any status: an approved-but-never-claimed request expires
    without a key ever having been minted, and a claimed row only matters for
    authorization-code replay detection during the (far shorter) code lifetime.
    Always commits, so any pending changes on the session are committed too.
    """
    cutoff = utcnow() - timedelta(seconds=CLEANUP_GRACE)
    deleted = db.session.execute(
        delete(AuthRequest).where(AuthRequest.expires_at < cutoff)
    ).rowcount
    db.session.commit()
    return deleted


def start_auth_request_janitor(app):
    """Background daemon that runs the cleanup rule every ~5 minutes."""

    def run():
        while True:
            time.sleep(300)
            try:
                with app.app_context():
                    cleanup_expired_auth_requests()
            except Exception:
                logger.exception("auth-request janitor error")

    t = threading.Thread(target=run, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Device flow
# ---------------------------------------------------------------------------

@oauth_bp.route("/oauth/device_authorization", methods=["POST"])
# Coarse flood guard keyed by IP only: there is no user identity yet at this
# endpoint, and behind ProxyFix-less ingress all users share one bucket, so it
# must be generous; real abuse control stays in slow_down + short lifetimes.
@limiter.limit("500 per minute")
def device_authorization():
    client_id = (request.form.get("client_id") or "").strip()
    name = (request.form.get("name") or "").strip()
    author = (request.form.get("author") or "").strip() or None
    if not _LABEL_RE.fullmatch(client_id) or not _LABEL_RE.fullmatch(name):
        return _oauth_error("invalid_request", "client_id and name are required, matching [A-Za-z0-9._-]{1,128}")
    if author and (len(author) > 128 or not re.fullmatch(r".{1,128}", author)):
        return _oauth_error("invalid_request", "author must be at most 128 characters")
    try:
        ttl = _parse_ttl(request.form.get("ttl"))
        requested_project = _parse_project(request.form.get("project"))
    except ValueError as e:
        return _oauth_error("invalid_request", str(e))

    device_code = secrets.token_urlsafe(32)
    req = None
    for _ in range(5):
        candidate = AuthRequest(
            flow="device",
            client_id=client_id,
            requested_name=name,
            author=author,
            ttl=ttl,
            requested_project=requested_project,
            expires_at=utcnow() + timedelta(seconds=DEVICE_EXPIRY),
            device_code_hash=_token_hash(device_code),
            user_code=_gen_user_code(),
        )
        db.session.add(candidate)
        try:
            db.session.commit()
            req = candidate
            break
        except IntegrityError:
            # user_code (or, absurdly rarely, the code hash) collided; retry.
            db.session.rollback()
    if req is None:
        return _oauth_error("server_error", "could not allocate a unique user code",
                            HTTPStatus.SERVICE_UNAVAILABLE)

    cleanup_expired_auth_requests()

    verification_uri = url_for("oauth.device_page", _external=True)
    return _no_store(jsonify({
        "device_code": device_code,
        "user_code": req.user_code,
        "verification_uri": verification_uri,
        "verification_uri_complete": f"{verification_uri}?code={req.user_code}",
        "expires_in": DEVICE_EXPIRY,
        "interval": POLL_INTERVAL,
    })), HTTPStatus.OK


@oauth_bp.route("/device")
@limiter.limit(_chat_limit, key_func=_chat_entity_id)
def device_page():
    if not g.get("entity_id"):
        # Stash where we came from so the login round trip lands back here;
        # auth.callback / auth.devlogin / landing honour the fixed destination.
        code = request.args.get("code")
        session["oauth_return_to"] = (
            url_for("oauth.device_page", code=code) if code else url_for("oauth.device_page")
        )
        return redirect(url_for("auth.landing"))
    if not request.args.get("code"):
        return render_template("device_code_entry.html", hide_nav=True)

    code = _normalize_user_code(request.args.get("code", ""))
    req = db.session.execute(
        select(AuthRequest).where(
            AuthRequest.user_code == code,
            AuthRequest.flow == "device",
            AuthRequest.status == "pending",
            AuthRequest.expires_at > utcnow(),
        )
    ).scalar_one_or_none() if code else None
    if req is None:
        return redirect(url_for("oauth.error_page", reason="unknown_code"))
    project = None
    if req.requested_project:
        project, reason = _check_project_request(g.entity_id, req.requested_project, req.requested_name)
        if reason:
            _deny_request(req, reason)
            return _forbidden_page(reason)
    return _render_consent(req, project=project)


# ---------------------------------------------------------------------------
# Authorization code + PKCE
# ---------------------------------------------------------------------------

@oauth_bp.route("/oauth/authorize")
@limiter.limit(_chat_limit, key_func=_chat_entity_id)
def authorize():
    error = None
    client_id = (request.args.get("client_id") or "").strip()
    name = (request.args.get("name") or "").strip()
    author = (request.args.get("author") or "").strip() or None
    redirect_uri = request.args.get("redirect_uri") or ""
    state = request.args.get("state") or ""
    challenge = request.args.get("code_challenge") or ""
    method = request.args.get("code_challenge_method") or ""
    ttl = None
    requested_project = None

    if request.args.get("response_type") != "code":
        error = "response_type must be 'code'"
    elif not _LABEL_RE.fullmatch(client_id) or not _LABEL_RE.fullmatch(name):
        error = "client_id and name are required, matching [A-Za-z0-9._-]{1,128}"
    elif author and len(author) > 128:
        error = "author must be at most 128 characters"
    elif not _valid_redirect_uri(redirect_uri):
        # redirect_uri is untrusted (no client registry yet) and errors arrive
        # before the user has seen any destination, so never redirect on error.
        error = "redirect_uri must be an absolute https URL (or http to localhost) without a fragment or credentials"
    elif method != "S256":
        error = "code_challenge_method must be S256"
    elif not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", challenge):
        error = "code_challenge must be 43-128 unreserved characters"
    elif not state or len(state) > 512:
        error = "state is required (at most 512 characters)"
    else:
        try:
            ttl = _parse_ttl(request.args.get("ttl"))
            requested_project = _parse_project(request.args.get("project"))
        except ValueError as e:
            error = str(e)
    if error:
        return _error_page("Invalid authorization request", error)

    if not g.get("entity_id"):
        session["oauth_return_to"] = request.full_path
        return redirect(url_for("auth.landing"))

    project = None
    if requested_project:
        # Refused before any row exists and without a redirect, like the other
        # errors before consent.
        project, reason = _check_project_request(g.entity_id, requested_project, name)
        if reason:
            return _forbidden_page(reason)

    req = AuthRequest(
        flow="code",
        client_id=client_id,
        requested_name=name,
        author=author,
        ttl=ttl,
        requested_project=requested_project,
        expires_at=utcnow() + timedelta(seconds=AUTHORIZE_EXPIRY),
        redirect_uri=redirect_uri,
        code_challenge=challenge,
    )
    db.session.add(req)
    db.session.commit()
    # Keyed by the request's public UUID so a later flow in the same browser never hands its
    # state to an earlier one.
    session["oauth_consent_state"] = {"request_id": str(req.uuid), "state": state}
    return _render_consent(req, project=project)


@oauth_bp.route("/oauth/consent", methods=["POST"])
@limiter.limit(_chat_limit, key_func=_chat_entity_id)
@login_required
def consent():
    entity_id = g.entity_id
    try:
        request_uuid = UUID(request.form.get("request_id", ""))
    except ValueError:
        request_uuid = None
    # Approve/deny only a request whose consent page was rendered to *this*
    # session — ids cannot be burned blind by a forged form.
    if request_uuid is None or str(request_uuid) != session.get("oauth_consent_request_id"):
        return _error_page("Unknown request", "This key request was not shown to your session.")
    action = request.form.get("action")

    req = db.session.execute(
        select(AuthRequest).where(AuthRequest.uuid == request_uuid)
    ).scalar_one_or_none()
    if req is None or req.status != "pending":
        return _result_page("Already handled",
                            "This request was already approved, denied, or has expired.")

    if action == "approve":
        project = None
        if req.requested_project:
            # Again here: membership or keys can change while the page is open.
            project, reason = _check_project_request(entity_id, req.requested_project, req.requested_name)
            if reason:
                _deny_request(req, reason)
                return _forbidden_page(reason)
        overwrite = request.form.get("overwrite") == "on"
        existing = db.session.execute(
            _existing_keys_stmt(entity_id, project, req.requested_name)
        ).scalars().all()
        if existing and not overwrite:
            return _render_consent(req, error='Tick "Overwrite key" to replace your existing key.', project=project)
        # The approver's choice from the dropdown, checked again here; a form
        # without one keeps what the request asked for (or the cap).
        try:
            ttl = _parse_ttl(request.form.get("ttl"))
            ttl = resolve_key_ttl(req.ttl if ttl is None else ttl)
        except ValueError as e:
            return _render_consent(req, error=f"Invalid expiry: {e}.", project=project)

        now = utcnow()
        extended = now + timedelta(seconds=CLAIM_WINDOW)
        # Cutoff recomputed immediately before the mutation, and the expiry
        # extension is done in dialect-portable SQL so the guarded transition
        # stays atomic (SQLite tests, Postgres prod).
        result = db.session.execute(
            update(AuthRequest)
            .where(
                AuthRequest.id == req.id,
                AuthRequest.status == "pending",
                AuthRequest.expires_at > now,
            )
            .values(
                status="approved",
                entity_id=entity_id,
                project_entity_id=project.id if project is not None else None,
                overwrite=overwrite,
                ttl=ttl,
                approved_at=now,
                expires_at=case(
                    (AuthRequest.expires_at < extended, extended),
                    else_=AuthRequest.expires_at,
                ),
            )
        )
        if result.rowcount == 0:
            db.session.rollback()
            return _result_page("Already handled",
                                "This request was already approved, denied, or has expired.")

        if req.flow == "code":
            auth_code = secrets.token_urlsafe(32)
            req.auth_code_hash = _token_hash(auth_code)
            req.auth_code_expires_at = now + timedelta(seconds=AUTH_CODE_TTL)
            db.session.commit()
            query = {"code": auth_code}
            state = _pop_consent_state(req.uuid)
            if state:
                query["state"] = state
            return _append_query_redirect(req.redirect_uri, query)

        db.session.commit()
        return _result_page(
            "Key approved",
            f"Key approved for {session.get('entity_email', 'your account')}. "
            "Return to your terminal to finish.",
        )

    # Deny
    result = db.session.execute(
        update(AuthRequest)
        .where(AuthRequest.id == req.id, AuthRequest.status == "pending")
        .values(status="denied")
    )
    if result.rowcount == 0:
        db.session.rollback()
        return _result_page("Already handled",
                            "This request was already approved, denied, or has expired.")
    db.session.commit()
    if req.flow == "code":
        query = {"error": "access_denied"}
        state = _pop_consent_state(req.uuid)
        if state:
            query["state"] = state
        return _append_query_redirect(req.redirect_uri, query)
    return _result_page("Request denied", "No key was created.")


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------

@oauth_bp.route("/oauth/token", methods=["POST", "OPTIONS"])
# Coarse flood guard keyed by IP only; per-request pacing is already enforced
# per device code via slow_down, so a shared NAT only needs headroom.
@limiter.limit("500 per minute")
def token():
    if request.method == "OPTIONS":
        resp = ("", HTTPStatus.NO_CONTENT)
        return _cors(resp)
    grant_type = request.form.get("grant_type") or ""
    if grant_type == "urn:ietf:params:oauth:grant-type:device_code":
        resp = _token_device()
    elif grant_type == "authorization_code":
        resp = _token_code()
    else:
        resp = _oauth_error("unsupported_grant_type", "unsupported grant_type")
    return _cors(resp)


def _cors(resp):
    """Allow browser pages on any origin to poll /oauth/token.

    Safe because the endpoint carries no cookies or session: everything it
    needs is in the POST body, and a cross-origin reader gets nothing more
    than the key its own flow was approved for.
    """
    resp = make_response(resp)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def _token_device():
    device_code = request.form.get("device_code") or ""
    client_id = request.form.get("client_id") or ""
    req = db.session.execute(
        select(AuthRequest).where(AuthRequest.device_code_hash == _token_hash(device_code))
    ).scalar_one_or_none() if device_code else None
    if req is None or req.client_id != client_id:
        return _oauth_error("invalid_grant", "unknown device_code or client_id mismatch")

    now = utcnow()
    if req.last_polled_at and now - req.last_polled_at < timedelta(seconds=POLL_INTERVAL):
        return _oauth_error("slow_down", "polling too fast")

    if req.status == "pending":
        req.last_polled_at = now
        db.session.commit()
        if req.expires_at <= now:
            return _oauth_error("expired_token", "the request expired")
        return _oauth_error("authorization_pending", "waiting for user approval")
    if req.status == "approved":
        req.last_polled_at = now
        if req.expires_at <= now:
            db.session.commit()
            return _oauth_error("expired_token", "the claim window expired")
        return _mint(req)
    if req.status == "denied":
        return _oauth_error("access_denied", req.denial_reason or "the user denied the request")
    return _oauth_error("invalid_grant", "already claimed")


def _token_code():
    code = request.form.get("code") or ""
    redirect_uri = request.form.get("redirect_uri") or ""
    client_id = request.form.get("client_id") or ""
    verifier = request.form.get("code_verifier") or ""
    req = db.session.execute(
        select(AuthRequest).where(AuthRequest.auth_code_hash == _token_hash(code))
    ).scalar_one_or_none() if code else None
    if (
        req is None
        or req.client_id != client_id
        or req.redirect_uri != redirect_uri
        or not _VERIFIER_RE.fullmatch(verifier)
        or not _pkce_s256_matches(verifier, req.code_challenge or "")
    ):
        return _oauth_error("invalid_grant")

    now = utcnow()
    if req.status == "claimed":
        # RFC 6749 §4.1.2: a redeemed authorization code presented twice means
        # the code leaked; revoke the key minted from it. Keys are soft deleted
        # so their usage history is kept.
        if req.api_key_id is not None:
            key = db.session.get(APIKey, req.api_key_id)
            if key is not None and key.revoked_at is None:
                key.revoked_at = now
                db.session.commit()
        return _oauth_error("invalid_grant", "authorization code replay detected; key revoked")
    if req.status != "approved":
        return _oauth_error("invalid_grant")
    if req.auth_code_expires_at is None or req.auth_code_expires_at <= now:
        return _oauth_error("invalid_grant", "authorization code expired")
    return _mint(req)


def _pkce_s256_matches(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return hmac.compare_digest(computed, challenge)


def _mint(req: AuthRequest):
    """Atomically claim an approved request and mint the key (returned once)."""
    project = None
    if req.requested_project:
        # Third check, before claiming, under a lock on the approver's
        # membership row. The project is the one resolved at approval; if it
        # was deleted since, the request row went with it (ON DELETE CASCADE).
        project = db.session.get(Entity, req.project_entity_id) if req.project_entity_id else None
        reason = _project_key_denial(req.entity_id, project, req.requested_project,
                                     req.requested_name, lock=True)
        if reason:
            if not _deny_request(req, reason, from_status="approved"):
                return _oauth_error("invalid_grant", "not approved (anymore)")
            return _oauth_error("access_denied", reason)
    key_entity_id = project.id if project is not None else req.entity_id

    result = db.session.execute(
        update(AuthRequest)
        .where(AuthRequest.id == req.id, AuthRequest.status == "approved")
        .values(status="claimed")
    )
    if result.rowcount == 0:
        db.session.rollback()
        return _oauth_error("invalid_grant", "not approved (anymore)")

    # Only normal keys can be overwritten: OAuth never touches an admin or
    # monitoring key, even one with the requested name.
    existing = db.session.execute(
        _existing_keys_stmt(req.entity_id, project, req.requested_name)
    ).scalars().all()
    if existing and not req.overwrite:
        db.session.rollback()
        return _oauth_error("invalid_grant", "key name now in use, run again")

    key = "sk_" + secrets.token_urlsafe(32)
    provenance = {"client_id": req.client_id, "requested_by": req.author}
    # Both paths count the approved TTL from now; the cap for a row approved before ttl existed.
    expires_at = key_expires_at(resolve_key_ttl() if req.ttl is None else req.ttl)
    # Overwrite refreshes the newest key with this name in place (the one the
    # consent page showed), keeping its id and usage stats. If it was revoked
    # meanwhile, mint a new row instead. Unlike rotate_key_secret, no
    # IntegrityError handling: a fresh token_urlsafe(32) secret cannot collide.
    target, duplicates = (existing[0], existing[1:]) if existing else (None, [])
    if target is not None and set_key_secret(target.id, key, expires_at=expires_at, **provenance):
        req.api_key_id = target.id
    else:
        api_key = APIKey(
            entity_id=key_entity_id,
            created_by_entity_id=req.entity_id,
            key_type=KEY_TYPE_NORMAL,
            name=req.requested_name,
            **key_secret_fields(key),
            **provenance,
            expires_at=expires_at,
        )
        db.session.add(api_key)
        db.session.flush()
        req.api_key_id = api_key.id
    # Manual duplicates with the same name are revoked, leaving one active key.
    for stale in duplicates:
        stale.revoked_at = utcnow()
        stale.revoked_by_entity_id = req.entity_id
    approver_email = db.session.execute(
        select(Entity.email).where(Entity.id == req.entity_id)
    ).scalar_one_or_none()
    db.session.commit()
    # approved_by tells the CLI whose account approved, closing the "someone
    # else approved my request with their account" confusion described in the
    # security model; it is sent only to the holder of the one-time secret.
    return _no_store(jsonify({
        "access_token": key,
        "token_type": "Bearer",
        "approved_by": approver_email,
        "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ") if expires_at else None,
    })), HTTPStatus.OK


# ---------------------------------------------------------------------------
# Consent page rendering
# ---------------------------------------------------------------------------

def _consent_model_rows(entity_id: int):
    """All active, non-blocked models with their acknowledgement state.

    Same visibility rule as the Models page (``bulk_model_access_statuses`` minus
    blocked) and the same accepted-state semantics as model_detail.html, so the
    ack dialog behaves identically here.
    """
    configs = db.session.execute(
        select(ModelConfig).where(ModelConfig.active).order_by(ModelConfig.model_name)
    ).scalars().all()
    if not configs:
        return []
    statuses = bulk_model_access_statuses(entity_id, [c.id for c in configs])
    consent_rows = {
        r.model_config_id: r
        for r in db.session.execute(
            select(EntityModelConsent).where(EntityModelConsent.entity_id == entity_id)
        ).scalars().all()
    }
    rows = []
    for c in configs:
        if statuses.get(c.id, "allowed") == "blocked":
            continue
        notice, early_notice = model_notices(c)
        row = consent_rows.get(c.id)
        accepted = _consent_satisfied(row, c)
        accepted_at = None
        if accepted and row is not None:
            times = [t for t in (row.consented_at, row.early_access_at) if t is not None]
            accepted_at = max(times) if times else None
        rows.append({
            "name": c.model_name,
            "needs_ack": bool(c.needs_ack),
            "early_access": bool(c.early_access),
            "requires_action": (c.needs_ack or c.early_access) and not accepted,
            "accepted": accepted,
            "accepted_at": accepted_at,
            "notice": notice,
            "early_notice": early_notice,
        })
    return rows


def _pop_consent_state(request_uuid: UUID) -> str | None:
    """Pop the code-flow state stored by authorize(), only if it belongs to request_uuid."""
    saved = session.pop("oauth_consent_state", None)
    if isinstance(saved, dict) and saved.get("request_id") == str(request_uuid):
        return saved.get("state")
    return None


def _existing_keys_stmt(entity_id: int, project: Entity | None, name: str):
    """Active normal keys named ``name`` that an OAuth approval may overwrite, newest first.

    Personal requests: the approver's own keys. Project requests: keys in the
    project that the approver created, never a teammate's same-name key. Only
    normal keys: OAuth never touches an admin or monitoring key.
    """
    stmt = select(APIKey).where(APIKey.name == name, APIKey.key_type == KEY_TYPE_NORMAL,
                                APIKey.revoked_at.is_(None))
    if project is None:
        stmt = stmt.where(APIKey.entity_id == entity_id)
    else:
        stmt = stmt.where(APIKey.entity_id == project.id, APIKey.created_by_entity_id == entity_id)
    return stmt.order_by(APIKey.created_at.desc(), APIKey.id.desc())


def _render_consent(req: AuthRequest, error: str | None = None, project: Entity | None = None):
    entity_id = g.entity_id
    existing_key = db.session.execute(
        _existing_keys_stmt(entity_id, project, req.requested_name)
    ).scalars().first()
    session["oauth_consent_request_id"] = str(req.uuid)
    origin = full_uri = ""
    if req.flow == "code" and req.redirect_uri:
        parts = urlsplit(req.redirect_uri)
        origin = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
        full_uri = req.redirect_uri
    return render_template(
        "oauth_consent.html",
        hide_nav=True,
        req=req,
        error=error,
        existing_key=existing_key,
        key_ttl_choices=key_ttl_choices(req.ttl),
        model_rows=_consent_model_rows(project.id if project is not None else entity_id),
        project=project,
        # project_consent() only lets owners and managers acknowledge for the project.
        can_ack=project is None or get_project_role(entity_id, project.id) in ("owner", "manager"),
        # The consent route's URL with the model name cut off; the page appends it.
        project_consent_base=(url_for("projects.project_consent", sid=project.uuid, model_name="_")[:-1]
                              if project is not None else None),
        destination_origin=origin,
        destination_uri=full_uri,
        verified_client=False,
    )


def _result_page(title: str, message: str):
    return _no_store(make_response(
        render_template("oauth_error.html", hide_nav=True,
                        error_title=title, error_message=message),
        HTTPStatus.OK,
    ))


def _error_page(title: str, message: str):
    return render_template("oauth_error.html", hide_nav=True,
                           error_title=title, error_message=message), HTTPStatus.BAD_REQUEST


def _forbidden_page(reason: str):
    return _no_store(make_response(
        render_template("oauth_error.html", hide_nav=True,
                        error_title="Project key not allowed", error_message=reason),
        HTTPStatus.FORBIDDEN,
    ))


@oauth_bp.route("/device/error")
def error_page():
    reasons = {
        "unknown_code": ("Unknown code", "No pending request matches that code. Start the request again in your application."),
        "already_handled": ("Already handled", "This request was already approved, denied, or has expired."),
    }
    title, message = reasons.get(request.args.get("reason") or "", reasons["unknown_code"])
    return render_template("oauth_error.html", hide_nav=True,
                           error_title=title, error_message=message), HTTPStatus.BAD_REQUEST


def _append_query_redirect(uri: str, query: dict):
    parts = urlsplit(uri)
    # Keep the existing query verbatim (decoded then re-encoded via
    # parse_qsl + urlencode over a list of pairs, so percent-encoded values
    # survive once, bare flags keep their emptiness, and repeated keys are
    # preserved) instead of naively splitting and double-encoding it.
    merged = parse_qsl(parts.query, keep_blank_values=True)
    merged += list(query.items())
    return redirect(urlunsplit((parts.scheme, parts.netloc, parts.path,
                                urlencode(merged), ""))), HTTPStatus.FOUND
