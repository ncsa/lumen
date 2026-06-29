import json
from http import HTTPStatus

from bs4 import BeautifulSoup


def _connect_data(html_bytes):
    soup = BeautifulSoup(html_bytes, "html.parser")
    return json.loads(soup.find("script", id="connect-data").string)


def test_connect_logged_out_is_generic(client):
    resp = client.get("/connect")
    assert resp.status_code == HTTPStatus.OK
    data = _connect_data(resp.data)
    assert data["models"] == []
    assert data["selected"] == ""
    assert data["base_url"].endswith("/v1")
    # generic placeholder advertised in the body
    assert b"MODEL" in resp.data


def test_connect_lists_accessible_model(auth_client, test_model):
    resp = auth_client.get("/connect")
    assert resp.status_code == HTTPStatus.OK
    ids = [m["id"] for m in _connect_data(resp.data)["models"]]
    assert test_model["model_name"] in ids


def test_connect_excludes_blocked_model(app, auth_client, test_model, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"],
            model_config_id=test_model["id"],
            access_type="blocked",
        ))
        db.session.commit()

    resp = auth_client.get("/connect")
    ids = [m["id"] for m in _connect_data(resp.data)["models"]]
    assert test_model["model_name"] not in ids


def test_connect_preselects_valid_model(auth_client, test_model):
    resp = auth_client.get("/connect", query_string={"model": test_model["model_name"]})
    assert _connect_data(resp.data)["selected"] == test_model["model_name"]


def test_connect_ignores_unknown_model(auth_client, test_model):
    resp = auth_client.get("/connect", query_string={"model": "does-not-exist"})
    assert _connect_data(resp.data)["selected"] == ""
