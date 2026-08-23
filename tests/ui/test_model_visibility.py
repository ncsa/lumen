from http import HTTPStatus

from bs4 import BeautifulSoup


def test_accessible_model_present_in_table(auth_client, test_model):
    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    soup = BeautifulSoup(resp.data, "html.parser")
    links = [a.get_text(strip=True) for a in soup.find_all("a")]
    assert test_model["model_name"] in links


def _make_owner(app, email="owner@example.com"):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        owner = Entity(entity_type="user", email=email, name="Owner", active=True)
        db.session.add(owner)
        db.session.commit()
        return owner.id


def test_blocked_model_absent_from_table(app, auth_client, test_model, test_user):
    """A model owned by another user (no grant) does not appear in the table."""
    from tests.conftest import set_model_owner
    with app.app_context():
        set_model_owner(test_model["id"], _make_owner(app))

    resp = auth_client.get("/models")
    soup = BeautifulSoup(resp.data, "html.parser")
    cell_text = " ".join(td.get_text(strip=True) for td in soup.find_all("td"))
    assert test_model["model_name"] not in cell_text


def test_no_models_shows_empty_message(auth_client):
    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    assert b"No models configured." in resp.data


def test_multiple_models_all_shown(app, auth_client, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        m2 = ModelConfig(model_name="second-model", input_cost_per_million=1.0, output_cost_per_million=1.0)
        db.session.add(m2)
        db.session.commit()

    resp = auth_client.get("/models")
    soup = BeautifulSoup(resp.data, "html.parser")
    links = [a.get_text(strip=True) for a in soup.find_all("a")]
    assert test_model["model_name"] in links
    assert "second-model" in links


def test_non_member_blocked_model_absent(app, auth_client, test_model, test_user):
    """An owned model granted to a group the user is not in stays hidden."""
    from tests.conftest import grant_model_to_group, set_model_owner
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.group import Group
        owner_id = _make_owner(app)
        set_model_owner(test_model["id"], owner_id)
        group = Group(name="other-group")
        db.session.add(group)
        db.session.commit()
        grant_model_to_group(test_model["id"], group.id)

    resp = auth_client.get("/models")
    soup = BeautifulSoup(resp.data, "html.parser")
    links = [a.get_text(strip=True) for a in soup.find_all("a")]
    assert test_model["model_name"] not in links


def test_needs_ack_model_visible_without_consent(app, auth_client, test_model, test_user):
    # needs_ack is a model-level property: the model stays visible so the user can acknowledge it.
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        db.session.get(ModelConfig, test_model["id"]).needs_ack = True
        db.session.commit()

    resp = auth_client.get("/models")
    soup = BeautifulSoup(resp.data, "html.parser")
    links = [a.get_text(strip=True) for a in soup.find_all("a")]
    assert test_model["model_name"] in links


def test_needs_ack_model_not_overridable_by_group(app, auth_client, test_model, test_user):
    # A group grant on an owned model cannot remove its acknowledgement requirement.
    from tests.conftest import grant_model_to_group, make_group_with_member, set_model_owner
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        db.session.get(ModelConfig, test_model["id"]).needs_ack = True
        db.session.commit()
        owner_id = _make_owner(app)
        set_model_owner(test_model["id"], owner_id)
        group_id = make_group_with_member(test_user["id"])
        grant_model_to_group(test_model["id"], group_id)

        from lumen.services.llm import get_model_access_status
        assert get_model_access_status(test_user["id"], test_model["id"]) == "needs_ack"

    resp = auth_client.get("/models")
    soup = BeautifulSoup(resp.data, "html.parser")
    links = [a.get_text(strip=True) for a in soup.find_all("a")]
    assert test_model["model_name"] in links


# ---------------------------------------------------------------------------
# early_access badge and end_date visibility
# ---------------------------------------------------------------------------

def _set_model(app, model_id, **attrs):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        mc = db.session.get(ModelConfig, model_id)
        for k, v in attrs.items():
            setattr(mc, k, v)
        db.session.commit()


def test_early_access_badge_on_models_list(app, auth_client, test_model):
    _set_model(app, test_model["id"], early_access=True)
    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    assert b"early access" in resp.data


def test_early_access_badge_on_model_detail(app, auth_client, test_model):
    _set_model(app, test_model["id"], early_access=True)
    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    soup = BeautifulSoup(resp.data, "html.parser")
    badges = [b.get_text(strip=True) for b in soup.find_all("span", class_="badge")]
    assert "early access" in badges


def test_expired_model_absent_from_models_list(app, auth_client, test_model):
    from datetime import timedelta

    from lumen.timeutils import utcnow
    _set_model(app, test_model["id"], end_date=utcnow() - timedelta(days=1))
    resp = auth_client.get("/models")
    soup = BeautifulSoup(resp.data, "html.parser")
    cell_text = " ".join(td.get_text(strip=True) for td in soup.find_all("td"))
    assert test_model["model_name"] not in cell_text


def test_future_end_date_shows_available_until(app, auth_client, test_model):
    from datetime import timedelta

    from lumen.timeutils import utcnow
    _set_model(app, test_model["id"], end_date=utcnow() + timedelta(days=30))
    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    assert b"Available until" in resp.data


def test_model_detail_shows_first_seen(auth_client, test_model):
    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    assert b"First seen" in resp.data


def test_detail_modal_has_early_section_for_early_access(app, auth_client, test_model):
    _set_model(app, test_model["id"], early_access=True, needs_ack=True)
    resp = auth_client.get(f"/models/{test_model['model_name']}")
    soup = BeautifulSoup(resp.data, "html.parser")
    assert soup.find(id="ack-early-section") is not None
    assert soup.find(id="ack-notice-section") is not None


def test_detail_reprompts_when_requirement_added_after_consent(app, auth_client, test_model, test_user):
    """An existing needs_ack consent row does not satisfy a later early_access
    requirement — the acknowledge button is shown again."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        from lumen.timeutils import utcnow
        db.session.add(EntityModelConsent(entity_id=test_user["id"], model_config_id=test_model["id"],
                                          consented_at=utcnow()))
        db.session.commit()
    _set_model(app, test_model["id"], needs_ack=True, early_access=True)
    resp = auth_client.get(f"/models/{test_model['model_name']}")
    soup = BeautifulSoup(resp.data, "html.parser")
    assert soup.find(id="ack-open-btn") is not None
