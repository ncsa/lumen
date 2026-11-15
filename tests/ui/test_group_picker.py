"""The Edit User dialog's group picker (static/js/group-pills.js) by group uuid.

Runs in a real browser, served by the Flask test client at http://lumen.test
like test_copy_id_avatar.py, on an admin's view of another user's profile.
"""
import json
from http import HTTPStatus
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from tests.ui.test_key_tables import BOOTSTRAP_JS, BOOTSTRAP_JS_URL, ORIGIN


@pytest.fixture
def groups(app, test_user):
    """test_user is in "keep" and "drop"; "join" is open. Returns {name: uuid string}."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        out = {}
        for name, member in (("keep", True), ("drop", True), ("join", False)):
            g = Group(name=name, active=True)
            db.session.add(g)
            db.session.flush()
            if member:
                db.session.add(GroupMember(group_id=g.id, entity_id=test_user["id"]))
            out[name] = str(g.uuid)
        db.session.commit()
        return out


def _member_group_uuids(app, user_id):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        return {str(u) for u in db.session.execute(
            select(Group.uuid).join(GroupMember, GroupMember.group_id == Group.id)
            .where(GroupMember.entity_id == user_id)
        ).scalars().all()}


@pytest.fixture
def profile_page(admin_client, browser, test_user, groups):
    patched = []

    def handle(route, request):
        url = urlsplit(request.url)
        if request.url == BOOTSTRAP_JS_URL:
            route.fulfill(status=200, content_type="application/javascript",
                          headers={"Access-Control-Allow-Origin": "*"}, body=BOOTSTRAP_JS.read_bytes())
            return
        if f"{url.scheme}://{url.netloc}" != ORIGIN:
            route.abort()
            return
        path = url.path + (f"?{url.query}" if url.query else "")
        if request.method == "PATCH":
            patched.append(request.post_data_json)
            resp = admin_client.patch(path, json=request.post_data_json)
        else:
            resp = admin_client.get(path)
        # Close the response: a static file stays open until it is.
        with resp:
            route.fulfill(status=resp.status_code, content_type=resp.content_type, body=resp.get_data())

    page = browser.new_page()
    page.set_default_timeout(10_000)
    page.route("**/*", handle)
    resp = page.goto(f"{ORIGIN}/admin/users/{test_user['uuid']}/profile")
    assert resp.status == HTTPStatus.OK
    page.patched = patched
    yield page
    page.close()


def test_picker_embeds_group_uuids_not_integer_ids(admin_client, test_user, groups):
    html = admin_client.get(f"/admin/users/{test_user['uuid']}/profile").get_data(as_text=True)
    for guuid in groups.values():
        assert json.dumps(guuid) in html


def test_edit_user_adds_and_removes_groups_by_uuid(app, profile_page, test_user, groups):
    page = profile_page
    page.get_by_role("button", name="Edit").first.click()
    page.wait_for_selector("#editUserModal.show")
    page.get_by_role("button", name="Remove group drop").click()
    page.fill("#edit-user-groups", "joi")
    page.keyboard.press("Enter")
    page.wait_for_selector("#edit-user-groups-pills li:has-text('join')")
    with page.expect_navigation():
        page.click("#edit-user-save-btn")
    assert len(page.patched) == 1
    body = page.patched[0]
    assert body["add_group_ids"] == [groups["join"]]
    assert body["remove_group_ids"] == [groups["drop"]]
    assert _member_group_uuids(app, test_user["id"]) == {groups["keep"], groups["join"]}
