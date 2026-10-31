"""flask erase-entity on SQLite: refusals, what is deleted, what moves to the ghost,
how the user's keys are anonymised, and that usage totals survive the erasure.

SQLite never enforces foreign keys here, so every check below is on rows the
command itself deleted or moved, exactly as it does on PostgreSQL.
"""
import re
from datetime import datetime
from http import HTTPStatus

import pytest
from flask import template_rendered
from sqlalchemy import func, select

from lumen.commands import erase_entity_cmd
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
from lumen.models.group_member import GroupMember, get_group_owner
from lumen.models.message import Message
from lumen.models.model_config import ModelConfig
from lumen.models.model_stat import ModelStat
from lumen.models.request_log import RequestLog
from lumen.services import metrics_snapshot
from lumen.services.crypto import hash_api_key
from lumen.services.erasure import get_or_create_ghost
from lumen.services.llm import bill_request
from tests.conftest import make_project

EXPIRES = datetime(2030, 1, 1)
CREATED = datetime(2025, 1, 2, 3, 4, 5)
REVOKED = datetime(2025, 6, 1, 12, 0, 0)

_n = 0


def _user(name):
    global _n
    _n += 1
    user = Entity(entity_type="user", email=f"{name.lower()}{_n}@example.com", name=name,
                  initials=name[:2].upper(), active=True)
    db.session.add(user)
    db.session.flush()
    return user


def _key(entity_id, raw, **fields):
    key = APIKey(entity_id=entity_id, name=fields.pop("name", f"key {raw}"), key_hash=hash_api_key(raw),
                 key_hint=raw[-4:], created_at=CREATED, **fields)
    db.session.add(key)
    db.session.flush()
    return key


def _seed_user(name, model_id, private_model_name, raw_prefix):
    """A user with every kind of personal data and usage the command handles."""
    user = _user(name)
    active = _key(user.id, f"{raw_prefix}-active", client_id="cli", requested_by="laptop",
                  created_by_entity_id=user.id, expires_at=EXPIRES)
    revoked = _key(user.id, f"{raw_prefix}-revoked", revoked_at=REVOKED, revoked_by_entity_id=user.id)
    conv = Conversation(entity_id=user.id, title="secret", model="erase-model")
    db.session.add(conv)
    db.session.flush()
    db.session.add_all([
        Message(conversation_id=conv.id, role="user", content="my secret"),
        Message(conversation_id=conv.id, role="assistant", content="noted"),
        AuthRequest(flow="device", client_id="cli", requested_name="k", status="approved",
                    expires_at=EXPIRES, entity_id=user.id),
        EntityBalance(entity_id=user.id, coins_left=5),
        EntityLimit(entity_id=user.id, max_coins=10, refresh_coins=1, starting_coins=10),
        EntityModelConsent(entity_id=user.id, model_config_id=model_id),
        ModelConfig(model_name=private_model_name, input_cost_per_million=1, output_cost_per_million=1,
                    owner_entity_id=user.id),
    ])
    db.session.add(EntityStat(entity_id=user.id, requests=0, input_tokens=0, output_tokens=0,
                              audio_seconds=0, cost=0, conversations=1))
    db.session.commit()
    bill_request(user.id, model_id, "chat", 100, 50, 0.25, effective=None, audio_seconds=3)
    bill_request(user.id, model_id, "api", 10, 5, 0.5, effective=None, api_key_id=active.id)
    return user.id, active.id, revoked.id


