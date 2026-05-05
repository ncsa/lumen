from bs4 import BeautifulSoup


def test_accessible_model_present_in_table(auth_client, test_model):
    resp = auth_client.get("/models")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.data, "html.parser")
    links = [a.get_text(strip=True) for a in soup.find_all("a")]
    assert test_model["model_name"] in links


def test_blocked_model_absent_from_table(app, auth_client, test_model, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        from lumen.models.group_model_access import GroupModelAccess
        group = Group(name="test-group")
        db.session.add(group)
        db.session.flush()
        db.session.add(GroupMember(entity_id=test_user["id"], group_id=group.id))
        db.session.add(GroupModelAccess(group_id=group.id, model_config_id=test_model["id"], access_type="blacklist"))
        db.session.commit()

    resp = auth_client.get("/models")
    soup = BeautifulSoup(resp.data, "html.parser")
    cell_text = " ".join(td.get_text(strip=True) for td in soup.find_all("td"))
    assert test_model["model_name"] not in cell_text


def test_no_models_shows_empty_message(auth_client):
    resp = auth_client.get("/models")
    assert resp.status_code == 200
    assert b"No models configured." in resp.data


def test_multiple_models_all_shown(app, auth_client, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        m2 = ModelConfig(model_name="second-model", input_cost_per_million=1.0, output_cost_per_million=1.0, active=True)
        db.session.add(m2)
        db.session.commit()

    resp = auth_client.get("/models")
    soup = BeautifulSoup(resp.data, "html.parser")
    links = [a.get_text(strip=True) for a in soup.find_all("a")]
    assert test_model["model_name"] in links
    assert "second-model" in links


def test_user_blocked_model_absent(app, auth_client, test_model, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"],
            model_config_id=test_model["id"],
            access_type="blacklist",
        ))
        db.session.commit()

    resp = auth_client.get("/models")
    soup = BeautifulSoup(resp.data, "html.parser")
    links = [a.get_text(strip=True) for a in soup.find_all("a")]
    assert test_model["model_name"] not in links


def test_graylist_model_visible_without_consent(app, auth_client, test_model, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_access import EntityModelAccess
        db.session.add(EntityModelAccess(
            entity_id=test_user["id"],
            model_config_id=test_model["id"],
            access_type="graylist",
        ))
        db.session.commit()

    resp = auth_client.get("/models")
    soup = BeautifulSoup(resp.data, "html.parser")
    links = [a.get_text(strip=True) for a in soup.find_all("a")]
    assert test_model["model_name"] in links


def test_group_graylist_model_visible_without_consent(app, auth_client, test_model, test_user):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        from lumen.models.group_member import GroupMember
        from lumen.models.group_model_access import GroupModelAccess
        group = Group(name="test-group")
        db.session.add(group)
        db.session.flush()
        db.session.add(GroupMember(entity_id=test_user["id"], group_id=group.id))
        db.session.add(GroupModelAccess(group_id=group.id, model_config_id=test_model["id"], access_type="graylist"))
        db.session.commit()

    resp = auth_client.get("/models")
    soup = BeautifulSoup(resp.data, "html.parser")
    links = [a.get_text(strip=True) for a in soup.find_all("a")]
    assert test_model["model_name"] in links
