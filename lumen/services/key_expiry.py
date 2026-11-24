"""API key expiry: the TTL limits per key type, and revoking keys once they expire.

Each key type has its own maximum TTL (0 = no limit, which allows "never"):

- normal: ``api.keys.max_ttl`` (the deprecated ``api.keys.ttl`` is its
  fallback); no limit when unset;
- admin: ``api.keys.max_admin_ttl``; one week when unset;
- monitoring: ``api.keys.max_monitor_ttl``; no limit when unset.

A key's default TTL is ``api.keys.default_ttl`` (0 = never), lowered to the
type's maximum when it is larger ("never" counts as larger than any finite
maximum). Without ``default_ttl``, the default is the type's maximum, or
"never" when the type has no limit.

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
from lumen.models.api_key import KEY_TYPE_ADMIN, KEY_TYPE_MONITORING, KEY_TYPE_NORMAL, APIKey
from lumen.timeutils import utcnow

logger = logging.getLogger(__name__)

EXPIRE_INTERVAL = 60
# Upper bound on any TTL, so a typo like 999999999w cannot overflow the expiry datetime.
MAX_KEY_TTL = 100 * 365 * 86400
# Default of api.keys.max_admin_ttl: the longest TTL of an admin key when unset.
ADMIN_KEY_MAX_TTL = 7 * 86400

_TTL_RE = re.compile(r"(0|([0-9]+)([hdw]))")
_TTL_UNIT_SECONDS = {"h": 3600, "d": 86400, "w": 7 * 86400}

# Preset TTLs (seconds, label) offered by the "Expires after" dropdown.
KEY_TTL_PRESETS = [
    (86400, "1 day"),
    (7 * 86400, "7 days"),
    (30 * 86400, "30 days"),
    (90 * 86400, "90 days"),
    (365 * 86400, "1 year"),
]


def parse_key_ttl(value) -> int:
    """Seconds for an ``api.keys`` TTL value: ``0`` or ``<n>h``/``<n>d``/``<n>w``.

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


def _key_ttl_limits(key_type: str) -> tuple[int, bool, int]:
    """``(longest, never_allowed, default)`` TTL in seconds for a key type (0 = never)."""
    if key_type == KEY_TYPE_MONITORING:
        cap = current_app.config.get("KEY_MAX_MONITOR_TTL", 0)
    elif key_type == KEY_TYPE_ADMIN:
        cap = current_app.config.get("KEY_MAX_ADMIN_TTL", ADMIN_KEY_MAX_TTL)
    else:
        cap = current_app.config.get("KEY_TTL", 0)
    default = current_app.config.get("KEY_DEFAULT_TTL")
    if default is None:
        default = cap
    elif cap and (default == 0 or default > cap):
        default = cap
    if cap:
        return cap, False, default
    return MAX_KEY_TTL, True, default


def resolve_key_ttl(requested: Optional[int] = None, key_type: str = KEY_TYPE_NORMAL) -> int:
    """The TTL in seconds (0 = never) for a new or edited key of ``key_type``.

    Nothing requested gives the type's default. Otherwise the request must be
    at most the type's longest TTL, and 0 (never) only where the type allows
    it (see the module docstring). Raises ValueError for a disallowed request.
    """
    longest, never_allowed, default = _key_ttl_limits(key_type)
    if requested is None:
        return default
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 0:
        raise ValueError("ttl must be a whole number of seconds, 0 or more")
    if requested == 0 and not never_allowed:
        kind = "admin keys" if key_type == KEY_TYPE_ADMIN else "keys"
        raise ValueError(f"{kind} must expire: ttl cannot be 0 (never)")
    if requested > longest:
        raise ValueError(f"ttl cannot exceed {longest} seconds")
    return requested


def _ttl_label(seconds: int) -> str:
    """Human label for a TTL that is not a preset, in the largest whole unit."""
    for unit, size in (("year", 365 * 86400), ("week", 7 * 86400), ("day", 86400), ("hour", 3600)):
        if seconds % size == 0:
            n = seconds // size
            return f"{n} {unit}{'' if n == 1 else 's'}"
    return f"{seconds} seconds"


def key_ttl_choices(selected: Optional[int] = None, key_type: str = KEY_TYPE_NORMAL) -> list[tuple[int, str, bool]]:
    """``(seconds, label, selected)`` options for the "Expires after" dropdown.

    Where the type allows "never" (0): every preset plus "Never". Otherwise the
    presets up to the type's longest TTL, plus that longest TTL itself, which
    is always offered. The type's default is pre-selected; a ``selected`` TTL
    the type allows is pre-selected instead, and offered even when it is not
    a preset.
    """
    longest, never_allowed, default = _key_ttl_limits(key_type)
    if never_allowed:
        choices = [(s, label, False) for s, label in KEY_TTL_PRESETS] + [(0, "Never", False)]
    else:
        choices = [(s, label, False) for s, label in KEY_TTL_PRESETS if s <= longest]
        if not any(s == longest for s, _, _ in choices):
            choices.append((longest, _ttl_label(longest), False))
    try:
        selected = default if selected is None else resolve_key_ttl(selected, key_type)
    except ValueError:
        selected = default
    if not any(s == selected for s, _, _ in choices):
        choices.append((selected, _ttl_label(selected), False))
        # Ascending, with "Never" (0) last.
        choices.sort(key=lambda c: c[0] or float("inf"))
    return [(s, label, s == selected) for s, label, _ in choices]


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

    Returns True when the key was overdue as loaded, so the caller must reject it.
    """
    now = utcnow()
    if api_key.revoked_at is not None or api_key.expires_at is None or api_key.expires_at > now:
        return False
    # Guarded like expire_keys on the row as it is now, not as it was loaded: a
    # concurrent manual revoke is never overwritten, and a key an OAuth overwrite
    # refreshed in the meantime (new secret, new expiry) is left alone.
    db.session.execute(
        update(APIKey)
        .where(APIKey.id == api_key.id, APIKey.revoked_at.is_(None), APIKey.expires_at <= now)
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
