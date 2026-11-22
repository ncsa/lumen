"""Resolve a bearer token to its API key and owner.

The one place that checks a Lumen API key: ``api_key_required`` (proxy API)
and the admin API decorator both wrap ``resolve_api_key`` and then decide, by
``key_type``, whether the key may reach the route.
"""

from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus

from sqlalchemy import or_, select, update

from lumen.decorators import is_admin_eligible
from lumen.extensions import db
from lumen.models.api_key import KEY_TYPE_NORMAL, APIKey
from lumen.models.entity import Entity
from lumen.services.crypto import hash_api_key
from lumen.services.key_expiry import revoke_if_expired
from lumen.timeutils import utcnow

# Admin and monitoring keys are never billed, so bill_request never touches
# their last_used_at; it is written here instead, at most this often.
LAST_USED_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True)
class KeyRejected:
    """Why a token was refused: the error message and the HTTP status."""

    message: str
    status: HTTPStatus


INVALID_KEY = KeyRejected("Invalid or revoked API key", HTTPStatus.UNAUTHORIZED)
ACCOUNT_DISABLED = KeyRejected("Account disabled", HTTPStatus.FORBIDDEN)


def resolve_api_key(token: str) -> tuple[APIKey, Entity] | KeyRejected:
    """Return ``(api_key, owner)`` for a usable key, else a ``KeyRejected``.

    - unknown, revoked or expired key: 401 (an overdue key is revoked now);
    - admin or monitoring key whose owner is inactive or no longer in the
      ``admins`` list: 401. Admin mode does not apply (there is no session),
      and restoring the owner makes the key work again;
    - normal key of an inactive owner: 403, as before key types existed.

    The caller still has to check that ``key_type`` may use its route.
    """
    row = db.session.execute(
        select(APIKey, Entity).join(Entity, Entity.id == APIKey.entity_id)
        .where(APIKey.key_hash == hash_api_key(token))
    ).one_or_none()
    if not row or row.APIKey.revoked_at is not None or revoke_if_expired(row.APIKey):
        return INVALID_KEY

    api_key, entity = row
    if api_key.key_type != KEY_TYPE_NORMAL:
        if not entity.active or not is_admin_eligible(entity):
            return INVALID_KEY
        _touch_last_used(api_key)
    elif not entity.active:
        return ACCOUNT_DISABLED
    return api_key, entity


def _touch_last_used(api_key: APIKey) -> None:
    """Set ``last_used_at`` to now unless it was set within the last minute."""
    now = utcnow()
    cutoff = now - LAST_USED_INTERVAL
    if api_key.last_used_at is not None and api_key.last_used_at > cutoff:
        return
    # Guarded on the row as it is now, so concurrent requests write it once.
    db.session.execute(
        update(APIKey)
        .where(APIKey.id == api_key.id, or_(APIKey.last_used_at.is_(None), APIKey.last_used_at <= cutoff))
        .values(last_used_at=now)
    )
    db.session.commit()
    db.session.refresh(api_key)