@pytest.fixture
def world(app):
    """The user to erase, another user, a project, groups and a public model."""
    with app.app_context():
        model = ModelConfig(model_name="erase-model", input_cost_per_million=1, output_cost_per_million=1)
        db.session.add(model)
        db.session.flush()
        model_id = model.id
        user_id, active_id, revoked_id = _seed_user("Erin", model_id, "erin-private", "sk-erin")

        other = _user("Olga")
        project = make_project("Lab", owner_id=other.id)
        db.session.add(EntityManager(user_entity_id=user_id, project_entity_id=project.id))
        project_key = _key(project.id, "sk-project", created_by_entity_id=user_id,
                           revoked_at=REVOKED, revoked_by_entity_id=user_id)
        other_key = _key(other.id, "sk-olga", created_by_entity_id=other.id)
        other_conv = Conversation(entity_id=other.id, title="olga's", model="erase-model")
        db.session.add(other_conv)
        db.session.flush()
        db.session.add(Message(conversation_id=other_conv.id, role="user", content="hi"))
        # Another user's auth request that minted one of Erin's keys (code replay record).
        db.session.add(AuthRequest(flow="code", client_id="app", requested_name="k", status="claimed",
                                   expires_at=EXPIRES, entity_id=other.id, api_key_id=active_id))

        owned = Group(name="erin-owned", active=True)
        member = Group(name="erin-member", active=True)
        db.session.add_all([owned, member])
        db.session.flush()
        db.session.add_all([
            GroupMember(group_id=owned.id, entity_id=user_id, is_owner=True),
            GroupMember(group_id=owned.id, entity_id=other.id),
            GroupMember(group_id=member.id, entity_id=user_id),
            GroupMember(group_id=member.id, entity_id=other.id, is_owner=True),
        ])
        db.session.commit()
        bill_request(other.id, model_id, "chat", 7, 3, 0.125, effective=None)
        return {
            "user": user_id, "active_key": active_id, "revoked_key": revoked_id,
            "other": other.id, "project": project.id, "project_key": project_key.id,
            "other_key": other_key.id, "other_conv": other_conv.id, "model": model_id,
            "owned_group": owned.id, "member_group": member.id,
        }


def _erase(app, entity_id, *args, input=None):
    return app.test_cli_runner().invoke(erase_entity_cmd, [str(entity_id), *args], input=input)


def _sum(model, *columns):
    return tuple(db.session.execute(select(*(func.coalesce(func.sum(c), 0) for c in columns))).one())


def _stat_totals():
    return {
        "model_stats": _sum(ModelStat, ModelStat.requests, ModelStat.input_tokens, ModelStat.output_tokens,
                            ModelStat.audio_seconds, ModelStat.cost),
        "entity_stats": _sum(EntityStat, EntityStat.requests, EntityStat.input_tokens,
                             EntityStat.output_tokens, EntityStat.audio_seconds, EntityStat.cost,
                             EntityStat.conversations),
    }


def _per_model_metrics():
    return dict(metrics_snapshot.refresh_snapshot().model_usage)


def _users_header(admin_client):
    captured = {}

    def record(sender, template, context, **extra):
        captured.update(context)

    with template_rendered.connected_to(record):
        assert admin_client.get("/admin/users").status_code == HTTPStatus.OK
    return {k: captured[k] for k in ("total_users", "total_requests", "total_tokens", "total_cost")}


def _ghost_id():
    return db.session.execute(select(Entity.id).where(Entity.entity_type == "ghost")).scalar_one()


# --------------------------------------------------------------------------- #
# Refusals                                                                     #
# --------------------------------------------------------------------------- #

def test_refuses_a_project(app, world):
    result = _erase(app, world["project"], "--yes")
    assert result.exit_code == 1
    assert "is a project, not a user" in result.output
    with app.app_context():
        assert db.session.get(Entity, world["project"]).active is True


def test_refuses_the_ghost(app, world):
    with app.app_context():
        ghost_id = get_or_create_ghost().id
    result = _erase(app, ghost_id, "--yes")
    assert result.exit_code == 1
    assert "is a ghost, not a user" in result.output
    with app.app_context():
        assert db.session.get(Entity, ghost_id) is not None


def test_refuses_a_project_owner(app, world):
    result = _erase(app, world["other"], "--yes")
    assert result.exit_code == 1
    assert "owns these projects: Lab" in result.output
    assert "Transfer their ownership first" in result.output
    with app.app_context():
        assert db.session.get(Entity, world["other"]).active is True


