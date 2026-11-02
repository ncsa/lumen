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


def test_connect_where_to_put_this_file_above_config(client):
    """The "Where to put this file" note precedes the generated OpenCode config."""
    resp = client.get("/connect")
    assert resp.status_code == HTTPStatus.OK
    body = resp.data.decode()
    put_idx = body.index("Where to put this file:")
    examples_idx = body.index('id="examples-opencode"')
    assert put_idx < examples_idx


def test_connect_lists_accessible_model(auth_client, test_model):
    resp = auth_client.get("/connect")
    assert resp.status_code == HTTPStatus.OK
    ids = [m["id"] for m in _connect_data(resp.data)["models"]]
    assert test_model["model_name"] in ids


def test_connect_surfaces_model_limits_and_cost(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        db.session.add(ModelConfig(
            model_name="priced-model",
            context_window=131072,
            max_output_tokens=32768,
            input_cost_per_million=5.0,
            output_cost_per_million=15.0,
            cached_input_cost_per_million=0.5,
        ))
        db.session.commit()

    rows = {r["id"]: r for r in _connect_data(auth_client.get("/connect").data)["models"]}
    priced = rows["priced-model"]
    assert priced["context_window"] == 131072
    assert priced["max_output_tokens"] == 32768
    assert priced["input_cost_per_million"] == 5.0
    assert priced["output_cost_per_million"] == 15.0
    assert priced["cached_input_cost_per_million"] == 0.5


def test_connect_leaves_an_unset_cached_rate_null(auth_client, test_model):
    """Null is published as null, not resolved here: the generated OpenCode
    config is what turns it into the input rate, and a client must be able to
    tell "no separate rate" from a deliberate 0 (free cached input)."""
    rows = {r["id"]: r for r in _connect_data(auth_client.get("/connect").data)["models"]}
    assert rows[test_model["model_name"]]["cached_input_cost_per_million"] is None


def test_connect_excludes_blocked_model(app, auth_client, test_model, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        from tests.conftest import set_model_owner
        owner = Entity(entity_type="user", email="owner@example.com", name="Owner", active=True)
        db.session.add(owner)
        db.session.commit()
        set_model_owner(test_model["id"], owner.id)

    resp = auth_client.get("/connect")
    ids = [m["id"] for m in _connect_data(resp.data)["models"]]
    assert test_model["model_name"] not in ids


def test_connect_preselects_valid_model(auth_client, test_model):
    resp = auth_client.get("/connect", query_string={"model": test_model["model_name"]})
    assert _connect_data(resp.data)["selected"] == test_model["model_name"]


def test_connect_ignores_unknown_model(auth_client, test_model):
    resp = auth_client.get("/connect", query_string={"model": "does-not-exist"})
    assert _connect_data(resp.data)["selected"] == ""
