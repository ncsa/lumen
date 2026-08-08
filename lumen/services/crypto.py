import hashlib
import hmac

from flask import current_app


def hash_api_key(key: str) -> str:
    secret = current_app.config["ENCRYPTION_KEY"]
    return hmac.new(secret.encode(), key.encode(), hashlib.sha256).hexdigest()


def cache_salt_for_entity(entity_id: int) -> str:
    """Derive a stable, per-entity prefix-cache salt.

    Keyed on SECRET_KEY so the salt is unguessable across entities: an attacker
    cannot land in another entity's cache namespace (and probe it via cache-hit
    timing) without knowing that entity's salt. See CHANGELOG / ncsa/lumen#36.
    """
    secret = current_app.config["SECRET_KEY"]
    return hmac.new(secret.encode(), f"entity:{entity_id}".encode(), hashlib.sha256).hexdigest()
