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
