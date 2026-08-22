"""Tests for the groups blueprint (list, detail, members, ownership, model grants)."""
from http import HTTPStatus

import pytest

from tests.conftest import set_model_owner

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_group(app, name="research-lab", owner_id=None, active=True, auto_join=False):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        g = Group(name=name, active=active, auto_join=auto_join)
        db.session.add(g)
        db.session.flush()
        if owner_id:
            db.session.add(GroupMember(group_id=g.id, entity_id=owner_id, is_owner=True))
        db.session.commit()
        return g.id


@pytest.fixture
def owned_group(app, test_user):
    """A group owned by test_user."""
    return _make_group(app, name="owned-group", owner_id=test_user["id"])


@pytest.fixture
def member_group(app, test_user, second_user):
    """A group test_user belongs to but does not own (second_user owns it)."""
    gid = _make_group(app, name="member-group", owner_id=second_user["id"])
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_member import GroupMember
        db.session.add(GroupMember(group_id=gid, entity_id=test_user["id"]))
        db.session.commit()
    return gid


@pytest.fixture
def config_group(app, test_user):
    gid = _make_group(app, name="config-group", auto_join=True)
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_member import GroupMember
        from lumen.models.group_rule import GroupRule
        db.session.add(GroupRule(group_id=gid, field="affiliation", match="contains", value="staff@x.edu"))
        db.session.add(GroupMember(group_id=gid, entity_id=test_user["id"], config_managed=True))
        db.session.commit()
    return gid