def test_refuses_without_confirmation(app, world):
    result = _erase(app, world["user"], input="n\n")
    assert result.exit_code == 1
    assert "Erin <erin" in result.output
    assert "groups that will be left without an owner: erin-owned" in result.output
    assert "Aborted." in result.output
    with app.app_context():
        user = db.session.get(Entity, world["user"])
        assert user is not None and user.active is True
        assert db.session.execute(select(Entity).where(Entity.entity_type == "ghost")).first() is None


def test_confirmation_yes_erases(app, world):
    result = _erase(app, world["user"], input="y\n")
    assert result.exit_code == 0, result.output
    with app.app_context():
        assert db.session.get(Entity, world["user"]) is None


# --------------------------------------------------------------------------- #
# A full erasure                                                               #
# --------------------------------------------------------------------------- #

def test_erasure_deletes_every_personal_row(app, world):
    with app.app_context():
        user_convs = db.session.execute(
            select(Conversation.id).where(Conversation.entity_id == world["user"])).scalars().all()

    result = _erase(app, world["user"], "--yes")

    assert result.exit_code == 0, result.output
    for line in ("messages deleted: 2", "conversations deleted: 1", "auth_requests deleted: 1",
                 "entity_balances deleted: 1", "entity_limits deleted: 1",
                 "entity_model_consents deleted: 1", "group_members deleted: 2",
                 "entity_managers deleted: 1", f"user {world['user']} deleted"):
        assert line in result.output
    u = world["user"]
    with app.app_context():
        assert db.session.get(Entity, u) is None
        for model, column in [
            (Conversation, Conversation.entity_id), (AuthRequest, AuthRequest.entity_id),
            (EntityBalance, EntityBalance.entity_id), (EntityLimit, EntityLimit.entity_id),
            (EntityModelConsent, EntityModelConsent.entity_id), (GroupMember, GroupMember.entity_id),
            (EntityManager, EntityManager.user_entity_id), (EntityManager, EntityManager.project_entity_id),
            (ModelStat, ModelStat.entity_id), (EntityStat, EntityStat.entity_id),
            (RequestLog, RequestLog.entity_id), (APIKey, APIKey.entity_id),
            (APIKey, APIKey.created_by_entity_id), (APIKey, APIKey.revoked_by_entity_id),
            (ModelConfig, ModelConfig.owner_entity_id),
        ]:
            assert db.session.scalar(select(func.count()).select_from(model).where(column == u)) == 0, column
        assert db.session.scalar(select(func.count()).select_from(Message)
                                 .where(Message.conversation_id.in_(user_convs))) == 0


def test_erasure_revokes_anonymises_and_moves_keys(app, world):
    with app.app_context():
        before = {k.id: k for k in db.session.execute(
            select(APIKey).where(APIKey.entity_id == world["user"])).scalars()}
        old = {k.id: (k.key_hash, k.requests, k.input_tokens, k.output_tokens, k.audio_seconds, k.cost,
                      k.created_at, k.last_used_at, k.expires_at, k.revoked_at) for k in before.values()}
        db.session.expunge_all()
    assert old[world["active_key"]][1] == 1  # billed through bill_request

    result = _erase(app, world["user"], "--yes")

    assert result.exit_code == 0, result.output
    assert "api_keys revoked, anonymised and moved to the ghost: 2" in result.output
    with app.app_context():
        ghost_id = _ghost_id()
        keys = db.session.execute(select(APIKey).where(APIKey.id.in_(old))).scalars().all()
        assert len(keys) == 2
        hashes = set()
        for key in keys:
            was = old[key.id]
            assert key.entity_id == ghost_id
            assert key.name == "ghost key"
            assert key.key_hint is None and key.client_id is None and key.requested_by is None
            assert key.created_by_entity_id is None and key.revoked_by_entity_id is None
            assert re.fullmatch(r"[0-9a-f]{64}", key.key_hash)
            assert key.key_hash != was[0]
            hashes.add(key.key_hash)
            assert (key.requests, key.input_tokens, key.output_tokens, key.audio_seconds, key.cost,
                    key.created_at, key.last_used_at, key.expires_at) == was[1:9]
        assert len(hashes) == 2
        by_id = {k.id: k for k in keys}
        assert by_id[world["revoked_key"]].revoked_at == REVOKED
        assert by_id[world["active_key"]].revoked_at is not None
        for raw in ("sk-erin-active", "sk-erin-revoked"):
            assert db.session.execute(select(APIKey).where(APIKey.key_hash == hash_api_key(raw))).first() is None


