"""Resolve the opaque public ``uuid`` of a row to the row, or 404.

Every id a user can see is a row's ``uuid`` (see AGENTS.md). Routes take it
with Flask's ``<uuid:...>`` converter, so a malformed or integer value already
404s before the view runs, and resolve it here. The integer primary key stays
server-side for keys and joins. These helpers only find the row: ownership and
membership checks stay in the views.
"""

from uuid import UUID

from sqlalchemy import select

from ..extensions import db
from .api_key import APIKey
from .entity import Entity
from .group import Group


def entity_by_uuid_or_404(u: UUID, entity_type: str) -> Entity:
    """The entity with this uuid and entity_type ('user', 'project' or 'ghost'), else 404."""
    return db.first_or_404(select(Entity).where(Entity.uuid == u, Entity.entity_type == entity_type))


def group_by_uuid_or_404(u: UUID) -> Group:
    """The group with this uuid, else 404."""
    return db.first_or_404(select(Group).where(Group.uuid == u))


def key_by_uuid_or_404(u: UUID) -> APIKey:
    """The API key with this uuid, else 404."""
    return db.first_or_404(select(APIKey).where(APIKey.uuid == u))


# The admin API (/v1/organization) names users and groups with a typed prefix,
# as OpenAI does: "user-<uuid>" and "group-<uuid>", the uuid in dashed form.
USER_ID_PREFIX = "user-"
GROUP_ID_PREFIX = "group-"


def prefixed_id(prefix: str, u: UUID) -> str:
    """The public admin API id for a row's uuid, e.g. ``user-<uuid>``."""
    return f"{prefix}{u}"


def parse_prefixed_id(value: str, prefix: str) -> UUID | None:
    """The uuid in a ``<prefix><uuid>`` id, or None if it is not one.

    Only the canonical lowercase dashed form is accepted, so every row has
    exactly one public id.
    """
    if not value.startswith(prefix):
        return None
    rest = value[len(prefix):]
    try:
        u = UUID(rest)
    except ValueError:
        return None
    return u if str(u) == rest else None


def user_by_prefixed_id(value: str) -> Entity | None:
    """The user (never a project or the ghost) with this ``user-<uuid>`` id, else None."""
    u = parse_prefixed_id(value, USER_ID_PREFIX)
    if u is None:
        return None
    return db.session.execute(
        select(Entity).where(Entity.uuid == u, Entity.entity_type == "user")
    ).scalar_one_or_none()
