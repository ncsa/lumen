from datetime import datetime

from bs4 import BeautifulSoup

from lumen.extensions import db
from lumen.models.entity_model_consent import EntityModelConsent
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


def _access_cell(row):
    return row.find_all("td")[-1]


def _access_buttons(row):
    return {b.find(string=True, recursive=False).strip(): b for b in _access_cell(row).find_all("button")}


def _consent(app, entity_id, model_id, **timestamps):
    with app.app_context():
        db.session.add(EntityModelConsent(entity_id=entity_id, model_config_id=model_id, **timestamps))
        db.session.commit()


def test_needs_ack_row_has_acknowledge_button(app, auth_client, test_model):
    _set_model(app, test_model["id"], needs_ack=True, ack_message="Read **this**.")

    row = _row(_soup(auth_client), test_model["model_name"])
    assert row is not None
    buttons = _access_buttons(row)
    assert list(buttons) == ["acknowledge"]
    btn = buttons["acknowledge"]
    assert btn["type"] == "button"
    assert {"badge", "rounded-pill", "bg-warning", "text-dark", "ack-btn"} <= set(btn["class"])
    assert btn["data-name"] == test_model["model_name"]
    assert btn["data-notice"] == "Read **this**."
    assert btn["data-early-notice"] == ""
    assert btn.find("span", class_="visually-hidden").get_text() == f" for {test_model['model_name']}"
    assert row.find("i", class_="bi-lock-fill") is None
    assert "Acknowledgment required" not in row.get_text()


def test_early_access_row_has_early_access_button(app, auth_client, test_model):
    _set_model(app, test_model["id"], early_access=True)

    row = _row(_soup(auth_client), test_model["model_name"])
    assert row is not None
    buttons = _access_buttons(row)
    assert list(buttons) == ["early access"]
    btn = buttons["early access"]
    assert "bg-info" in btn["class"]
    assert btn["data-name"] == test_model["model_name"]
    assert btn["data-notice"] == ""


def test_both_requirements_render_both_buttons(app, auth_client, test_model):
    _set_model(app, test_model["id"], needs_ack=True, early_access=True)

    row = _row(_soup(auth_client), test_model["model_name"])
    buttons = _access_buttons(row)
    assert list(buttons) == ["acknowledge", "early access"]
    # Either pill opens the same dialog with both notices.
    assert buttons["acknowledge"]["data-early-notice"] == buttons["early access"]["data-early-notice"]
    assert buttons["acknowledge"]["data-notice"] == buttons["early access"]["data-notice"]


def test_consented_model_shows_single_granted_pill(app, auth_client, test_user, test_model):
    _set_model(app, test_model["id"], needs_ack=True, early_access=True)
    _consent(app, test_user["id"], test_model["id"], consented_at=datetime(2026, 1, 1), early_access_at=datetime(2026, 1, 1))

    cell = _access_cell(_row(_soup(auth_client), test_model["model_name"]))
    assert cell.find(class_="ack-btn") is None
    pills = cell.find_all(class_="badge")
    assert len(pills) == 1
    pill = pills[0]
    assert pill.name == "button"
    assert pill["type"] == "button"
    assert pill.find(string=True, recursive=False).strip() == "granted"
    assert {"bg-success", "consent-info-btn"} <= set(pill["class"])
    assert pill["data-consented-at"] == "2026-01-01T00:00:00Z"
    assert "for test-model" in pill.find("span", class_="visually-hidden").get_text()


def test_granted_pill_carries_notices_for_details_popover(app, auth_client, test_user, test_model):
    _set_model(app, test_model["id"], needs_ack=True, ack_message="Read **this**.")
    _consent(app, test_user["id"], test_model["id"], consented_at=datetime(2026, 2, 3, 4, 5, 6))

    pill = _access_cell(_row(_soup(auth_client), test_model["model_name"])).find("button", class_="consent-info-btn")
    assert pill["data-notice"] == "Read **this**."
    assert pill["data-early-notice"] == ""
    assert pill["data-consented-at"] == "2026-02-03T04:05:06Z"


def test_requirement_added_after_consent_shows_pills_again(app, auth_client, test_user, test_model):
    _set_model(app, test_model["id"], needs_ack=True)
    _consent(app, test_user["id"], test_model["id"], consented_at=datetime(2026, 1, 1))
    _set_model(app, test_model["id"], early_access=True)

    row = _row(_soup(auth_client), test_model["model_name"])
    assert list(_access_buttons(row)) == ["acknowledge", "early access"]
    assert "granted" not in _pills(row)


def test_plain_model_has_empty_access_cell(auth_client, test_model):
    row = _row(_soup(auth_client), test_model["model_name"])
    assert row is not None
    cell = _access_cell(row)
    assert cell.get_text(strip=True) == ""
    assert cell.find("button") is None
    assert row.find("i", class_="bi-lock-fill") is None


def test_page_includes_ack_dialog(auth_client, test_model):
    soup = _soup(auth_client)
    assert soup.find(id="ackModal") is not None
    assert any("ack-consent.js" in (s.get("src") or "") for s in soup.find_all("script"))


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