@pytest.fixture
def second_user(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        e = Entity(entity_type="user", email="second@example.com", name="Second User",
                   initials="SU", active=True)
        db.session.add(e)
        db.session.commit()
        return {"id": e.id, "name": e.name, "email": e.email}


@pytest.fixture
def test_project(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        e = Entity(entity_type="project", name="Research Bot", initials="RB", active=True)
        db.session.add(e)
        db.session.commit()
        return {"id": e.id, "name": e.name}


def _member_ids(app, gid):
    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.group_member import GroupMember
        return set(db.session.execute(
            select(GroupMember.entity_id).where(GroupMember.group_id == gid)
        ).scalars().all())


def _group_limit(app, gid):
    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.group_limit import GroupLimit
        return db.session.execute(select(GroupLimit).filter_by(group_id=gid)).scalar_one_or_none()


def _summary_cards(client):
    """Read the {label: value} pairs out of the rendered summary cards."""
    import re
    html = client.get("/groups").data.decode()
    pairs = re.findall(
        r'<div class="text-muted small mb-1">([^<]+)</div>\s*<div class="fs-4 fw-semibold">([\d,]+)</div>',
        html,
    )
    return {k: int(v.replace(",", "")) for k, v in pairs if k in ("Total Members", "Total Requests")}


# ---------------------------------------------------------------------------
# List page and /groups/data
# ---------------------------------------------------------------------------

def test_index_requires_login(client):
    assert client.get("/groups").status_code in (HTTPStatus.FOUND, HTTPStatus.UNAUTHORIZED)


def test_index_renders_for_plain_user(auth_client):
    resp = auth_client.get("/groups")
    assert resp.status_code == HTTPStatus.OK
    assert b"New Group" in resp.data


def test_data_empty_for_non_member(auth_client, app):
    _make_group(app, name="someone-elses")
    data = auth_client.get("/groups/data").get_json()
    assert data["groups"] == []
    assert data["total"] == 0


def test_data_lists_member_groups(auth_client, owned_group, member_group):
    data = auth_client.get("/groups/data").get_json()
    names = {g["name"] for g in data["groups"]}
    assert names == {"owned-group", "member-group"}
    by_name = {g["name"]: g for g in data["groups"]}
    assert by_name["owned-group"]["is_owner"] is True
    assert by_name["member-group"]["is_owner"] is False


def test_data_admin_sees_all(admin_client, app, owned_group):
    _make_group(app, name="unrelated")
    data = admin_client.get("/groups/data").get_json()
    assert {g["name"] for g in data["groups"]} == {"owned-group", "unrelated"}


def test_data_includes_inactive_groups(auth_client, app, test_user):
    _make_group(app, name="off-group", owner_id=test_user["id"], active=False)
    data = auth_client.get("/groups/data").get_json()
    assert data["groups"][0]["active"] is False


def test_data_invalid_per_page_falls_back(auth_client, owned_group):
    data = auth_client.get("/groups/data?per_page=7").get_json()
    assert data["per_page"] == 25


def test_data_pagination(auth_client, app, test_user):
    for i in range(30):
        _make_group(app, name=f"g{i:02d}", owner_id=test_user["id"])
    data = auth_client.get("/groups/data?per_page=25&page=2").get_json()
    assert data["total"] == 30
    assert len(data["groups"]) == 5


def test_data_search_filters_by_name(auth_client, app, test_user):
    _make_group(app, name="alpha", owner_id=test_user["id"])
    _make_group(app, name="beta", owner_id=test_user["id"])
    data = auth_client.get("/groups/data?search=alp").get_json()
    assert [g["name"] for g in data["groups"]] == ["alpha"]


def test_data_aggregates_member_usage(auth_client, app, owned_group, test_user, second_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_stat import EntityStat
        from lumen.models.group_member import GroupMember
        db.session.add(GroupMember(group_id=owned_group, entity_id=second_user["id"]))
        db.session.add(EntityStat(entity_id=test_user["id"], requests=3, input_tokens=10,
                                  output_tokens=5, cost=1.5))
        db.session.add(EntityStat(entity_id=second_user["id"], requests=2, input_tokens=1,
                                  output_tokens=4, cost=0.5))
        db.session.commit()
    row = auth_client.get("/groups/data").get_json()["groups"][0]
    assert row["requests"] == 5
    assert row["tokens"] == 20
    assert row["cost"] == pytest.approx(2.0)
    assert row["members"] == 2


def test_data_includes_coin_policy(admin_client, app, owned_group):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_limit import GroupLimit
        db.session.add(GroupLimit(group_id=owned_group, max_coins=50, refresh_coins=5, starting_coins=50))
        db.session.commit()
    row = admin_client.get("/groups/data").get_json()["groups"][0]
    assert row["max_coins"] == pytest.approx(50.0)
    assert row["refresh_coins"] == pytest.approx(5.0)


def test_data_no_limit_is_null(auth_client, owned_group):
    row = auth_client.get("/groups/data").get_json()["groups"][0]
    assert row["max_coins"] is None
    assert row["refresh_coins"] is None


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

def test_detail_forbidden_for_non_member(auth_client, app):
    gid = _make_group(app, name="private")
    assert auth_client.get(f"/groups/{gid}").status_code == HTTPStatus.FORBIDDEN


def test_detail_ok_for_member(auth_client, member_group):
    resp = auth_client.get(f"/groups/{member_group}")
    assert resp.status_code == HTTPStatus.OK
    # A non-owner member gets no management controls.
    assert b"Add Member" not in resp.data


def test_detail_owner_sees_controls(auth_client, owned_group):
    resp = auth_client.get(f"/groups/{owned_group}")
    assert resp.status_code == HTTPStatus.OK
    assert b"Add Member" in resp.data


def test_detail_admin_ok(admin_client, app):
    gid = _make_group(app, name="somebody")
    assert admin_client.get(f"/groups/{gid}").status_code == HTTPStatus.OK


def test_detail_404_unknown(admin_client):
    assert admin_client.get("/groups/99999").status_code == HTTPStatus.NOT_FOUND


def test_detail_add_model_disabled_without_owned_models(auth_client, owned_group):
    resp = auth_client.get(f"/groups/{owned_group}")
    assert b"you have no models of your own left to grant" in resp.data.lower()


def test_detail_add_model_enabled_with_owned_model(auth_client, app, owned_group, test_model, test_user):
    with app.app_context():
        set_model_owner(test_model["id"], test_user["id"])
    resp = auth_client.get(f"/groups/{owned_group}")
    assert b'id="add-model-open-btn"' in resp.data


def test_detail_auto_join_group_read_only_for_member(auth_client, config_group):
    """An auto-join group is fully automatic: ownerless, so a member sees no
    management controls, no member data, and no Rules tab."""
    resp = auth_client.get(f"/groups/{config_group}")
    assert resp.status_code == HTTPStatus.OK
    assert b"Add Member" not in resp.data
    assert b'id="tab-rules"' not in resp.data


def test_detail_auto_join_group_admin_sees_no_member_actions(admin_client, config_group):
    """Even admins manage auto-join membership through the rules, not by hand."""
    resp = admin_client.get(f"/groups/{config_group}")
    assert resp.status_code == HTTPStatus.OK
    assert b"Add Member" not in resp.data
    assert b"Membership is managed by this group" in resp.data


def test_detail_shows_rules_tab_to_admin(admin_client, config_group):
    resp = admin_client.get(f"/groups/{config_group}")
    assert resp.status_code == HTTPStatus.OK
    assert b'id="tab-rules"' in resp.data
    assert b"staff@x.edu" in resp.data


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def test_create_requires_name(auth_client):
    resp = auth_client.post("/groups", json={"name": "  "})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_create_by_non_admin_makes_creator_owner(auth_client, app, test_user):
    resp = auth_client.post("/groups", json={"name": "my-lab"})
    assert resp.status_code == HTTPStatus.CREATED
    gid = resp.get_json()["id"]
    with app.app_context():
        from lumen.models.group_member import get_group_owner
        assert get_group_owner(gid).id == test_user["id"]


def test_create_by_non_admin_rejects_owner_email(auth_client, second_user):
    resp = auth_client.post("/groups", json={"name": "x", "owner_email": second_user["email"]})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_create_by_non_admin_rejects_coins(auth_client):
    resp = auth_client.post("/groups", json={"name": "x", "max_coins": "10"})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_create_by_non_admin_makes_no_limit_row(auth_client, app):
    gid = auth_client.post("/groups", json={"name": "no-pool"}).get_json()["id"]
    assert _group_limit(app, gid) is None


def test_create_duplicate_name_conflicts(auth_client, owned_group):
    resp = auth_client.post("/groups", json={"name": "owned-group"})
    assert resp.status_code == HTTPStatus.CONFLICT


def test_admin_create_with_owner_and_coins(admin_client, app, second_user):
    resp = admin_client.post("/groups", json={
        "name": "admin-made", "owner_email": second_user["email"],
        "max_coins": "40", "refresh_coins": "4",
    })
    assert resp.status_code == HTTPStatus.CREATED
    gid = resp.get_json()["id"]
    with app.app_context():
        from lumen.models.group_member import get_group_owner
        assert get_group_owner(gid).id == second_user["id"]
    limit = _group_limit(app, gid)
    assert float(limit.max_coins) == 40.0
    assert float(limit.refresh_coins) == 4.0
    # starting_coins tracks a finite max so refill-from-zero works.
    assert float(limit.starting_coins) == 40.0


def test_admin_create_without_owner(admin_client, app):
    gid = admin_client.post("/groups", json={"name": "ownerless"}).get_json()["id"]
    assert _member_ids(app, gid) == set()


def test_admin_create_unknown_owner_404(admin_client):
    resp = admin_client.post("/groups", json={"name": "x", "owner_email": "nobody@example.com"})
    assert resp.status_code == HTTPStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# Update / toggle / delete
# ---------------------------------------------------------------------------

def test_owner_can_rename(auth_client, app, owned_group):
    resp = auth_client.patch(f"/groups/{owned_group}", json={"name": "renamed"})
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        assert db.session.get(Group, owned_group).name == "renamed"


def test_owner_can_set_description(auth_client, app, owned_group):
    auth_client.patch(f"/groups/{owned_group}", json={"description": "the lab"})
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        assert db.session.get(Group, owned_group).description == "the lab"


def test_plain_member_cannot_update(auth_client, member_group):
    resp = auth_client.patch(f"/groups/{member_group}", json={"name": "nope"})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_owner_cannot_set_coins(auth_client, owned_group):
    resp = auth_client.patch(f"/groups/{owned_group}", json={"max_coins": "10"})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_admin_can_set_coins(admin_client, app, owned_group):
    resp = admin_client.patch(f"/groups/{owned_group}", json={"max_coins": "25", "refresh_coins": "2"})
    assert resp.status_code == HTTPStatus.OK
    limit = _group_limit(app, owned_group)
    assert float(limit.max_coins) == 25.0
    assert float(limit.starting_coins) == 25.0


def test_admin_blank_max_coins_deletes_limit(admin_client, app, owned_group):
    admin_client.patch(f"/groups/{owned_group}", json={"max_coins": "25", "refresh_coins": "2"})
    admin_client.patch(f"/groups/{owned_group}", json={"max_coins": "", "refresh_coins": ""})
    assert _group_limit(app, owned_group) is None


def test_admin_unlimited_keeps_starting_coins(admin_client, app, owned_group):
    admin_client.patch(f"/groups/{owned_group}", json={"max_coins": "-2"})
    limit = _group_limit(app, owned_group)
    assert float(limit.max_coins) == -2.0
    assert float(limit.starting_coins) == 0.0


def test_admin_invalid_coins_400(admin_client, owned_group):
    resp = admin_client.patch(f"/groups/{owned_group}", json={"max_coins": "abc"})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_update_duplicate_name_conflicts(auth_client, app, owned_group, test_user):
    _make_group(app, name="taken", owner_id=test_user["id"])
    resp = auth_client.patch(f"/groups/{owned_group}", json={"name": "taken"})
    assert resp.status_code == HTTPStatus.CONFLICT


def test_update_empty_name_400(auth_client, owned_group):
    resp = auth_client.patch(f"/groups/{owned_group}", json={"name": " "})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_auto_join_group_can_be_renamed(admin_client, app, config_group):
    """Rules reference the group by id, so renaming is safe and allowed."""
    resp = admin_client.patch(f"/groups/{config_group}", json={"name": "renamed-auto"})
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        g = db.session.get(Group, config_group)
        assert g.name == "renamed-auto"
        assert g.auto_join is True
        assert len(g.rules) == 1


def test_toggle_by_owner(auth_client, app, owned_group):
    resp = auth_client.post(f"/groups/{owned_group}/toggle")
    assert resp.get_json()["active"] is False


def test_toggle_by_plain_member_forbidden(auth_client, member_group):
    assert auth_client.post(f"/groups/{member_group}/toggle").status_code == HTTPStatus.FORBIDDEN


def test_owner_can_soft_delete(auth_client, app, owned_group):
    resp = auth_client.delete(f"/groups/{owned_group}")
    assert resp.status_code == HTTPStatus.NO_CONTENT
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        assert db.session.get(Group, owned_group).active is False


def test_plain_member_cannot_delete(auth_client, member_group):
    assert auth_client.delete(f"/groups/{member_group}").status_code == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

def test_members_data_lists_owner_flag(auth_client, owned_group, test_user):
    rows = auth_client.get(f"/groups/{owned_group}/members/data").get_json()["members"]
    assert len(rows) == 1
    assert rows[0]["id"] == test_user["id"]
    assert rows[0]["is_owner"] is True
    assert rows[0]["type"] == "user"


def test_members_data_forbidden_for_non_member(auth_client, app):
    gid = _make_group(app, name="closed")
    assert auth_client.get(f"/groups/{gid}/members/data").status_code == HTTPStatus.FORBIDDEN


def test_members_data_pagination(auth_client, app, owned_group):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.group_member import GroupMember
        for i in range(30):
            e = Entity(entity_type="user", email=f"m{i:02d}@example.com", name=f"Member {i:02d}",
                       initials="MM", active=True)
            db.session.add(e)
            db.session.flush()
            db.session.add(GroupMember(group_id=owned_group, entity_id=e.id))
        db.session.commit()
    data = auth_client.get(f"/groups/{owned_group}/members/data?per_page=25&page=2").get_json()
    assert data["total"] == 31  # 30 added + the owner
    assert len(data["members"]) == 6


def test_members_data_invalid_per_page_falls_back(auth_client, owned_group):
    data = auth_client.get(f"/groups/{owned_group}/members/data?per_page=7").get_json()
    assert data["per_page"] == 25


def test_members_data_search_matches_name_and_email(auth_client, owned_group, second_user, test_project):
    auth_client.post(f"/groups/{owned_group}/members", json={"entity_id": second_user["id"]})
    auth_client.post(f"/groups/{owned_group}/members", json={"entity_id": test_project["id"]})
    by_name = auth_client.get(f"/groups/{owned_group}/members/data?search=Research").get_json()
    assert [m["name"] for m in by_name["members"]] == ["Research Bot"]
    by_email = auth_client.get(f"/groups/{owned_group}/members/data?search=second@").get_json()
    assert [m["name"] for m in by_email["members"]] == ["Second User"]


def test_members_data_sort_by_type(auth_client, owned_group, test_project):
    auth_client.post(f"/groups/{owned_group}/members", json={"entity_id": test_project["id"]})
    asc = auth_client.get(f"/groups/{owned_group}/members/data?sort=type&order=asc").get_json()
    assert [m["type"] for m in asc["members"]] == ["project", "user"]
    desc = auth_client.get(f"/groups/{owned_group}/members/data?sort=type&order=desc").get_json()
    assert [m["type"] for m in desc["members"]] == ["user", "project"]


def test_member_search_excludes_existing(auth_client, owned_group, test_user):
    data = auth_client.get(f"/groups/{owned_group}/members/search?q=Test").get_json()
    assert all(e["id"] != test_user["id"] for e in data["entities"])


def test_member_search_returns_projects(auth_client, owned_group, test_project):
    data = auth_client.get(f"/groups/{owned_group}/members/search?q=Research").get_json()
    assert any(e["id"] == test_project["id"] and e["type"] == "project" for e in data["entities"])


def test_member_search_short_query(auth_client, owned_group):
    assert auth_client.get(f"/groups/{owned_group}/members/search?q=a").get_json()["entities"] == []


def test_add_user_member(auth_client, app, owned_group, second_user):
    resp = auth_client.post(f"/groups/{owned_group}/members", json={"entity_id": second_user["id"]})
    assert resp.status_code == HTTPStatus.CREATED
    assert second_user["id"] in _member_ids(app, owned_group)


def test_add_project_member(auth_client, app, owned_group, test_project):
    resp = auth_client.post(f"/groups/{owned_group}/members", json={"entity_id": test_project["id"]})
    assert resp.status_code == HTTPStatus.CREATED
    assert resp.get_json()["type"] == "project"
    assert test_project["id"] in _member_ids(app, owned_group)


def test_add_member_duplicate_conflicts(auth_client, owned_group, test_user):
    resp = auth_client.post(f"/groups/{owned_group}/members", json={"entity_id": test_user["id"]})
    assert resp.status_code == HTTPStatus.CONFLICT


def test_add_member_unknown_404(auth_client, owned_group):
    resp = auth_client.post(f"/groups/{owned_group}/members", json={"entity_id": 999999})
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_add_member_requires_id(auth_client, owned_group):
    assert auth_client.post(f"/groups/{owned_group}/members", json={}).status_code == HTTPStatus.BAD_REQUEST


def test_add_member_forbidden_for_plain_member(auth_client, member_group, test_project):
    resp = auth_client.post(f"/groups/{member_group}/members", json={"entity_id": test_project["id"]})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_remove_member(auth_client, app, owned_group, second_user):
    auth_client.post(f"/groups/{owned_group}/members", json={"entity_id": second_user["id"]})
    resp = auth_client.delete(f"/groups/{owned_group}/members/{second_user['id']}")
    assert resp.status_code == HTTPStatus.NO_CONTENT
    assert second_user["id"] not in _member_ids(app, owned_group)


def test_remove_owner_conflicts(auth_client, owned_group, test_user):
    resp = auth_client.delete(f"/groups/{owned_group}/members/{test_user['id']}")
    assert resp.status_code == HTTPStatus.CONFLICT


def test_remove_unknown_member_404(auth_client, owned_group, second_user):
    resp = auth_client.delete(f"/groups/{owned_group}/members/{second_user['id']}")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_remove_config_managed_membership_in_user_group_succeeds(auth_client, app, owned_group, second_user):
    """OAuth stamps config_managed on rule-matched memberships; an owner must still be able
    to curate their own group. The membership returns at that user's next login if the rule matches."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_member import GroupMember
        db.session.add(GroupMember(group_id=owned_group, entity_id=second_user["id"], config_managed=True))
        db.session.commit()
    resp = auth_client.delete(f"/groups/{owned_group}/members/{second_user['id']}")
    assert resp.status_code == HTTPStatus.NO_CONTENT
    assert second_user["id"] not in _member_ids(app, owned_group)


def test_remove_member_from_auto_join_group_rejected(admin_client, app, config_group, second_user):
    """Auto-join membership is rule-driven: not even admins remove members by hand."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_member import GroupMember
        db.session.add(GroupMember(group_id=config_group, entity_id=second_user["id"], config_managed=True))
        db.session.commit()
    resp = admin_client.delete(f"/groups/{config_group}/members/{second_user['id']}")
    assert resp.status_code == HTTPStatus.CONFLICT


# ---------------------------------------------------------------------------
# Ownership transfer
# ---------------------------------------------------------------------------

def test_transfer_to_existing_member(auth_client, app, owned_group, second_user):
    auth_client.post(f"/groups/{owned_group}/members", json={"entity_id": second_user["id"]})
    resp = auth_client.post(f"/groups/{owned_group}/owner", json={"entity_id": second_user["id"]})
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from lumen.models.group_member import get_group_owner
        assert get_group_owner(owned_group).id == second_user["id"]


def test_transfer_to_non_member_rejected(auth_client, app, owned_group, second_user):
    """Ownership is a promotion, not an invitation: the target must be a member."""
    resp = auth_client.post(f"/groups/{owned_group}/owner", json={"entity_id": second_user["id"]})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "must already be a member" in resp.get_json()["error"]
    assert second_user["id"] not in _member_ids(app, owned_group)


def test_transfer_to_current_owner_conflicts(auth_client, owned_group, test_user):
    resp = auth_client.post(f"/groups/{owned_group}/owner", json={"entity_id": test_user["id"]})
    assert resp.status_code == HTTPStatus.CONFLICT


def test_transfer_to_project_rejected(auth_client, owned_group, test_project):
    resp = auth_client.post(f"/groups/{owned_group}/owner", json={"entity_id": test_project["id"]})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_transfer_requires_entity_id(auth_client, owned_group):
    assert auth_client.post(f"/groups/{owned_group}/owner", json={}).status_code == HTTPStatus.BAD_REQUEST


def test_transfer_unknown_user_404(auth_client, owned_group):
    resp = auth_client.post(f"/groups/{owned_group}/owner", json={"entity_id": 999999})
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_transfer_forbidden_for_plain_member(auth_client, member_group, test_user):
    resp = auth_client.post(f"/groups/{member_group}/owner", json={"entity_id": test_user["id"]})
    assert resp.status_code == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------------------
# Model grants
# ---------------------------------------------------------------------------

def test_addable_models_only_lists_owned(auth_client, app, owned_group, test_model, test_user):
    assert auth_client.get(f"/groups/{owned_group}/models").get_json()["models"] == []
    with app.app_context():
        set_model_owner(test_model["id"], test_user["id"])
    models = auth_client.get(f"/groups/{owned_group}/models").get_json()["models"]
    assert [m["model_name"] for m in models] == ["test-model"]


def test_addable_models_excludes_already_granted(auth_client, app, owned_group, test_model, test_user):
    with app.app_context():
        set_model_owner(test_model["id"], test_user["id"])
    auth_client.post(f"/groups/{owned_group}/models", json={"model_config_id": test_model["id"]})
    assert auth_client.get(f"/groups/{owned_group}/models").get_json()["models"] == []


def test_add_model_grants_access(auth_client, app, owned_group, test_model, test_user):
    with app.app_context():
        set_model_owner(test_model["id"], test_user["id"])
    resp = auth_client.post(f"/groups/{owned_group}/models", json={"model_config_id": test_model["id"]})
    assert resp.status_code == HTTPStatus.CREATED


def test_add_model_not_owned_forbidden(auth_client, app, owned_group, test_model, second_user):
    with app.app_context():
        set_model_owner(test_model["id"], second_user["id"])
    resp = auth_client.post(f"/groups/{owned_group}/models", json={"model_config_id": test_model["id"]})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_add_unowned_model_rejected(auth_client, owned_group, test_model):
    """A model with no owner is public already, so granting it is meaningless."""
    resp = auth_client.post(f"/groups/{owned_group}/models", json={"model_config_id": test_model["id"]})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_admin_can_grant_any_owned_model(admin_client, app, owned_group, test_model, second_user):
    with app.app_context():
        set_model_owner(test_model["id"], second_user["id"])
    resp = admin_client.post(f"/groups/{owned_group}/models", json={"model_config_id": test_model["id"]})
    assert resp.status_code == HTTPStatus.CREATED


def test_add_model_duplicate_conflicts(auth_client, app, owned_group, test_model, test_user):
    with app.app_context():
        set_model_owner(test_model["id"], test_user["id"])
    auth_client.post(f"/groups/{owned_group}/models", json={"model_config_id": test_model["id"]})
    resp = auth_client.post(f"/groups/{owned_group}/models", json={"model_config_id": test_model["id"]})
    assert resp.status_code == HTTPStatus.CONFLICT


def test_add_model_unknown_404(auth_client, owned_group):
    resp = auth_client.post(f"/groups/{owned_group}/models", json={"model_config_id": 999999})
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_remove_model_grant(auth_client, app, owned_group, test_model, test_user):
    with app.app_context():
        set_model_owner(test_model["id"], test_user["id"])
    auth_client.post(f"/groups/{owned_group}/models", json={"model_config_id": test_model["id"]})
    resp = auth_client.delete(f"/groups/{owned_group}/models/{test_model['id']}")
    assert resp.status_code == HTTPStatus.NO_CONTENT
    assert auth_client.get(f"/groups/{owned_group}/models").get_json()["models"]


def test_remove_model_grant_unknown_404(auth_client, owned_group, test_model):
    resp = auth_client.delete(f"/groups/{owned_group}/models/{test_model['id']}")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_model_grants_forbidden_for_plain_member(auth_client, member_group):
    assert auth_client.get(f"/groups/{member_group}/models").status_code == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------------------
# End to end: a grant made through the UI really opens access
# ---------------------------------------------------------------------------

def test_user_member_gains_access_through_group(auth_client, app, owned_group, test_model,
                                                test_user, second_user):
    """A user granted a model via the group can use it; a non-member still cannot."""
    with app.app_context():
        set_model_owner(test_model["id"], test_user["id"])
    auth_client.post(f"/groups/{owned_group}/models", json={"model_config_id": test_model["id"]})
    auth_client.post(f"/groups/{owned_group}/members", json={"entity_id": second_user["id"]})

    with app.app_context():
        from lumen.services.llm import get_model_access_status
        assert get_model_access_status(second_user["id"], test_model["id"]) == "allowed"

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        outsider = Entity(entity_type="user", email="out@example.com", name="Outsider",
                          initials="OU", active=True)
        db.session.add(outsider)
        db.session.commit()
        from lumen.services.llm import get_model_access_status
        assert get_model_access_status(outsider.id, test_model["id"]) == "blocked"


def test_project_member_gains_access_and_pool_through_group(auth_client, app, owned_group,
                                                            test_model, test_user, test_project):
    """A project is itself the authenticating entity for API traffic, so a project member
    inherits both the group's model grants and its coin pool."""
    with app.app_context():
        set_model_owner(test_model["id"], test_user["id"])
        from lumen.extensions import db
        from lumen.models.group_limit import GroupLimit
        db.session.add(GroupLimit(group_id=owned_group, max_coins=30, refresh_coins=3, starting_coins=30))
        db.session.commit()
    auth_client.post(f"/groups/{owned_group}/models", json={"model_config_id": test_model["id"]})
    auth_client.post(f"/groups/{owned_group}/members", json={"entity_id": test_project["id"]})

    with app.app_context():
        from lumen.services.llm import get_model_access_status, get_pool_limit
        assert get_model_access_status(test_project["id"], test_model["id"]) == "allowed"
        assert float(get_pool_limit(test_project["id"]).max_coins) == 30.0


# ---------------------------------------------------------------------------
# auto-join groups: profile is editable (admin), membership is not
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,payload", [
    ("PATCH", "", {"name": "renamed", "description": "edited"}),
    ("POST", "/toggle", None),
])
def test_auto_join_group_profile_editable_by_admin(admin_client, config_group, method, path, payload):
    resp = admin_client.open(f"/groups/{config_group}{path}", method=method, json=payload)
    assert resp.status_code in (HTTPStatus.OK, HTTPStatus.NO_CONTENT), resp.get_json()


@pytest.mark.parametrize("method,path,payload", [
    ("POST", "/members", {"entity_id": 0}),
    ("POST", "/owner", {"entity_id": 0}),
])
def test_auto_join_group_membership_locked_even_for_admin(admin_client, config_group, second_user,
                                                          method, path, payload):
    payload = {"entity_id": second_user["id"]}
    resp = admin_client.open(f"/groups/{config_group}{path}", method=method, json=payload)
    assert resp.status_code == HTTPStatus.CONFLICT
    assert "auto-join rules" in resp.get_json()["error"]


def test_auto_join_group_admin_can_grant_models(admin_client, app, config_group, test_model, test_user):
    """Model grants are policy, not membership — still editable on auto groups."""
    with app.app_context():
        set_model_owner(test_model["id"], test_user["id"])
    resp = admin_client.post(f"/groups/{config_group}/models", json={"model_config_id": test_model["id"]})
    assert resp.status_code == HTTPStatus.CREATED
    resp = admin_client.delete(f"/groups/{config_group}/models/{test_model['id']}")
    assert resp.status_code == HTTPStatus.NO_CONTENT


# ---------------------------------------------------------------------------
# Ownerless groups withhold members and rolled-up stats from non-admins
# ---------------------------------------------------------------------------

@pytest.fixture
def ownerless_group(app, test_user, second_user):
    """A group test_user belongs to that nobody owns (e.g. created by a group rule)."""
    gid = _make_group(app, name="ownerless")
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_stat import EntityStat
        from lumen.models.group_member import GroupMember
        db.session.add(GroupMember(group_id=gid, entity_id=test_user["id"]))
        db.session.add(GroupMember(group_id=gid, entity_id=second_user["id"]))
        db.session.add(EntityStat(entity_id=test_user["id"], requests=7, input_tokens=10,
                                  output_tokens=5, cost=2.5))
        db.session.commit()
    return gid


def test_ownerless_group_hides_members_and_stats_in_list(auth_client, ownerless_group):
    row = next(g for g in auth_client.get("/groups/data").get_json()["groups"]
               if g["id"] == ownerless_group)
    assert row["has_owner"] is False
    assert row["members"] is None
    assert row["requests"] is None
    assert row["tokens"] is None
    assert row["cost"] is None
    # Name, active and the coin policy stay visible — they affect the member.
    assert row["name"] == "ownerless"
    assert row["active"] is True


def test_ownerless_group_shows_everything_to_admin(admin_client, ownerless_group):
    row = next(g for g in admin_client.get("/groups/data").get_json()["groups"]
               if g["id"] == ownerless_group)
    assert row["has_owner"] is False
    assert row["members"] == 2
    assert row["requests"] == 7
    assert row["tokens"] == 15
    assert row["cost"] == pytest.approx(2.5)


def test_owned_group_still_shows_stats_to_member(auth_client, app, member_group, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_stat import EntityStat
        db.session.add(EntityStat(entity_id=test_user["id"], requests=4, input_tokens=2,
                                  output_tokens=1, cost=0.5))
        db.session.commit()
    row = next(g for g in auth_client.get("/groups/data").get_json()["groups"]
               if g["id"] == member_group)
    assert row["has_owner"] is True
    assert row["members"] == 2
    assert row["requests"] == 4


def test_ownerless_group_members_api_forbidden_for_member(auth_client, ownerless_group):
    """The API must enforce this — not just the template."""
    resp = auth_client.get(f"/groups/{ownerless_group}/members/data")
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_ownerless_group_members_api_allowed_for_admin(admin_client, ownerless_group):
    resp = admin_client.get(f"/groups/{ownerless_group}/members/data")
    assert resp.status_code == HTTPStatus.OK
    assert resp.get_json()["total"] == 2


def test_ownerless_group_detail_hides_members_tab(auth_client, ownerless_group):
    resp = auth_client.get(f"/groups/{ownerless_group}")
    assert resp.status_code == HTTPStatus.OK
    assert b'id="tab-members"' not in resp.data
    assert b"has no owner" in resp.data
    # The models tab is still there — it tells the member what access they get.
    assert b'id="tab-models"' in resp.data


def test_ownerless_group_detail_shows_members_tab_to_admin(admin_client, ownerless_group):
    resp = admin_client.get(f"/groups/{ownerless_group}")
    assert b'id="tab-members"' in resp.data


def test_ownerless_group_excluded_from_summary_cards(auth_client, app, ownerless_group, test_user):
    """Withheld groups must not silently inflate the totals either."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_stat import EntityStat
        st = db.session.get(EntityStat, test_user["id"])
        st.requests = 7
        db.session.commit()
    # Total Members / Total Requests come only from owned groups (none here).
    assert _summary_cards(auth_client) == {"Total Members": 0, "Total Requests": 0}
    data = auth_client.get("/groups/data").get_json()
    assert all(g["members"] is None for g in data["groups"])


def test_summary_cards_count_ownerless_group_for_admin(admin_client, app, ownerless_group, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_stat import EntityStat
        st = db.session.get(EntityStat, test_user["id"])
        st.requests = 7
        db.session.commit()
    assert _summary_cards(admin_client) == {"Total Members": 2, "Total Requests": 7}


def test_transferring_ownership_reveals_stats(admin_client, app, ownerless_group, test_user):
    """Assigning an owner makes the withheld data visible to members again."""
    client = admin_client
    resp = client.post(f"/groups/{ownerless_group}/owner", json={"entity_id": test_user["id"]})
    assert resp.status_code == HTTPStatus.OK
    # test_user is the owner now, so as a plain member they see the numbers.
    with client.session_transaction() as sess:
        sess["entity_id"] = test_user["id"]
        sess.pop("admin_mode", None)
    row = next(g for g in client.get("/groups/data").get_json()["groups"]
               if g["id"] == ownerless_group)
    assert row["has_owner"] is True
    assert row["members"] == 2


# ---------------------------------------------------------------------------
# Review fixes: owner uniqueness, config_managed promotion, coin clamps, guards
# ---------------------------------------------------------------------------

def test_two_owner_rows_rejected_by_db(app, owned_group, second_user):
    """The partial unique index makes a concurrent double-transfer impossible
    to commit — without it, get_group_owner() raises for everyone afterwards."""
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_member import GroupMember
        db.session.add(GroupMember(group_id=owned_group, entity_id=second_user["id"], is_owner=True))
        with _pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_promoting_config_managed_member_clears_flag(auth_client, app, owned_group, second_user):
    """OAuth login reconciliation deletes config_managed rows whose rule no longer
    matches; an owner row must never be silently deletable that way."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_member import GroupMember
        db.session.add(GroupMember(group_id=owned_group, entity_id=second_user["id"], config_managed=True))
        db.session.commit()
    resp = auth_client.post(f"/groups/{owned_group}/owner", json={"entity_id": second_user["id"]})
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.group_member import GroupMember
        assoc = db.session.execute(
            select(GroupMember).filter_by(group_id=owned_group, entity_id=second_user["id"])
        ).scalar_one()
        assert assoc.is_owner is True
        assert assoc.config_managed is False


def test_create_rejects_numeric_zero_coins_from_non_admin(auth_client):
    """JSON 0 is falsy but is still an attempt to set a coin limit."""
    resp = auth_client.post("/groups", json={"name": "zero-coins", "max_coins": 0})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_add_inactive_member_rejected(auth_client, app, owned_group):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        e = Entity(entity_type="user", email="gone@example.com", name="Gone", initials="GG", active=False)
        db.session.add(e)
        db.session.commit()
        eid = e.id
    resp = auth_client.post(f"/groups/{owned_group}/members", json={"entity_id": eid})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def test_transfer_to_inactive_user_rejected(auth_client, app, owned_group):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        e = Entity(entity_type="user", email="gone2@example.com", name="Gone2", initials="GG", active=False)
        db.session.add(e)
        db.session.commit()
        eid = e.id
    resp = auth_client.post(f"/groups/{owned_group}/owner", json={"entity_id": eid})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


def _set_balance(app, entity_id, coins):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        db.session.add(EntityBalance(entity_id=entity_id, coins_left=coins))
        db.session.commit()


def _balance(app, entity_id):
    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        return float(db.session.execute(
            select(EntityBalance.coins_left).filter_by(entity_id=entity_id)
        ).scalar_one())


def test_lowering_group_max_clamps_member_balances(admin_client, app, owned_group, test_user):
    """A member spending against the group pool must not keep a balance above a
    lowered ceiling — the refill job only corrects downward when refresh > 0."""
    _set_balance(app, test_user["id"], 500)
    admin_client.patch(f"/groups/{owned_group}", json={"max_coins": "500", "refresh_coins": "0"})
    admin_client.patch(f"/groups/{owned_group}", json={"max_coins": "50", "refresh_coins": "0"})
    assert _balance(app, test_user["id"]) == 50.0


def test_lowering_group_max_ignores_members_with_own_limit(admin_client, app, owned_group, test_user):
    """A member with their own EntityLimit is not governed by the group pool."""
    _set_balance(app, test_user["id"], 500)
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(entity_id=test_user["id"], max_coins=1000, refresh_coins=1, starting_coins=1000))
        db.session.commit()
    admin_client.patch(f"/groups/{owned_group}", json={"max_coins": "50", "refresh_coins": "0"})
    assert _balance(app, test_user["id"]) == 500.0


def test_lowering_group_max_respects_better_pool_elsewhere(admin_client, app, owned_group, test_user):
    """A member of a second group with a higher pool is clamped to the best pool
    across their groups, not blindly to this group's new max."""
    _set_balance(app, test_user["id"], 500)
    other = _make_group(app, name="richer")
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_limit import GroupLimit
        from lumen.models.group_member import GroupMember
        db.session.add(GroupMember(group_id=other, entity_id=test_user["id"]))
        db.session.add(GroupLimit(group_id=other, max_coins=400, refresh_coins=1, starting_coins=400))
        db.session.commit()
    admin_client.patch(f"/groups/{owned_group}", json={"max_coins": "50", "refresh_coins": "0"})
    assert _balance(app, test_user["id"]) == 400.0


# ---------------------------------------------------------------------------
# Auto-join rules endpoint and create-with-rules
# ---------------------------------------------------------------------------

def _group_rules(app, gid):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        g = db.session.get(Group, gid)
        return g.auto_join, [(r.field, r.match, r.value) for r in g.rules]


def test_set_rules_admin_only(auth_client, owned_group):
    """Rules pull members in from login claims — org policy, not owner territory."""
    resp = auth_client.put(f"/groups/{owned_group}/rules",
                           json={"auto_join": True, "rules": [{"field": "idp", "match": "equals", "value": "x"}]})
    assert resp.status_code == HTTPStatus.FORBIDDEN


@pytest.fixture
def plain_group(app):
    """A group with no owner and no members — eligible for auto-join."""
    return _make_group(app, name="plain-group")


def test_set_rules_roundtrip(admin_client, app, plain_group):
    owned_group = plain_group
    resp = admin_client.put(f"/groups/{owned_group}/rules", json={
        "auto_join": True,
        "rules": [
            {"field": "affiliation", "match": "contains", "value": "staff@x.edu"},
            {"field": "idp", "match": "equals", "value": "urn:example"},
        ],
    })
    assert resp.status_code == HTTPStatus.OK
    assert _group_rules(app, owned_group) == (True, [
        ("affiliation", "contains", "staff@x.edu"),
        ("idp", "equals", "urn:example"),
    ])
    # Replace-all: saving a shorter list drops the rest.
    admin_client.put(f"/groups/{owned_group}/rules", json={
        "auto_join": True, "rules": [{"field": "idp", "match": "equals", "value": "urn:example"}],
    })
    assert _group_rules(app, owned_group) == (True, [("idp", "equals", "urn:example")])


def test_set_rules_auto_requires_a_rule(admin_client, owned_group):
    resp = admin_client.put(f"/groups/{owned_group}/rules", json={"auto_join": True, "rules": []})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "at least one rule" in resp.get_json()["error"]


def test_set_rules_off_keeps_rules_dormant(admin_client, app, plain_group):
    owned_group = plain_group
    admin_client.put(f"/groups/{owned_group}/rules", json={
        "auto_join": True, "rules": [{"field": "idp", "match": "equals", "value": "x"}]})
    resp = admin_client.put(f"/groups/{owned_group}/rules", json={
        "auto_join": False, "rules": [{"field": "idp", "match": "equals", "value": "x"}]})
    assert resp.status_code == HTTPStatus.OK
    assert _group_rules(app, owned_group) == (False, [("idp", "equals", "x")])


@pytest.mark.parametrize("bad_rule,msg", [
    ({"field": "", "match": "contains", "value": "x"}, "field"),
    ({"field": "idp", "match": "regex", "value": "x"}, "match"),
    ({"field": "idp", "match": "contains", "value": ""}, "value"),
])
def test_set_rules_validation(admin_client, owned_group, bad_rule, msg):
    resp = admin_client.put(f"/groups/{owned_group}/rules", json={"auto_join": True, "rules": [bad_rule]})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert msg in resp.get_json()["error"]


def test_admin_create_with_auto_join_rules(admin_client, app):
    resp = admin_client.post("/groups", json={
        "name": "auto-made", "auto_join": True,
        "rules": [{"field": "affiliation", "match": "contains", "value": "student@x.edu"}],
    })
    assert resp.status_code == HTTPStatus.CREATED
    assert _group_rules(app, resp.get_json()["id"]) == (
        True, [("affiliation", "contains", "student@x.edu")])


def test_admin_create_auto_join_without_rules_rejected(admin_client):
    resp = admin_client.post("/groups", json={"name": "auto-empty", "auto_join": True, "rules": []})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "at least one rule" in resp.get_json()["error"]


def test_non_admin_create_with_auto_join_rejected(auth_client):
    resp = auth_client.post("/groups", json={
        "name": "sneaky", "auto_join": True,
        "rules": [{"field": "idp", "match": "equals", "value": "x"}],
    })
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_rules_created_via_api_assign_members_at_login(admin_client, app, second_user):
    """End to end: a rule saved through the UI endpoint really assigns at login."""
    gid = admin_client.post("/groups", json={
        "name": "rule-e2e", "auto_join": True,
        "rules": [{"field": "eppn", "match": "contains", "value": "@illinois.edu"}],
    }).get_json()["id"]
    with app.app_context():
        from lumen.blueprints.auth.routes import sync_auto_memberships
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.group_member import GroupMember
        entity = db.session.get(Entity, second_user["id"])
        sync_auto_memberships(entity, userinfo={"eppn": "sec@illinois.edu"})
        db.session.commit()
        from sqlalchemy import select
        assert db.session.execute(
            select(GroupMember).filter_by(group_id=gid, entity_id=second_user["id"])
        ).scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Lost uniqueness races surface as 409, not 500
# ---------------------------------------------------------------------------

def _shadow_session_method(name, exc):
    """Temporarily shadow a scoped-session method so it raises exc.

    monkeypatch.setattr is unsafe here: restoring would pin a stale bound
    method onto the scoped_session proxy. Deleting the shadow attribute
    restores normal delegation instead.
    """
    import contextlib

    from lumen.extensions import db

    @contextlib.contextmanager
    def _cm():
        def _raise(*args, **kwargs):
            raise exc
        setattr(db.session, name, _raise)
        try:
            yield
        finally:
            delattr(db.session, name)
    return _cm()


def test_create_group_lost_name_race_returns_409(auth_client):
    """The duplicate-name SELECT can lose a race with a concurrent insert; the
    unique constraint then raises IntegrityError, which must map to 409."""
    from sqlalchemy.exc import IntegrityError

    exc = IntegrityError("INSERT INTO groups", {}, Exception("UNIQUE constraint failed: groups.name"))
    with _shadow_session_method("flush", exc):
        resp = auth_client.post("/groups", json={"name": "raced"})
    assert resp.status_code == HTTPStatus.CONFLICT
    assert "already exists" in resp.get_json()["error"]


def test_add_member_lost_race_returns_409(auth_client, owned_group, second_user):
    from sqlalchemy.exc import IntegrityError

    exc = IntegrityError("INSERT INTO group_members", {}, Exception("UNIQUE constraint failed"))
    with _shadow_session_method("commit", exc):
        resp = auth_client.post(f"/groups/{owned_group}/members", json={"entity_id": second_user["id"]})
    assert resp.status_code == HTTPStatus.CONFLICT
    assert "Already a member" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# auto-join exclusivity: no owner, no manual members
# ---------------------------------------------------------------------------

def test_enable_auto_join_rejected_when_group_has_owner(admin_client, owned_group):
    resp = admin_client.put(f"/groups/{owned_group}/rules", json={
        "auto_join": True, "rules": [{"field": "idp", "match": "equals", "value": "x"}],
    })
    assert resp.status_code == HTTPStatus.CONFLICT
    assert "cannot have an owner" in resp.get_json()["error"]


def test_enable_auto_join_rejected_with_manual_members(admin_client, app, plain_group, second_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_member import GroupMember
        db.session.add(GroupMember(group_id=plain_group, entity_id=second_user["id"], config_managed=False))
        db.session.commit()
    resp = admin_client.put(f"/groups/{plain_group}/rules", json={
        "auto_join": True, "rules": [{"field": "idp", "match": "equals", "value": "x"}],
    })
    assert resp.status_code == HTTPStatus.CONFLICT
    assert "manually added members" in resp.get_json()["error"]


def test_enable_auto_join_allowed_with_only_auto_members(admin_client, app, plain_group, second_user):
    """Rule-assigned (config_managed) members don't block enabling auto-join —
    they are exactly what the reconciler manages."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_member import GroupMember
        db.session.add(GroupMember(group_id=plain_group, entity_id=second_user["id"], config_managed=True))
        db.session.commit()
    resp = admin_client.put(f"/groups/{plain_group}/rules", json={
        "auto_join": True, "rules": [{"field": "idp", "match": "equals", "value": "x"}],
    })
    assert resp.status_code == HTTPStatus.OK


def test_create_auto_join_with_owner_rejected(admin_client, second_user):
    resp = admin_client.post("/groups", json={
        "name": "auto-owned", "owner_email": second_user["email"],
        "auto_join": True, "rules": [{"field": "idp", "match": "equals", "value": "x"}],
    })
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "cannot have an owner" in resp.get_json()["error"]


def test_disable_auto_join_then_manage_manually(admin_client, app, config_group, second_user):
    """Turning auto-join off converts the group to manual management: members
    can then be added and ownership assigned."""
    resp = admin_client.put(f"/groups/{config_group}/rules", json={"auto_join": False, "rules": []})
    assert resp.status_code == HTTPStatus.OK
    resp = admin_client.post(f"/groups/{config_group}/members", json={"entity_id": second_user["id"]})
    assert resp.status_code == HTTPStatus.CREATED
    resp = admin_client.post(f"/groups/{config_group}/owner", json={"entity_id": second_user["id"]})
    assert resp.status_code == HTTPStatus.OK


# ---------------------------------------------------------------------------
# Change Owner dialog: candidate search
# ---------------------------------------------------------------------------

def test_owner_search_returns_only_user_members(auth_client, owned_group, second_user, test_project):
    auth_client.post(f"/groups/{owned_group}/members", json={"entity_id": second_user["id"]})
    auth_client.post(f"/groups/{owned_group}/members", json={"entity_id": test_project["id"]})
    hits = auth_client.get(f"/groups/{owned_group}/owner/search?q=Se").get_json()["entities"]
    assert [e["id"] for e in hits] == [second_user["id"]]
    # A project member never qualifies, however well it matches.
    hits = auth_client.get(f"/groups/{owned_group}/owner/search?q=Research").get_json()["entities"]
    assert hits == []


def test_owner_search_excludes_current_owner_and_non_members(auth_client, owned_group, test_user, second_user):
    # The current owner matches "Test User" but must not be offered.
    hits = auth_client.get(f"/groups/{owned_group}/owner/search?q=Test").get_json()["entities"]
    assert all(e["id"] != test_user["id"] for e in hits)
    # second_user is not a member at all, so they never appear either.
    hits = auth_client.get(f"/groups/{owned_group}/owner/search?q=Second").get_json()["entities"]
    assert hits == []


def test_owner_search_requires_group_admin(auth_client, member_group):
    resp = auth_client.get(f"/groups/{member_group}/owner/search?q=Se")
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_owner_search_rejected_on_auto_join_group(admin_client, config_group):
    resp = admin_client.get(f"/groups/{config_group}/owner/search?q=Te")
    assert resp.status_code == HTTPStatus.CONFLICT


# ---------------------------------------------------------------------------
# joined_at: when did each member join
# ---------------------------------------------------------------------------

def test_members_data_includes_joined_timestamp(auth_client, app, owned_group, second_user):
    auth_client.post(f"/groups/{owned_group}/members", json={"entity_id": second_user["id"]})
    rows = auth_client.get(f"/groups/{owned_group}/members/data?sort=joined&order=desc").get_json()["members"]
    by_id = {m["id"]: m for m in rows}
    assert by_id[second_user["id"]]["joined"] is not None
    # ISO-Z shape the frontend expects
    assert by_id[second_user["id"]]["joined"].endswith("Z")


def test_members_data_joined_null_for_legacy_rows(auth_client, app, owned_group, second_user):
    """Rows that predate the column have no join time; the API says null, the UI shows a dash."""
    with app.app_context():
        from sqlalchemy import update

        from lumen.extensions import db
        from lumen.models.group_member import GroupMember
        db.session.add(GroupMember(group_id=owned_group, entity_id=second_user["id"]))
        db.session.commit()
        # Passing joined_at=None would still trigger the column default at
        # flush; null it out afterwards, as a pre-migration row would be.
        db.session.execute(
            update(GroupMember)
            .where(GroupMember.group_id == owned_group, GroupMember.entity_id == second_user["id"])
            .values(joined_at=None)
        )
        db.session.commit()
    rows = auth_client.get(f"/groups/{owned_group}/members/data").get_json()["members"]
    by_id = {m["id"]: m for m in rows}
    assert by_id[second_user["id"]]["joined"] is None


def test_members_data_sorts_by_joined(auth_client, app, owned_group, second_user, test_project):
    from datetime import datetime
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_member import GroupMember
        db.session.add(GroupMember(group_id=owned_group, entity_id=second_user["id"],
                                   joined_at=datetime(2026, 1, 1)))
        db.session.add(GroupMember(group_id=owned_group, entity_id=test_project["id"],
                                   joined_at=datetime(2026, 6, 1)))
        db.session.commit()
    rows = auth_client.get(f"/groups/{owned_group}/members/data?sort=joined&order=asc").get_json()["members"]
    joined = [m["joined"] for m in rows if m["joined"]]
    assert joined == sorted(joined)


# ---------------------------------------------------------------------------
# Review round 2: roster freeze, transfer ordering
# ---------------------------------------------------------------------------

def test_disable_auto_join_freezes_roster(admin_client, app, config_group, test_user):
    """Turning auto-join off converts auto memberships to manual so the login
    reconciler cannot drain the group one sign-in at a time."""
    resp = admin_client.put(f"/groups/{config_group}/rules", json={"auto_join": False, "rules": []})
    assert resp.status_code == HTTPStatus.OK
    with app.app_context():
        from sqlalchemy import select

        from lumen.blueprints.auth.routes import sync_auto_memberships
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.group_member import GroupMember
        assoc = db.session.execute(
            select(GroupMember).filter_by(group_id=config_group, entity_id=test_user["id"])
        ).scalar_one()
        assert assoc.config_managed is False
        # A later login must NOT remove the frozen membership.
        entity = db.session.get(Entity, test_user["id"])
        sync_auto_memberships(entity, userinfo={"affiliation": "nothing-matches"})
        db.session.commit()
        assert db.session.execute(
            select(GroupMember).filter_by(group_id=config_group, entity_id=test_user["id"])
        ).scalar_one_or_none() is not None


def test_transfer_to_member_with_lower_row_id(auth_client, app, second_user):
    """The demote must flush before the promote: SQLAlchemy updates rows in PK
    order, so a new owner with a LOWER id than the current owner transiently
    violated the partial unique index and 409'd every such transfer."""
    gid = _make_group(app, name="ordering")
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_member import GroupMember
        # Member row first (lower id), owner row second (higher id).
        db.session.add(GroupMember(group_id=gid, entity_id=second_user["id"]))
        db.session.commit()
    # test_user becomes owner via a later (higher-id) row
    from tests.conftest import make_group_with_member  # noqa: F401  (documentational)
    with app.app_context():
        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.entity import Entity
        from lumen.models.group_member import GroupMember
        me = db.session.execute(select(Entity).filter_by(email="testuser@example.com")).scalar_one()
        db.session.add(GroupMember(group_id=gid, entity_id=me.id, is_owner=True))
        db.session.commit()
    resp = auth_client.post(f"/groups/{gid}/owner", json={"entity_id": second_user["id"]})
    assert resp.status_code == HTTPStatus.OK, resp.get_json()


def test_clearing_group_pool_clamps_to_remaining_pool(admin_client, app, owned_group, test_user):
    """Blank Max Coins deletes the pool — members must be clamped to whatever
    ceiling remains, not left holding the old, higher balance."""
    _set_balance(app, test_user["id"], 500)
    other = _make_group(app, name="fallback-pool")
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group_limit import GroupLimit
        from lumen.models.group_member import GroupMember
        db.session.add(GroupMember(group_id=other, entity_id=test_user["id"]))
        db.session.add(GroupLimit(group_id=other, max_coins=100, refresh_coins=1, starting_coins=100))
        db.session.commit()
    admin_client.patch(f"/groups/{owned_group}", json={"max_coins": "500", "refresh_coins": "0"})
    _set_balance_update(app, test_user["id"], 500)
    admin_client.patch(f"/groups/{owned_group}", json={"max_coins": "", "refresh_coins": ""})
    assert _balance(app, test_user["id"]) == 100.0


def _set_balance_update(app, entity_id, coins):
    with app.app_context():
        from sqlalchemy import update

        from lumen.extensions import db
        from lumen.models.entity_balance import EntityBalance
        db.session.execute(update(EntityBalance).where(EntityBalance.entity_id == entity_id)
                           .values(coins_left=coins))
        db.session.commit()


def test_clearing_group_pool_with_no_fallback_blocks_spending(admin_client, app, owned_group, test_user):
    """The test config has no defaults.tokens, so with the group pool gone the
    member has NO pool: the stale balance is inert because get_pool_limit is
    None and spending is refused outright — no clamp needed."""
    _set_balance(app, test_user["id"], 500)
    admin_client.patch(f"/groups/{owned_group}", json={"max_coins": "500", "refresh_coins": "0"})
    _set_balance_update(app, test_user["id"], 500)
    admin_client.patch(f"/groups/{owned_group}", json={"max_coins": "", "refresh_coins": ""})
    with app.app_context():
        from lumen.services.llm import get_pool_limit
        assert get_pool_limit(test_user["id"]) is None


def test_clearing_group_pool_clamps_to_token_defaults(admin_client, app, owned_group, test_user, monkeypatch):
    """When defaults.tokens provides a fallback ceiling, clearing the group
    pool clamps member balances down to it."""
    _set_balance(app, test_user["id"], 500)
    admin_client.patch(f"/groups/{owned_group}", json={"max_coins": "500", "refresh_coins": "0"})
    _set_balance_update(app, test_user["id"], 500)
    app.config["TOKEN_DEFAULTS"] = {"max": 50, "refresh": 0, "starting": 50}
    try:
        admin_client.patch(f"/groups/{owned_group}", json={"max_coins": "", "refresh_coins": ""})
    finally:
        app.config["TOKEN_DEFAULTS"] = {"max": 0, "refresh": 0, "starting": 0}
    assert _balance(app, test_user["id"]) == 50.0