def test_old_raw_key_gets_401(app, client, world):
    headers = {"Authorization": "Bearer sk-erin-active"}
    assert client.get("/v1/models", headers=headers).status_code == HTTPStatus.OK

    assert _erase(app, world["user"], "--yes").exit_code == 0

    assert client.get("/v1/models", headers=headers).status_code == HTTPStatus.UNAUTHORIZED


def test_erasure_moves_history_stats_and_models_to_the_ghost(app, world):
    with app.app_context():
        totals = _stat_totals()
        log_ids = db.session.execute(
            select(RequestLog.id).where(RequestLog.entity_id == world["user"])).scalars().all()
        user_stats = {(s.model_config_id, s.source): (s.requests, s.input_tokens, s.cost)
                      for s in db.session.execute(select(ModelStat).filter_by(entity_id=world["user"])).scalars()}
        user_entity_stat = db.session.get(EntityStat, world["user"])
        user_entity = (user_entity_stat.requests, user_entity_stat.cost, user_entity_stat.conversations)
    assert len(log_ids) == 2

    result = _erase(app, world["user"], "--yes")

    assert result.exit_code == 0, result.output
    assert "request_logs moved to the ghost: 2" in result.output
    assert "model_stats moved: 2, merged: 0; entity_stats merged: 1" in result.output
    assert "model disabled and moved to the ghost: erin-private" in result.output
    with app.app_context():
        ghost_id = _ghost_id()
        owners = db.session.execute(select(RequestLog.entity_id).where(RequestLog.id.in_(log_ids))).scalars().all()
        assert owners == [ghost_id, ghost_id]
        assert _stat_totals() == totals
        ghost_stats = {(s.model_config_id, s.source): (s.requests, s.input_tokens, s.cost)
                       for s in db.session.execute(select(ModelStat).filter_by(entity_id=ghost_id)).scalars()}
        assert ghost_stats == user_stats
        ghost_entity = db.session.get(EntityStat, ghost_id)
        assert (ghost_entity.requests, ghost_entity.cost, ghost_entity.conversations) == user_entity
        private = db.session.execute(select(ModelConfig).filter_by(model_name="erin-private")).scalar_one()
        assert private.disabled is True and private.owner_entity_id == ghost_id


def test_model_still_in_config_yaml_gets_a_warning(app, world, monkeypatch):
    yaml_data = dict(app.config["YAML_DATA"])
    yaml_data["models"] = [*yaml_data.get("models", []), {"name": "erin-private"}]
    monkeypatch.setitem(app.config, "YAML_DATA", yaml_data)

    result = _erase(app, world["user"], "--yes")

    assert result.exit_code == 0, result.output
    assert "Warning: 'erin-private' is still in config.yaml" in result.output


def test_usage_readers_after_erasure(app, admin_client, world):
    with app.app_context():
        metrics = _per_model_metrics()
        users_total = metrics_snapshot.refresh_snapshot().users_total
    header = _users_header(admin_client)
    listed = [u["id"] for u in admin_client.get("/admin/api/users").get_json()["users"]]
    assert world["user"] in listed

    assert _erase(app, world["user"], "--yes").exit_code == 0

    with app.app_context():
        assert _per_model_metrics() == metrics
        assert metrics_snapshot.refresh_snapshot().users_total == users_total - 1
        ghost_id = _ghost_id()
    after = _users_header(admin_client)
    assert after["total_users"] == header["total_users"] - 1
    assert (after["total_requests"], after["total_tokens"], after["total_cost"]) == \
        (header["total_requests"], header["total_tokens"], header["total_cost"])
    listed = [u["id"] for u in admin_client.get("/admin/api/users").get_json()["users"]]
    assert world["user"] not in listed
    assert ghost_id not in listed
    listed = [u["id"] for u in admin_client.get("/admin/api/users?sort=requests&order=desc").get_json()["users"]]
    assert ghost_id not in listed


