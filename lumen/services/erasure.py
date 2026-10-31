"""GDPR erasure: the shared ghost entity that erased users are merged into.

An erased user's request history, usage stats, models and revoked API keys
move to one ghost entity, so totals stay complete while nothing points at the
person any more. The ghost has no email, is inactive, and has no memberships,
balance or limits; every user listing filters on entity_type = 'user', so it
never appears next to real users.
"""

import secrets

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.models.auth_request import AuthRequest
from lumen.models.conversation import Conversation
from lumen.models.entity import Entity
from lumen.models.entity_balance import EntityBalance
from lumen.models.entity_limit import EntityLimit
from lumen.models.entity_manager import EntityManager
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.entity_stat import EntityStat
from lumen.models.group import Group
from lumen.models.group_member import GroupMember
from lumen.models.message import Message
from lumen.models.model_stat import ModelStat
from lumen.timeutils import utcnow

GHOST_NAME = "Erased user"
GHOST_KEY_NAME = "ghost key"


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


# Key and bookkeeping columns of the stats tables; every other column is a
# counter that a merge adds up. Derived from the table rather than listed, so a
# counter added later is merged too instead of silently dropped. This assumes
# model_stats and entity_stats hold only summable counters besides these: a new
# non-counter column (a label, a min or max) must be added to this set.
_STAT_KEY_COLUMNS = frozenset({"id", "entity_id", "model_config_id", "source", "last_used_at"})


def _counter_columns(model):
    return [c.key for c in model.__table__.columns if c.key not in _STAT_KEY_COLUMNS]


def _merge_row(dst, src, counters):
    for name in counters:
        setattr(dst, name, (getattr(dst, name) or 0) + (getattr(src, name) or 0))
    if src.last_used_at and (not dst.last_used_at or src.last_used_at > dst.last_used_at):
        dst.last_used_at = src.last_used_at


def merge_stats_into(ghost_id, user_id):
    """Merge the user's model_stats and entity_stats rows into the ghost's.

    A model_stats row the ghost already has for the same (model, source) gets the
    user's counters added and the latest last_used_at; the user's row is deleted.
    A row the ghost has no counterpart for is re-pointed instead. entity_stats is
    one row per entity and is merged the same way, conversations included.
    Returns ``(model_stats moved, model_stats merged, entity_stats merged)``.
    Does not commit.
    """
    counters = _counter_columns(ModelStat)
    ghost_rows = {
        (s.model_config_id, s.source): s
        for s in db.session.execute(select(ModelStat).filter_by(entity_id=ghost_id)).scalars()
    }
    moved = merged = 0
    for stat in db.session.execute(select(ModelStat).filter_by(entity_id=user_id)).scalars().all():
        dst = ghost_rows.get((stat.model_config_id, stat.source))
        if dst is None:
            stat.entity_id = ghost_id
            moved += 1
        else:
            _merge_row(dst, stat, counters)
            db.session.delete(stat)
            merged += 1

    entity_merged = 0
    user_stat = db.session.get(EntityStat, user_id)
    if user_stat is not None:
        ghost_stat = db.session.get(EntityStat, ghost_id)
        if ghost_stat is None:
            ghost_stat = EntityStat(entity_id=ghost_id, **{c: 0 for c in _counter_columns(EntityStat)})
            db.session.add(ghost_stat)
        _merge_row(ghost_stat, user_stat, _counter_columns(EntityStat))
        db.session.delete(user_stat)
        entity_merged = 1
    db.session.flush()
    return moved, merged, entity_merged


def _ghost_keys(user_id, ghost_id):
    """Revoke the user's API keys, strip what identifies them, and move them to the ghost.

    key_hash is hash_api_key(raw key): an HMAC-SHA256 of the raw key keyed on the
    server's ENCRYPTION_KEY. Through the API a revoked key already answers 401 like an
    unknown one, but anyone holding that secret (the server, or an operator with a
    database dump and the secret) could still hash the user's old raw key and find
    the row, linking it to the ghost. Each key therefore gets a fresh random 64-hex
    value instead: the column stays unique and the same shape, and no raw key under
    any secret matches it.

    Kept on purpose: the usage counters, created_at, last_used_at and expires_at, the
    key's share of the history the ghost now holds. An already-revoked key keeps its
    original revoked_at. Other entities' keys only lose created_by_entity_id /
    revoked_by_entity_id where those point at the user (project keys the user created
    or revoked). Returns the number of the user's keys moved. Does not commit.
    """
    now = utcnow()
    keys = db.session.execute(select(APIKey).filter_by(entity_id=user_id)).scalars().all()
    for key in keys:
        key.entity_id = ghost_id
        key.revoked_at = key.revoked_at or now
        key.revoked_by_entity_id = None
        key.name = GHOST_KEY_NAME
        key.key_hint = None
        key.key_hash = secrets.token_hex(32)
        key.client_id = None
        key.requested_by = None
        key.created_by_entity_id = None
    db.session.flush()
    db.session.execute(update(APIKey).where(APIKey.created_by_entity_id == user_id)
                       .values(created_by_entity_id=None))
    db.session.execute(update(APIKey).where(APIKey.revoked_by_entity_id == user_id)
                       .values(revoked_by_entity_id=None))
    return len(keys)


def groups_losing_owner(user_id):
    """Names of the groups the user owns, which erasure leaves without an owner."""
    return db.session.execute(
        select(Group.name).join(GroupMember, GroupMember.group_id == Group.id)
        .where(GroupMember.entity_id == user_id, GroupMember.is_owner.is_(True))
        .order_by(Group.name)
    ).scalars().all()


def _erase_dependents(user_id):
    """Delete the user's personal data, children first, one statement per table.

    Explicit rather than left to ON DELETE CASCADE, so SQLite (which never enforces
    foreign keys here) erases exactly what PostgreSQL does, and the counts are the
    operator's record of the erasure. Group memberships go too, owner rows included:
    a group the user owned survives with no owner (get_group_owner returns None) until
    an admin sets one. Returns ``(counts by table, names of the groups left without an
    owner)``. Does not commit.
    """
    orphaned = groups_losing_owner(user_id)
    conversations = select(Conversation.id).where(Conversation.entity_id == user_id)
    statements = [
        ("messages", delete(Message).where(Message.conversation_id.in_(conversations))),
        ("conversations", delete(Conversation).where(Conversation.entity_id == user_id)),
        ("auth_requests", delete(AuthRequest).where(AuthRequest.entity_id == user_id)),
        ("entity_balances", delete(EntityBalance).where(EntityBalance.entity_id == user_id)),
        ("entity_limits", delete(EntityLimit).where(EntityLimit.entity_id == user_id)),
        ("entity_model_consents", delete(EntityModelConsent).where(EntityModelConsent.entity_id == user_id)),
        ("group_members", delete(GroupMember).where(GroupMember.entity_id == user_id)),
        ("entity_managers", delete(EntityManager).where(EntityManager.user_entity_id == user_id)),
    ]
    counts = {
        table: db.session.execute(stmt.execution_options(synchronize_session=False)).rowcount
        for table, stmt in statements
    }
    return counts, orphaned
