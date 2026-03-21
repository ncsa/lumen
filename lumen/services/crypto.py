import hashlib
import hmac

from flask import current_app


def hash_api_key(key: str) -> str:
    secret = current_app.config.get("ENCRYPTION_KEY", "")
    return hmac.new(secret.encode(), key.encode(), hashlib.sha256).hexdigest()