def test_owned_group_survives_without_an_owner(app, world):
    result = _erase(app, world["user"], "--yes")

    assert result.exit_code == 0, result.output
    assert "group left without an owner: erin-owned" in result.output
    with app.app_context():
        assert db.session.get(Group, world["owned_group"]) is not None
        assert get_group_owner(world["owned_group"]) is None
        members = db.session.execute(select(GroupMember.entity_id, GroupMember.is_owner)
                                     .order_by(GroupMember.group_id)).all()
        assert sorted(members) == [(world["other"], False), (world["other"], True)]
        assert get_group_owner(world["member_group"]).id == world["other"]


def test_other_entities_keep_their_rows(app, world):
    assert _erase(app, world["user"], "--yes").exit_code == 0

    with app.app_context():
        project_key = db.session.get(APIKey, world["project_key"])
        assert project_key.entity_id == world["project"]
        assert project_key.created_by_entity_id is None and project_key.revoked_by_entity_id is None
        assert project_key.revoked_at == REVOKED and project_key.name == "key sk-project"
        other_request = db.session.execute(
            select(AuthRequest).where(AuthRequest.entity_id == world["other"])).scalar_one()
        assert other_request.api_key_id == world["active_key"]
        assert db.session.get(APIKey, world["active_key"]).entity_id == _ghost_id()
        other_key = db.session.get(APIKey, world["other_key"])
        assert (other_key.entity_id, other_key.name, other_key.key_hash, other_key.created_by_entity_id) == \
            (world["other"], "key sk-olga", hash_api_key("sk-olga"), world["other"])
        assert db.session.get(Conversation, world["other_conv"]) is not None
        assert db.session.scalar(select(func.count()).select_from(Message)
                                 .where(Message.conversation_id == world["other_conv"])) == 1
        project = db.session.get(Entity, world["project"])
        assert project.owner_entity_id == world["other"]
        assert db.session.execute(select(EntityManager.user_entity_id)
                                  .filter_by(project_entity_id=world["project"])).scalars().all() == [world["other"]]


def test_second_erasure_merges_into_the_same_ghost(app, world):
    with app.app_context():
        second, *_ = _seed_user("Sam", world["model"], "sam-private", "sk-sam")
        totals = _stat_totals()
        user_sum = _sum(EntityStat, EntityStat.requests, EntityStat.cost)
        both = db.session.execute(
            select(func.sum(EntityStat.requests), func.sum(EntityStat.cost))
            .where(EntityStat.entity_id.in_([world["user"], second]))).one()

    assert _erase(app, world["user"], "--yes").exit_code == 0
    result = _erase(app, second, "--yes")

    assert result.exit_code == 0, result.output
    assert "model_stats moved: 0, merged: 2; entity_stats merged: 1" in result.output
    with app.app_context():
        assert db.session.scalar(select(func.count()).select_from(Entity)
                                 .where(Entity.entity_type == "ghost")) == 1
        ghost_id = _ghost_id()
        keys = db.session.execute(select(APIKey).filter_by(entity_id=ghost_id)).scalars().all()
        assert len(keys) == 4 and {k.name for k in keys} == {"ghost key"}
        assert all(k.revoked_at is not None for k in keys)
        ghost = db.session.get(EntityStat, ghost_id)
        assert (ghost.requests, ghost.cost) == tuple(both)
        assert _stat_totals() == totals
        assert _sum(EntityStat, EntityStat.requests, EntityStat.cost) == user_sum
        owned = db.session.execute(select(ModelConfig.model_name).filter_by(owner_entity_id=ghost_id)
                                   .order_by(ModelConfig.model_name)).scalars().all()
        assert owned == ["erin-private", "sam-private"]


