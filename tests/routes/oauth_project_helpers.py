"""Shared setup for the OAuth project-key tests (device, code and consent page)."""
import secrets

import bs4
from sqlalchemy import delete, select

from lumen.blueprints.oauth import routes as oauth_routes
from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.models.auth_request import AuthRequest
from lumen.models.entity import Entity
from lumen.models.entity_manager import EntityManager
from lumen.services.crypto import hash_api_key
from tests.conftest import make_project


def project_with_member(app, name, member_id=None, role="manager", **fields):
    """A project with ``member_id`` added in ``role`` (None: no member); returns (id, uuid string)."""
    with app.app_context():
        project = make_project(name, **fields)
        if member_id is not None:
            db.session.add(EntityManager(user_entity_id=member_id, project_entity_id=project.id, role=role))
            db.session.commit()
        return project.id, str(project.uuid)


def add_member(app, project_id, user_id, role="user"):
    with app.app_context():
        db.session.add(EntityManager(user_entity_id=user_id, project_entity_id=project_id, role=role))
        db.session.commit()


def remove_member(app, project_id, user_id):
    with app.app_context():
        db.session.execute(delete(EntityManager).where(
            EntityManager.project_entity_id == project_id, EntityManager.user_entity_id == user_id))
        db.session.commit()


def new_user(app, email):
    with app.app_context():
        user = Entity(entity_type="user", email=email, name=email, initials="NU", active=True)
        db.session.add(user)
        db.session.commit()
        return user.id


def project_key(app, project_id, creator_id, name):
    """An active normal key in the project created by ``creator_id``; returns its id."""
    raw = "sk_" + secrets.token_urlsafe(32)
    with app.app_context():
        key = APIKey(entity_id=project_id, created_by_entity_id=creator_id, name=name,
                     key_hash=hash_api_key(raw), key_hint=f"{raw[:7]}...{raw[-4:]}")
        db.session.add(key)
        db.session.commit()
        return key.id


def active_keys(app, entity_id):
    """(name, created_by_entity_id) of the entity's active keys, sorted."""
    with app.app_context():
        return sorted(
            (k.name, k.created_by_entity_id) for k in db.session.execute(
                select(APIKey).where(APIKey.entity_id == entity_id, APIKey.revoked_at.is_(None))
            ).scalars()
        )


def access_reason(project):
    return (f"the approving account is not a member of project '{project}', "
            "or the project does not exist or is inactive")


def one_key_reason(project):
    return (f"members with the user role can hold one key in project '{project}'; "
            "request it with the name of your existing key to replace it")


def error_message(page) -> str:
    """The message text of an oauth_error.html page."""
    soup = bs4.BeautifulSoup(page.get_data(as_text=True), "html.parser")
    return soup.select_one("h1 + p").get_text()


def delete_project_during_mint(monkeypatch, project_id):
    """Delete the project, its memberships and its requests once _mint has loaded the request.

    Without autoflush, so a change the route made to the request beforehand is
    still pending, as when a concurrent delete commits between the two.
    """
    real = oauth_routes._project_key_denial

    def deleted_meanwhile(*args, **kwargs):
        with db.session.no_autoflush:
            db.session.execute(delete(EntityManager).where(EntityManager.project_entity_id == project_id))
            db.session.execute(delete(AuthRequest).where(AuthRequest.project_entity_id == project_id))
            db.session.execute(delete(Entity).where(Entity.id == project_id))
        return real(*args, **kwargs)

    monkeypatch.setattr(oauth_routes, "_project_key_denial", deleted_meanwhile)


def consent_form(page) -> dict:
    """The hidden fields of a rendered consent form (request id, and the project tag for project requests)."""
    soup = bs4.BeautifulSoup(page.get_data(as_text=True), "html.parser")
    return {i["name"]: i["value"] for i in soup.select('form[action$="/oauth/consent"] input[type=hidden]')
            if i["name"] != "csrf_token"}
