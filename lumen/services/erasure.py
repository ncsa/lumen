"""GDPR erasure: the shared ghost entity that erased users are merged into.

An erased user's request history, usage stats, models and revoked API keys
move to one ghost entity, so totals stay complete while nothing points at the
person any more. The ghost has no email, is inactive, and has no memberships,
balance or limits; every user listing filters on entity_type = 'user', so it
never appears next to real users.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from lumen.extensions import db
from lumen.models.entity import Entity

GHOST_NAME = "Erased user"


def get_or_create_ghost() -> Entity:
    """Return the ghost entity, creating and committing it on first use.

    uq_entities_single_ghost allows a single ghost row, so when two callers
    race to create it, the loser's insert fails on the index and it re-reads
    the winner's row.
    """
    stmt = select(Entity).where(Entity.entity_type == "ghost")
    ghost = db.session.execute(stmt).scalar_one_or_none()
    if ghost is not None:
        return ghost
    ghost = Entity(entity_type="ghost", email=None, name=GHOST_NAME, initials="", active=False)
    db.session.add(ghost)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return db.session.execute(stmt).scalar_one()
    return ghost