def test_rerun_with_a_deleted_id_has_nothing_to_do(app, world):
    assert _erase(app, world["user"], "--yes").exit_code == 0

    result = _erase(app, world["user"], "--yes")

    assert result.exit_code == 0, result.output
    assert "nothing to do" in result.output


def test_rerun_after_a_failure_before_the_delete_finishes(app, world, monkeypatch):
    """An interrupted run leaves the user inactive, every row owned by the user or the
    ghost, and nothing deleted; the same command then completes."""
    from lumen import commands

    def boom(user_id):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(commands, "_erase_dependents", boom)
    result = _erase(app, world["user"], "--yes")
    assert result.exit_code != 0
    with app.app_context():
        user = db.session.get(Entity, world["user"])
        assert user is not None and user.active is False
        assert db.session.scalar(select(func.count()).select_from(APIKey)
                                 .where(APIKey.entity_id == world["user"])) == 2
        assert db.session.scalar(select(func.count()).select_from(Conversation)
                                 .where(Conversation.entity_id == world["user"])) == 1
    monkeypatch.undo()

    result = _erase(app, world["user"], "--yes")

    assert result.exit_code == 0, result.output
    with app.app_context():
        assert db.session.get(Entity, world["user"]) is None


def test_refuses_to_delete_while_rows_still_reference_the_user(app, world, monkeypatch):
    """SQLite has no RESTRICT: the command checks itself and rolls everything back."""
    from lumen import commands

    monkeypatch.setattr(commands, "_ghost_keys", lambda user_id, ghost_id: 0)
    result = _erase(app, world["user"], "--yes")

    assert result.exit_code == 1
    assert "rows still reference user" in result.output
    with app.app_context():
        assert db.session.get(Entity, world["user"]) is not None
        assert db.session.scalar(select(func.count()).select_from(Conversation)
                                 .where(Conversation.entity_id == world["user"])) == 1
        assert db.session.get(EntityStat, world["user"]) is not None
        assert db.session.get(APIKey, world["active_key"]).key_hash == hash_api_key("sk-erin-active")


def test_check_stats_is_clean_after_erasure(app, world):
    """model_stats and entity_stats still agree with request_logs, per entity and in total."""
    assert _erase(app, world["user"], "--yes").exit_code == 0

    with app.app_context():
        logs = dict(db.session.execute(
            select(RequestLog.entity_id, func.count()).group_by(RequestLog.entity_id)).all())
        stats = dict(db.session.execute(
            select(ModelStat.entity_id, func.sum(ModelStat.requests)).group_by(ModelStat.entity_id)).all())
        entity = dict(db.session.execute(select(EntityStat.entity_id, EntityStat.requests)).all())
        assert logs == stats == entity
        assert _ghost_id() in logs


# --------------------------------------------------------------------------- #
# Coverage guard                                                               #
# --------------------------------------------------------------------------- #

# Every foreign key into the tables erasure empties, and the step that handles it.
HANDLED = {
    ("api_keys", "entity_id"): "_ghost_keys",
    ("api_keys", "created_by_entity_id"): "_ghost_keys",
    ("api_keys", "revoked_by_entity_id"): "_ghost_keys",
    ("auth_requests", "entity_id"): "_erase_dependents",
    # The key survives under the ghost, so the reference stays valid.
    ("auth_requests", "api_key_id"): "no-op",
    ("conversations", "entity_id"): "_erase_dependents",
    ("entity_balances", "entity_id"): "_erase_dependents",
    ("entity_limits", "entity_id"): "_erase_dependents",
    ("entity_managers", "project_entity_id"): "the entity is a user, never a project",
    ("entity_managers", "user_entity_id"): "_erase_dependents",
    ("entity_model_consents", "entity_id"): "_erase_dependents",
    ("entity_stats", "entity_id"): "merge_stats_into",
    ("group_members", "entity_id"): "_erase_dependents",
    ("messages", "conversation_id"): "_erase_dependents",
    ("model_configs", "owner_entity_id"): "models step",
    ("model_stats", "entity_id"): "merge_stats_into",
    ("request_logs", "entity_id"): "request_logs move",
}


