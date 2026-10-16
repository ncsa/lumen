"""API key expiry: the ``api.keys.ttl`` cap, and revoking keys once they expire.

An expired key is simply revoked: ``revoked_at`` is set to its ``expires_at``
and ``revoked_by_entity_id`` stays NULL (a system revocation), so every
liveness check keeps testing ``revoked_at`` alone. The background expirer does
this within a minute; ``revoke_if_expired`` closes that gap where a request
must not see an overdue key as live.
"""

import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from flask import current_app
from sqlalchemy import update

from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.timeutils import utcnow

logger = logging.getLogger(__name__)

EXPIRE_INTERVAL = 60
# Upper bound on any TTL, so a typo like 999999999w cannot overflow the expiry datetime.
MAX_KEY_TTL = 100 * 365 * 86400

_TTL_RE = re.compile(r"(0|([0-9]+)([hdw]))")
_TTL_UNIT_SECONDS = {"h": 3600, "d": 86400, "w": 7 * 86400}


def parse_key_ttl(value) -> int:
    """Seconds for an ``api.keys.ttl`` value: ``0`` or ``<n>h``/``<n>d``/``<n>w``.

    Raises ValueError for anything else (garbage, negative, blank, a bare number,
    or more than MAX_KEY_TTL).
    """
    if isinstance(value, bool):
        raise ValueError(f"invalid key TTL {value!r}")
    match = _TTL_RE.fullmatch(str(value).strip()) if isinstance(value, (int, str)) else None
    if not match:
        raise ValueError(f"invalid key TTL {value!r}")
    if match.group(1) == "0":
        return 0
    seconds = int(match.group(2)) * _TTL_UNIT_SECONDS[match.group(3)]
    if seconds > MAX_KEY_TTL:
        raise ValueError(f"key TTL {value!r} exceeds 100 years")
    return seconds


def resolve_key_ttl(requested: Optional[int] = None) -> int:
    """The TTL in seconds (0 = never) for a new or edited key, under the KEY_TTL cap.

    Nothing requested gives the cap (0 = never). With a cap above 0 the request
    must be between 1 second and the cap; with no cap any value from 0 (never)
    up to MAX_KEY_TTL is allowed. Raises ValueError for a disallowed request.
    """
    cap = current_app.config.get("KEY_TTL", 0)
    if requested is None:
        return cap
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 0:
        raise ValueError("ttl must be a whole number of seconds, 0 or more")
    if cap and requested == 0:
        raise ValueError("keys must expire: ttl cannot be 0 (never)")
    if requested > (cap or MAX_KEY_TTL):
        raise ValueError(f"ttl cannot exceed {cap or MAX_KEY_TTL} seconds")
    return requested


def key_expires_at(ttl: int) -> Optional[datetime]:
    """Absolute expiry for a key given its TTL from now; None when ttl is 0 (never)."""
    return utcnow() + timedelta(seconds=ttl) if ttl else None


def expire_keys() -> int:
    """Revoke every live key whose expiry has passed. Idempotent; always commits."""
    revoked = db.session.execute(
        update(APIKey)
        .where(APIKey.revoked_at.is_(None), APIKey.expires_at <= utcnow())
        .values(revoked_at=APIKey.expires_at)
    ).rowcount
    db.session.commit()
    return revoked


def revoke_if_expired(api_key: APIKey) -> bool:
    """Revoke ``api_key`` now if it is live but past its expiry. Commits if it does.

    Returns True when the key was overdue (and is now revoked).
    """
    if api_key.revoked_at is not None or api_key.expires_at is None or api_key.expires_at > utcnow():
        return False
    # Guarded like expire_keys, so a concurrent manual revoke is never overwritten.
    db.session.execute(
        update(APIKey)
        .where(APIKey.id == api_key.id, APIKey.revoked_at.is_(None))
        .values(revoked_at=APIKey.expires_at)
    )
    db.session.commit()
    db.session.refresh(api_key)
    return True


def start_key_expirer(app):
    """Background daemon that revokes expired keys at startup and then every 60 s."""

    def run():
        while True:
            try:
                with app.app_context():
                    expire_keys()
            except Exception:
                logger.exception("key expirer error")
            time.sleep(EXPIRE_INTERVAL)

    t = threading.Thread(target=run, daemon=True)
    t.start()
