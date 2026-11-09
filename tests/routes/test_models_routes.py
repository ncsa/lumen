from datetime import timedelta
from http import HTTPStatus

from bs4 import BeautifulSoup


def _row_for(html_bytes, model_name):
    soup = BeautifulSoup(html_bytes, "html.parser")
    for row in soup.find_all("tr"):
        if row.find("a", string=model_name):
            return row
    return None


def test_models_requires_login(client):
    resp = client.get("/models", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND
    assert "/" in resp.headers["Location"]


def test_models_lists_active_model(app, auth_client, test_model):
    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() in resp.data


def test_models_header_and_input_modality_pills(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        db.session.add(ModelConfig(
            model_name="pill-model",
            input_cost_per_million=1.0,
            output_cost_per_million=1.0,
            input_modalities=["text", "image"],
        ))
        db.session.commit()

    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    soup = BeautifulSoup(resp.data, "html.parser")
    assert soup.find("th", string="Input") is not None
    row = _row_for(resp.data, "pill-model")
    assert row is not None
    pills = [s.get_text(strip=True) for s in row.find_all(
        "span", class_=lambda c: c and "badge" in c and "rounded-pill" in c and "bg-secondary" in c)]
    assert pills == ["text", "image"]


def test_models_row_unknown_modalities_shows_dash(app, auth_client, test_model):
    resp = auth_client.get("/models")
    row = _row_for(resp.data, test_model["model_name"])
    assert row is not None
    cells = row.find_all("td")
    assert cells[1].get_text(strip=True) == "\u2014"  # em dash
    assert not cells[1].find_all("span", class_="badge")


def _make_other_owner(app):
    """Create a second user entity to own a model."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity import Entity
        owner = Entity(entity_type="user", email="owner@example.com", name="Owner", active=True)
        db.session.add(owner)
        db.session.commit()
        return owner.id


def test_models_owned_model_not_listed(app, auth_client, test_model, test_user):
    """A model owned by someone else (no grant) is hidden from the list."""
    from tests.conftest import set_model_owner
    with app.app_context():
        owner_id = _make_other_owner(app)
        set_model_owner(test_model["id"], owner_id)

    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() not in resp.data


def test_models_inactive_model_not_listed(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        m = ModelConfig(model_name="inactive-model", input_cost_per_million=1.0, output_cost_per_million=1.0, disabled=True)
        db.session.add(m)
        db.session.commit()

    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK
    assert b"inactive-model" not in resp.data


def test_model_detail_requires_login(client, test_model):
    resp = client.get(f"/models/{test_model['model_name']}", follow_redirects=False)
    assert resp.status_code == HTTPStatus.FOUND


def test_model_detail_404_unknown(auth_client):
    resp = auth_client.get("/models/does-not-exist")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_models_cache_column_shows_the_charged_rates(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        db.session.add_all([
            ModelConfig(model_name="cached-model", input_cost_per_million=1.0, output_cost_per_million=2.0,
                        cached_input_cost_per_million=0.1),
            ModelConfig(model_name="cache-write-model", input_cost_per_million=1.0, output_cost_per_million=2.0,
                        cache_write_cost_per_million=0.5),
            ModelConfig(model_name="plain-model", input_cost_per_million=1.0, output_cost_per_million=2.0),
        ])
        db.session.commit()

    resp = auth_client.get("/models")
    assert resp.status_code == HTTPStatus.OK

    def cache_cell(model_name):
        cells = _row_for(resp.data, model_name).find_all("td")
        # The cache column is the last of the three "Coins / 1M tokens" cells.
        assert cells[2].get_text(strip=True) == "1.00"
        assert cells[3].get_text(strip=True) == "2.00"
        return cells[4]

    # An unconfigured rate is billed at the input price, so the column shows the
    # input rate on the missing side rather than a dash or a hover tip.
    assert cache_cell("cached-model").get_text(strip=True) == "0.10 / 1.00"
    assert cache_cell("cache-write-model").get_text(strip=True) == "1.00 / 0.50"
    assert cache_cell("plain-model").get_text(strip=True) == "1.00 / 1.00"
    for model_name in ("cached-model", "cache-write-model", "plain-model"):
        row = _row_for(resp.data, model_name)
        assert row.find("span", attrs={"data-bs-toggle": "tooltip"}) is None


def test_model_detail_cached_input_row_only_when_set(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        db.session.add_all([
            ModelConfig(model_name="cached-detail", input_cost_per_million=1.0, output_cost_per_million=2.0,
                        cached_input_cost_per_million=0.05),
            ModelConfig(model_name="cache-write-detail", input_cost_per_million=1.0, output_cost_per_million=2.0,
                        cache_write_cost_per_million=0.75),
            ModelConfig(model_name="plain-detail", input_cost_per_million=1.0, output_cost_per_million=2.0),
        ])
        db.session.commit()

    soup = BeautifulSoup(auth_client.get("/models/cached-detail").data, "html.parser")
    th = soup.find("th", string="Cached input")
    assert th is not None
    assert th.parent.find("td").get_text(strip=True) == "0.05 / 1M"
    write_soup = BeautifulSoup(auth_client.get("/models/cache-write-detail").data, "html.parser")
    write = write_soup.find("th", string="Cache write")
    assert write is not None
    assert write.parent.find("td").get_text(strip=True) == "0.75 / 1M"
    plain = BeautifulSoup(auth_client.get("/models/plain-detail").data, "html.parser")
    assert plain.find("th", string="Cached input") is None
    assert plain.find("th", string="Cache write") is None


def test_model_detail_ok(auth_client, test_model):
    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() in resp.data


def test_model_detail_blocked_returns_not_found(app, auth_client, test_model, test_user):
    from tests.conftest import set_model_owner
    with app.app_context():
        owner_id = _make_other_owner(app)
        set_model_owner(test_model["id"], owner_id)

    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert test_model["model_name"].encode() not in resp.data


def test_model_detail_inactive_returns_not_found(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        m = ModelConfig(model_name="inactive-detail", input_cost_per_million=1.0, output_cost_per_million=1.0, disabled=True)
        db.session.add(m)
        db.session.commit()

    resp = auth_client.get("/models/inactive-detail")
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert b"inactive-detail" not in resp.data


def test_model_detail_expired_returns_not_found(app, auth_client):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        from lumen.timeutils import utcnow

        m = ModelConfig(
            model_name="expired-detail",
            input_cost_per_million=1.0,
            output_cost_per_million=1.0,
            end_date=utcnow() - timedelta(seconds=1),
        )
        db.session.add(m)
        db.session.commit()

    resp = auth_client.get("/models/expired-detail")
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert b"expired-detail" not in resp.data


def test_model_readme_inactive_returns_not_found_without_fetching(app, auth_client, monkeypatch):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        m = ModelConfig(
            model_name="inactive-readme",
            input_cost_per_million=1.0,
            output_cost_per_million=1.0,
            url="https://huggingface.co/org/Some-Model",
            disabled=True,
        )
        db.session.add(m)
        db.session.commit()

    from lumen.blueprints.models_page import routes

    def fail_fetch(*args, **kwargs):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(routes.http_requests, "get", fail_fetch)

    resp = auth_client.get("/models/inactive-readme/readme")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_model_readme_blocked_returns_not_found_without_fetching(
    app, auth_client, test_model, test_user, monkeypatch
):
    from tests.conftest import set_model_owner

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig

        model = db.session.get(ModelConfig, test_model["id"])
        model.url = "https://huggingface.co/org/private-model"
        db.session.commit()
        set_model_owner(test_model["id"], _make_other_owner(app))

    from lumen.blueprints.models_page import routes

    def fail_fetch(*args, **kwargs):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(routes.http_requests, "get", fail_fetch)

    resp = auth_client.get(f"/models/{test_model['model_name']}/readme")
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_model_detail_needs_ack_remains_visible(app, auth_client, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig

        db.session.get(ModelConfig, test_model["id"]).needs_ack = True
        db.session.commit()

    resp = auth_client.get(f"/models/{test_model['model_name']}")
    assert resp.status_code == HTTPStatus.OK
    assert test_model["model_name"].encode() in resp.data
