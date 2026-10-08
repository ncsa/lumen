from bs4 import BeautifulSoup


def _get_status_cell(html_bytes, model_name):
    soup = BeautifulSoup(html_bytes, "html.parser")
    # Column index of Status, expanding the grouped "Coins / 1M tokens" header.
    # Assumes Status is a rowspan header in the first <thead> row.
    status_idx = 0
    for th in soup.find("thead").find("tr").find_all("th"):
        if th.get_text(strip=True) == "Status":
            break
        status_idx += int(th.get("colspan", 1))
    # Find the row containing the model name link, then its Status cell
    for row in soup.find_all("tr"):
        if row.find("a", string=model_name):
            cells = row.find_all("td")
            assert len(cells) > status_idx, f"row for {model_name} has no Status cell"
            return cells[status_idx]
    return None


def _get_badge(html_bytes, model_name):
    cell = _get_status_cell(html_bytes, model_name)
    return cell.find("span", class_="badge") if cell else None


def _status_text(html_bytes, model_name):
    cell = _get_status_cell(html_bytes, model_name)
    assert cell is not None, f"model row for {model_name} not found"
    return " ".join(cell.get_text().split())


def _count_span(html_bytes, model_name):
    cell = _get_status_cell(html_bytes, model_name)
    return cell.find("span", class_="fw-bold") if cell else None


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
    assert _status_text(resp.data, test_model["model_name"]) == "ok 1/1"
    assert "text-success" in _count_span(resp.data, test_model["model_name"])["class"]


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
    assert _status_text(resp.data, test_model["model_name"]) == "down 0/1"
    assert "text-danger" in _count_span(resp.data, test_model["model_name"])["class"]


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
    assert _status_text(resp.data, test_model["model_name"]) == "degraded 1/2"
    assert "text-success" in _count_span(resp.data, test_model["model_name"])["class"]


def test_no_endpoints_badge_uses_bg_secondary(app, auth_client, test_model):
    # No endpoints added — model has zero endpoints
    resp = auth_client.get("/models")
    badge = _get_badge(resp.data, test_model["model_name"])
    assert badge is not None
    assert "bg-secondary" in badge["class"]
    assert badge.get_text(strip=True) == "no endpoints"
    assert _status_text(resp.data, test_model["model_name"]) == "no endpoints"
    assert _count_span(resp.data, test_model["model_name"]) is None


def test_status_badge_skips_modality_pills(app, auth_client, test_model):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        db.session.get(ModelConfig, test_model["id"]).input_modalities = ["text", "image"]
        db.session.commit()

    resp = auth_client.get("/models")
    soup = BeautifulSoup(resp.data, "html.parser")
    row = soup.find("a", string=test_model["model_name"]).find_parent("tr")
    assert len(row.find_all("span", class_="rounded-pill")) == 2
    badge = _get_badge(resp.data, test_model["model_name"])
    assert "rounded-pill" not in badge["class"]
    assert badge.get_text(strip=True) == "no endpoints"


def test_no_healthy_column(auth_client, test_model):
    soup = BeautifulSoup(auth_client.get("/models").data, "html.parser")
    headers = [th.get_text(strip=True) for th in soup.find("thead").find_all("th")]
    assert "Healthy" not in headers
    assert "Status" in headers
