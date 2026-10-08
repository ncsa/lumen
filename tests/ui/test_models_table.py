from datetime import datetime

from bs4 import BeautifulSoup

from lumen.extensions import db
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint


def _soup(auth_client):
    return BeautifulSoup(auth_client.get("/models").data, "html.parser")


def _row(soup, model_name):
    for row in soup.find_all("tr"):
        if row.find("a", string=model_name):
            return row
    return None


def _pills(row):
    return [b.get_text(strip=True) for b in row.find_all("span", class_="badge")]


def _set_model(app, model_id, **fields):
    with app.app_context():
        model = db.session.get(ModelConfig, model_id)
        for name, value in fields.items():
            setattr(model, name, value)
        db.session.commit()


def test_coin_columns_grouped_under_one_header(auth_client, test_model):
    thead = _soup(auth_client).find("thead")
    top, sub = thead.find_all("tr")
    group = top.find("th", string="Coins / 1M tokens")
    assert group is not None
    assert group["colspan"] == "2"
    assert group["scope"] == "colgroup"
    assert [th.get_text(strip=True) for th in sub.find_all("th")] == ["Input", "Output"]
    for th in sub.find_all("th"):
        assert th["scope"] == "col"
        assert "text-end" in th["class"]
    for th in top.find_all("th"):
        if th is not group:
            assert th["scope"] == "col"
            assert th["rowspan"] == "2"


def test_last_column_header_is_access(auth_client, test_model):
    top = _soup(auth_client).find("thead").find("tr")
    assert top.find_all("th")[-1].get_text(strip=True) == "Access"


def test_model_name_does_not_wrap(auth_client, test_model):
    row = _row(_soup(auth_client), test_model["model_name"])
    assert row is not None
    assert "text-nowrap" in row.find("a")["class"]


def test_needs_ack_row_has_acknowledge_pill(app, auth_client, test_model):
    _set_model(app, test_model["id"], needs_ack=True)

    row = _row(_soup(auth_client), test_model["model_name"])
    assert row is not None
    pill = row.find("span", class_="badge", string="acknowledge")
    assert pill is not None
    assert "bg-warning" in pill["class"]
    assert "text-dark" in pill["class"]
    assert row.find("i", class_="bi-lock-fill") is None
    assert "Acknowledgment required" not in row.get_text()


def test_early_access_row_has_early_access_pill(app, auth_client, test_model):
    _set_model(app, test_model["id"], early_access=True)

    row = _row(_soup(auth_client), test_model["model_name"])
    assert row is not None
    pill = row.find("span", class_="badge", string="early access")
    assert pill is not None
    assert "bg-info" in pill["class"]
    assert "acknowledge" not in _pills(row)


def test_plain_model_has_no_access_pills(auth_client, test_model):
    row = _row(_soup(auth_client), test_model["model_name"])
    assert row is not None
    pills = _pills(row)
    assert "acknowledge" not in pills
    assert "early access" not in pills
    assert row.find("i", class_="bi-lock-fill") is None


def test_checked_header_renamed(auth_client, test_model):
    headers = [th.get_text(strip=True) for th in _soup(auth_client).find("thead").find_all("th")]
    assert "Checked" in headers
    assert "Last Checked" not in headers


def test_checked_time_is_relative(app, auth_client, test_model, test_model_endpoint):
    with app.app_context():
        db.session.get(ModelEndpoint, test_model_endpoint["id"]).last_checked_at = datetime(2026, 1, 2, 3, 4, 5)
        db.session.commit()

    span = _row(_soup(auth_client), test_model["model_name"]).find("span", class_="local-datetime")
    assert span is not None
    assert span.has_attr("data-relative")
    assert span["data-utc"] == "2026-01-02T03:04:05Z"


def test_unchecked_model_shows_never(auth_client, test_model, test_model_endpoint):
    row = _row(_soup(auth_client), test_model["model_name"])
    assert row.find("span", class_="local-datetime") is None
    assert "Never" in row.get_text()