def test_every_foreign_key_into_erased_tables_is_handled(app):
    found = set()
    for table in db.metadata.tables.values():
        for fk in table.foreign_keys:
            if fk.column.table.name in {"entities", "conversations", "api_keys"} and fk.column.name == "id":
                found.add((table.name, fk.parent.name))
    assert found == set(HANDLED), (
        "a foreign key into entities, conversations or api_keys is not handled by erase-entity: "
        f"{sorted(found - set(HANDLED))}; stale entries: {sorted(set(HANDLED) - found)}"
    )


T_OLD = datetime(2025, 1, 1)
T_MID = datetime(2025, 6, 1)
T_NEW = datetime(2025, 12, 1)


@pytest.mark.parametrize(("ghost_ts", "user_ts", "expected"), [
    (T_OLD, T_MID, T_MID),
    (T_NEW, T_MID, T_NEW),
    (None, T_MID, T_MID),
    (T_NEW, None, T_NEW),
], ids=["ghost-older", "ghost-newer", "ghost-null", "user-null"])
def test_merge_into_an_existing_ghost_keeps_the_latest_last_used_at(app, world, ghost_ts, user_ts, expected):
    """One erasure where the ghost already has the user's (model, chat) row but not the
    (model, api) one: the first is merged, the second re-pointed, and last_used_at is the
    later of the two on both stats tables."""
    u, model = world["user"], world["model"]
    with app.app_context():
        ghost_id = get_or_create_ghost().id
        db.session.add_all([
            ModelStat(entity_id=ghost_id, model_config_id=model, source="chat", requests=5, input_tokens=50,
                      output_tokens=25, audio_seconds=1, cost=1, last_used_at=ghost_ts),
            EntityStat(entity_id=ghost_id, requests=5, input_tokens=50, output_tokens=25, audio_seconds=1,
                       cost=1, conversations=2, last_used_at=ghost_ts),
        ])
        db.session.flush()
        # model_stats.last_used_at defaults to now on insert, so NULL is set after it.
        for stat in db.session.execute(select(ModelStat).filter_by(entity_id=ghost_id)).scalars():
            stat.last_used_at = ghost_ts
        for stat in db.session.execute(select(ModelStat).filter_by(entity_id=u)).scalars():
            stat.last_used_at = user_ts
        db.session.get(EntityStat, u).last_used_at = user_ts
        db.session.commit()
        user_rows = {s.source: (s.requests, s.input_tokens, s.output_tokens, s.audio_seconds, s.cost)
                     for s in db.session.execute(select(ModelStat).filter_by(entity_id=u)).scalars()}
        user_entity = db.session.get(EntityStat, u)
        user_entity = (user_entity.requests, user_entity.cost, user_entity.conversations)
        totals = _stat_totals()

    result = _erase(app, u, "--yes")

    assert result.exit_code == 0, result.output
    assert "model_stats moved: 1, merged: 1; entity_stats merged: 1" in result.output
    with app.app_context():
        rows = {s.source: s for s in db.session.execute(select(ModelStat).filter_by(entity_id=ghost_id)).scalars()}
        assert set(rows) == {"chat", "api"}
        chat = rows["chat"]
        r, i, o, a, c = user_rows["chat"]
        assert (chat.requests, chat.input_tokens, chat.output_tokens, chat.audio_seconds, chat.cost) == \
            (r + 5, i + 50, o + 25, a + 1, c + 1)
        assert chat.last_used_at == expected
        api = rows["api"]
        assert (api.requests, api.input_tokens, api.output_tokens, api.audio_seconds, api.cost) == user_rows["api"]
        assert api.last_used_at == user_ts
        ghost = db.session.get(EntityStat, ghost_id)
        assert (ghost.requests, ghost.cost, ghost.conversations) == \
            (user_entity[0] + 5, user_entity[1] + 1, user_entity[2] + 2)
        assert ghost.last_used_at == expected
        assert _stat_totals() == totals
