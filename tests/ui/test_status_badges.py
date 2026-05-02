from bs4 import BeautifulSoup


def _get_badge(html_bytes, model_name):
    soup = BeautifulSoup(html_bytes, "html.parser")
    # Find the row containing the model name link, then find its badge
    for row in soup.find_all("tr"):
        link = row.find("a", string=model_name)
        if link:
            badge = row.find("span", class_="badge")
            return badge
    return None


def test_ok_badge_uses_bg_success(app, auth_client, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        db.session.add(ModelEndpoint(
            model_config_id=test_model["id"],
            url="http://localhost:9999/v1",
            api_key="key",
            healthy=True,
        ))
        db.session.commit()

    resp = auth_client.get("/models")
    badge = _get_badge(resp.data, test_model["model_name"])
    assert badge is not None
    assert "bg-success" in badge["class"]
    assert badge.get_text(strip=True) == "ok"


def test_down_badge_uses_bg_danger(app, auth_client, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        db.session.add(ModelEndpoint(
            model_config_id=test_model["id"],
            url="http://localhost:9999/v1",
            api_key="key",
            healthy=False,
        ))
        db.session.commit()

    resp = auth_client.get("/models")
    badge = _get_badge(resp.data, test_model["model_name"])
    assert badge is not None
    assert "bg-danger" in badge["class"]
    assert badge.get_text(strip=True) == "down"


def test_degraded_badge_uses_bg_warning(app, auth_client, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        db.session.add(ModelEndpoint(
            model_config_id=test_model["id"],
            url="http://localhost:9999/v1",
            api_key="key1",
            healthy=True,
        ))
        db.session.add(ModelEndpoint(
            model_config_id=test_model["id"],
            url="http://localhost:9999/v1",
            api_key="key2",
            healthy=False,
        ))
        db.session.commit()

    resp = auth_client.get("/models")
    badge = _get_badge(resp.data, test_model["model_name"])
    assert badge is not None
    assert "bg-warning" in badge["class"]
    assert "text-dark" in badge["class"]
    assert badge.get_text(strip=True) == "degraded"


def test_no_endpoints_badge_uses_bg_secondary(app, auth_client, test_model):
    # No endpoints added — model has zero endpoints
    resp = auth_client.get("/models")
    badge = _get_badge(resp.data, test_model["model_name"])
    assert badge is not None
    assert "bg-secondary" in badge["class"]
    assert badge.get_text(strip=True) == "no endpoints"
