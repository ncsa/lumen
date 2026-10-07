from bs4 import BeautifulSoup

from lumen.extensions import db
from lumen.models.model_config import ModelConfig


def _soup(auth_client):
    return BeautifulSoup(auth_client.get("/models").data, "html.parser")


def _row(soup, model_name):
    for row in soup.find_all("tr"):
        if row.find("a", string=model_name):
            return row
    return None


def test_coin_columns_grouped_under_one_header(auth_client, test_model):
    thead = _soup(auth_client).find("thead")
    top, sub = thead.find_all("tr")
    group = top.find("th", string="Coins / 1M tokens")
    assert group is not None
    assert group["colspan"] == "2"
    assert group["scope"] == "colgroup"
    assert [th.get_text(strip=True) for th in sub.find_all("th")] == ["In", "Out"]
    for th in sub.find_all("th"):
        assert th["scope"] == "col"
        assert "text-end" in th["class"]
    for th in top.find_all("th"):
        if th is not group:
            assert th["scope"] == "col"
            assert th["rowspan"] == "2"


def test_ack_header_is_abbreviated(auth_client, test_model):
    abbr = _soup(auth_client).find("thead").find("abbr")
    assert abbr.get_text(strip=True) == "Ack"
    assert abbr["title"] == "Acknowledgment required before use"


def test_model_name_does_not_wrap(auth_client, test_model):
    link = _row(_soup(auth_client), test_model["model_name"]).find("a")
    assert "text-nowrap" in link["class"]


def test_needs_ack_row_has_lock_icon_and_hidden_text(app, auth_client, test_model):
    with app.app_context():
        db.session.get(ModelConfig, test_model["id"]).needs_ack = True
        db.session.commit()

    row = _row(_soup(auth_client), test_model["model_name"])
    assert row is not None
    icon = row.find("i", class_="bi-lock-fill")
    assert icon is not None
    assert icon["aria-hidden"] == "true"
    hidden = row.find("span", class_="visually-hidden")
    assert hidden is not None
    assert hidden.get_text(strip=True) == "Acknowledgment required"
    assert not any("required" in b.get_text(strip=True) for b in row.find_all("span", class_="badge"))


def test_model_without_ack_has_no_lock(auth_client, test_model):
    row = _row(_soup(auth_client), test_model["model_name"])
    assert row is not None
    assert row.find("i", class_="bi-lock-fill") is None
    assert "Acknowledgment required" not in row.get_text()
