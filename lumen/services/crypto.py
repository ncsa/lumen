import base64
import hashlib
import hmac

from cryptography.fernet import Fernet
from flask import current_app
from sqlalchemy import types

# Prefix marking a value produced by encrypt_secret, so encrypted values are
# distinguishable from legacy plaintext rows that predate encryption at rest.
_ENC_PREFIX = "enc:"


def hash_api_key(key: str) -> str:
    secret = current_app.config["ENCRYPTION_KEY"]
    return hmac.new(secret.encode(), key.encode(), hashlib.sha256).hexdigest()


def _fernet() -> Fernet:
    """Fernet keyed from the app's ENCRYPTION_KEY (already mandatory at startup)."""
    secret = current_app.config["ENCRYPTION_KEY"]
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def encrypt_secret(value: str) -> str:
    """Encrypt a secret for at-rest storage; reversible via decrypt_secret."""
    return _ENC_PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    """Decrypt a value produced by encrypt_secret.

    Un-prefixed values pass through unchanged, so rows written before
    encryption at rest keep working until the migration rewrites them.
    """
    if not value.startswith(_ENC_PREFIX):
        return value
    return _fernet().decrypt(value[len(_ENC_PREFIX):].encode()).decode()


class EncryptedText(types.TypeDecorator):
    """Text column transparently encrypted at rest with the app's ENCRYPTION_KEY.

    Values are encrypted on bind and decrypted on fetch, so ORM code reads and
    writes plaintext while the database only ever stores ciphertext. Requires an
    app context at query time (which every DB call in this codebase already has).
    """

    impl = types.Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_secret(value) if value is not None else None

    def process_result_value(self, value, dialect):
        return decrypt_secret(value) if value is not None else None


def cache_salt_for_entity(entity_id: int) -> str:
    """Derive a stable, per-entity prefix-cache salt.

    Keyed on SECRET_KEY so the salt is unguessable across entities: an attacker
    cannot land in another entity's cache namespace (and probe it via cache-hit
    timing) without knowing that entity's salt. See CHANGELOG / ncsa/lumen#36.
    """
    secret = current_app.config["SECRET_KEY"]
    return hmac.new(secret.encode(), f"entity:{entity_id}".encode(), hashlib.sha256).hexdigest()
