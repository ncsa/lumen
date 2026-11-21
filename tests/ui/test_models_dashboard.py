"""The Model Dashboard (/models): stat cards, collapsed rows, expanded panels and access."""
from datetime import datetime
from decimal import Decimal

import pytest
from bs4 import BeautifulSoup

from lumen.blueprints.models_page.routes import _token_count
from lumen.extensions import db
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint


def _soup(auth_client):
    # pytest-flask pushes one request context for the whole test, so every
    # request here shares its SQLAlchemy session and no request teardown runs
    # between them. Expire that session so each render reads the database the
    # way a real request's fresh session does; otherwise a row loaded by an
    # earlier request keeps its old attribute values for as long as it stays
    # in the identity map.
    db.session.expire_all()
    return BeautifulSoup(auth_client.get("/models").data, "html.parser")


def _row(soup, model_name):
    return soup.find("li", class_="model-row", attrs={"data-model": model_name})


def _card(soup, key):
    card = soup.find(class_="model-stat", attrs={"data-stat": key})
    return (
        int(card.find(class_="model-stat-count").get_text(strip=True)),
        card.find(class_="model-stat-subtitle").get_text(strip=True),
    )


def _set_model(app, model_id, **fields):
    with app.app_context():
        model = db.session.get(ModelConfig, model_id)
        for name, value in fields.items():
            setattr(model, name, value)
        db.session.commit()


def _add_model(app, name, *health, **fields):
    """A model with one active endpoint per entry of ``health`` (True = healthy)."""
    with app.app_context():
        model = ModelConfig(model_name=name, input_cost_per_million=1.0, output_cost_per_million=2.0, **fields)
        db.session.add(model)
        db.session.flush()
        for i, healthy in enumerate(health):
            db.session.add(ModelEndpoint(model_config_id=model.id, url="http://localhost:9999/v1", api_key=f"k{i}", healthy=healthy))
        db.session.commit()
        return model.id


def _add_endpoints(app, model_id, *health):
    with app.app_context():
        for i, healthy in enumerate(health):
            db.session.add(ModelEndpoint(model_config_id=model_id, url="http://localhost:9999/v1", api_key=f"e{i}", healthy=healthy))
        db.session.commit()


def _consent(app, entity_id, model_id, **timestamps):
    with app.app_context():
        db.session.add(EntityModelConsent(entity_id=entity_id, model_config_id=model_id, **timestamps))
        db.session.commit()


def _access(row):
    return row.find(class_="model-access")


def _access_buttons(row):
    return {b.find(string=True, recursive=False).strip(): b for b in _access(row).find_all("button")}


# ── Stat cards ────────────────────────────────────────────────────────────


def test_header_and_connect_button(auth_client):
    soup = _soup(auth_client)
    assert soup.find("h1").get_text(strip=True) == "Model Dashboard"
    connect = soup.find("a", href="/connect")
    assert connect.find("i", class_="bi-plug") is not None


def test_healthy_card_counts_degraded_and_names_down_and_no_endpoints(app, auth_client):
    _add_model(app, "ok-model", True)
    _add_model(app, "degraded-model", True, False)
    _add_model(app, "down-model", False)
    _add_model(app, "bare-model")

    soup = _soup(auth_client)
    assert _card(soup, "models") == (4, "Listed models")
    assert _card(soup, "healthy") == (2, "1 down · 1 no endpoints")


@pytest.mark.parametrize(("health", "subtitle"), [
    ([(False,)], "1 down"),
    ([()], "1 no endpoints"),
    ([(True, False), (True, False)], "2 degraded"),
    ([(True,), (True, True)], "All healthy"),
])
def test_healthy_card_subtitle(app, auth_client, health, subtitle):
    for i, endpoints in enumerate(health):
        _add_model(app, f"model-{i}", *endpoints)
    _add_model(app, "always-ok", True)

    assert _card(_soup(auth_client), "healthy")[1] == subtitle


def test_early_access_and_needs_ack_cards(app, auth_client, test_user):
    _add_model(app, "plain", True)
    _add_model(app, "early", True, early_access=True)
    ack_id = _add_model(app, "ack", True, needs_ack=True)

    soup = _soup(auth_client)
    assert _card(soup, "models") == (3, "Listed models")
    assert _card(soup, "early-access") == (1, "May change or be removed")
    assert _card(soup, "needs-ack") == (2, "Acknowledge before use")

    _consent(app, test_user["id"], ack_id, consented_at=datetime(2026, 1, 1))
    assert _card(_soup(auth_client), "needs-ack") == (1, "Acknowledge before use")


def test_needs_ack_card_with_nothing_to_acknowledge(auth_client, test_model):
    assert _card(_soup(auth_client), "needs-ack") == (0, "Nothing to acknowledge")


# ── Collapsed row ─────────────────────────────────────────────────────────


def test_collapsed_row(app, auth_client, test_model, test_model_endpoint):
    _set_model(app, test_model["id"], input_modalities=["text", "image"], output_modalities=["text"],
               context_window=131072, max_output_tokens=8192,
               cached_input_cost_per_million=0.25, cache_write_cost_per_million=1.5)

    row = _row(_soup(auth_client), "test-model")
    link = row.find("a", class_="model-name")
    assert link.get_text(strip=True) == "test-model"
    assert link["href"] == "/models/test-model"
    assert row.find(class_="model-modalities").get_text(strip=True) == "Text, Image → Text"
    assert row.find(class_="model-limits").get_text(strip=True) == "128K context · 8K max output"
    assert row.find(class_="model-price").get_text(strip=True) == "1.00 / 2.00"
    health = row.find(class_="model-health")
    assert health.get_text(strip=True) == "Healthy"
    assert health.find("i", class_="bi-check-circle-fill") is not None
    row.find(class_="collapse").extract()  # what is left is the collapsed row
    assert "0.25" not in row.get_text()
    assert "1.50" not in row.get_text()


def test_collapsed_row_dashes_for_unknown_values(auth_client, test_model):
    row = _row(_soup(auth_client), "test-model")
    assert row.find(class_="model-modalities").get_text(strip=True) == "—"
    assert row.find(class_="model-limits").get_text(strip=True) == "— context · — max output"


def test_collapsed_row_dash_for_missing_side(app, auth_client, test_model):
    _set_model(app, test_model["id"], input_modalities=["audio"])
    assert _row(_soup(auth_client), "test-model").find(class_="model-modalities").get_text(strip=True) == "Audio → —"


@pytest.mark.parametrize(("value", "text"), [
    (None, "—"), (0, "—"), (512, "512"), (1500, "1.5K"), (8192, "8K"), (128000, "128K"),
    (131072, "128K"), (200000, "200K"), (1_000_000, "1M"), (1_048_576, "1M"), (2_500_000, "2.5M"),
])
def test_token_count(value, text):
    assert _token_count(value) == text


@pytest.mark.parametrize(("health", "label", "icon"), [
    ((True, False), "Degraded", "bi-exclamation-triangle-fill"),
    ((False,), "Down", "bi-x-circle-fill"),
    ((), "No endpoints", "bi-dash-circle"),
])
def test_health_labels(app, auth_client, test_model, health, label, icon):
    _add_endpoints(app, test_model["id"], *health)
    tag = _row(_soup(auth_client), "test-model").find(class_="model-health")
    assert tag.get_text(strip=True) == label
    assert tag.find("i", class_=icon)["aria-hidden"] == "true"


def test_collapsed_row_pills_while_ack_pending(app, auth_client, test_user, test_model):
    _set_model(app, test_model["id"], needs_ack=True, early_access=True)
    pills = _row(_soup(auth_client), "test-model").find(class_="model-row-pills")
    assert [p.get_text(strip=True) for p in pills.find_all(class_="badge")] == ["acknowledge", "early access"]

    _consent(app, test_user["id"], test_model["id"], consented_at=datetime(2026, 1, 1), early_access_at=datetime(2026, 1, 1))
    assert _row(_soup(auth_client), "test-model").find(class_="model-row-pills") is None


# ── Expanded panel ────────────────────────────────────────────────────────


def test_panels_share_one_parent_and_start_collapsed(app, auth_client, test_model):
    _add_model(app, "second-model")
    soup = _soup(auth_client)
    model_list = soup.find(id="model-list")
    assert soup.find(id=model_list["aria-labelledby"]).get_text(strip=True) == "Models"
    rows = soup.find_all("li", class_="model-row")
    assert len(rows) == 2
    panel_ids = set()
    for row in rows:
        panel = row.find(class_="collapse")
        toggle = row.find("button", class_="model-row-toggle")
        assert panel["data-bs-parent"] == "#model-list"
        assert "show" not in panel["class"]
        assert toggle["data-bs-toggle"] == "collapse"
        assert toggle["aria-expanded"] == "false"
        assert toggle["aria-controls"] == panel["id"]
        assert toggle["data-bs-target"] == f"#{panel['id']}"
        assert toggle["aria-label"] == f"Toggle details for {row['data-model']}"
        panel_ids.add(panel["id"])
    assert len(panel_ids) == 2


def _panel_values(row, cls):
    dl = row.find("dl", class_=cls) if cls else row.find("dl")
    return dict(zip([t.get_text(strip=True) for t in dl.find_all("dt")], [d.get_text(strip=True) for d in dl.find_all("dd")]))


def test_panel_health_never_checked(app, auth_client, test_model):
    _add_endpoints(app, test_model["id"], True, False)
    row = _row(_soup(auth_client), "test-model")
    assert _panel_values(row, None) == {"Last checked": "Never", "Health checks": "1/2 passed"}
    assert row.find("span", class_="local-datetime") is None


def test_panel_last_checked_is_relative(app, auth_client, test_model, test_model_endpoint):
    with app.app_context():
        db.session.get(ModelEndpoint, test_model_endpoint["id"]).last_checked_at = datetime(2026, 1, 2, 3, 4, 5)
        db.session.commit()
    span = _row(_soup(auth_client), "test-model").find("span", class_="local-datetime")
    assert span.has_attr("data-relative")
    assert span["data-utc"] == "2026-01-02T03:04:05Z"


def test_panel_effective_cache_prices_unset(app, auth_client, test_model):
    # Unset cache rates: read billed at the input price, write at 0 (#685).
    assert _panel_values(_row(_soup(auth_client), "test-model"), "model-pricing") == {
        "Input": "1.00", "Output": "2.00", "Cached read": "1.00", "Cached write": "0.00",
    }


def test_panel_effective_cache_prices_set(app, auth_client, test_model):
    # Set before the first request: in the full suite the request session can
    # keep a ModelConfig loaded by an earlier request in the same test, so a
    # change made between two requests may not show up in the second.
    _set_model(app, test_model["id"], cached_input_cost_per_million=0.25, cache_write_cost_per_million=1.5)
    assert _panel_values(_row(_soup(auth_client), "test-model"), "model-pricing") == {
        "Input": "1.00", "Output": "2.00", "Cached read": "0.25", "Cached write": "1.50",
    }


def test_rerender_reads_rates_changed_between_requests(app, auth_client, test_model):
    # Both requests share the test's SQLAlchemy session (see _soup). Hold the
    # row there, as the first render does, so the second render shows the
    # change only because _soup expires the session first.
    held = db.session.get(ModelConfig, test_model["id"])
    assert _panel_values(_row(_soup(auth_client), "test-model"), "model-pricing")["Cached read"] == "1.00"
    _set_model(app, test_model["id"], cached_input_cost_per_million=0.25, cache_write_cost_per_million=1.5)
    assert _panel_values(_row(_soup(auth_client), "test-model"), "model-pricing") == {
        "Input": "1.00", "Output": "2.00", "Cached read": "0.25", "Cached write": "1.50",
    }
    assert held.cached_input_cost_per_million == Decimal("0.25")


# ── Access ────────────────────────────────────────────────────────────────


def test_access_acknowledge(app, auth_client, test_model):
    _set_model(app, test_model["id"], needs_ack=True, ack_message="Read **this**.")
    buttons = _access_buttons(_row(_soup(auth_client), "test-model"))
    assert list(buttons) == ["acknowledge"]
    btn = buttons["acknowledge"]
    assert {"ack-btn", "bg-warning", "text-dark"} <= set(btn["class"])
    assert btn["aria-haspopup"] == "dialog"
    assert btn["data-name"] == "test-model"
    assert btn["data-notice"] == "Read **this**."
    assert btn.find("span", class_="visually-hidden").get_text() == " for test-model"


def test_access_early_access(app, auth_client, test_model):
    _set_model(app, test_model["id"], early_access=True)
    buttons = _access_buttons(_row(_soup(auth_client), "test-model"))
    assert list(buttons) == ["early access"]
    assert {"ack-btn", "bg-info"} <= set(buttons["early access"]["class"])


def test_access_both_requirements(app, auth_client, test_model):
    _set_model(app, test_model["id"], needs_ack=True, early_access=True)
    buttons = _access_buttons(_row(_soup(auth_client), "test-model"))
    assert list(buttons) == ["acknowledge", "early access"]
    assert buttons["acknowledge"]["data-early-notice"] == buttons["early access"]["data-early-notice"]


def test_access_granted_with_popover_data(app, auth_client, test_user, test_model):
    _set_model(app, test_model["id"], needs_ack=True, ack_message="Read **this**.")
    _consent(app, test_user["id"], test_model["id"], consented_at=datetime(2026, 2, 3, 4, 5, 6))
    access = _access(_row(_soup(auth_client), "test-model"))
    assert access.find(class_="ack-btn") is None
    pill = access.find("button", class_="consent-info-btn")
    assert pill.find(string=True, recursive=False).strip() == "granted"
    assert pill["data-notice"] == "Read **this**."
    assert pill["data-early-notice"] == ""
    assert pill["data-consented-at"] == "2026-02-03T04:05:06Z"
    assert "for test-model" in pill.find("span", class_="visually-hidden").get_text()


def test_access_requirement_added_after_consent(app, auth_client, test_user, test_model):
    _set_model(app, test_model["id"], needs_ack=True)
    _consent(app, test_user["id"], test_model["id"], consented_at=datetime(2026, 1, 1))
    _set_model(app, test_model["id"], early_access=True)
    assert list(_access_buttons(_row(_soup(auth_client), "test-model"))) == ["acknowledge", "early access"]


def test_access_open(auth_client, test_model):
    access = _access(_row(_soup(auth_client), "test-model"))
    assert access.find("button") is None
    assert access.get_text(" ", strip=True) == "Access Open"


def test_page_includes_ack_dialog(auth_client, test_model):
    soup = _soup(auth_client)
    assert soup.find(id="ackModal") is not None
    assert any("ack-consent.js" in (s.get("src") or "") for s in soup.find_all("script"))


# ── Empty state ───────────────────────────────────────────────────────────


def test_empty_state(auth_client):
    soup = _soup(auth_client)
    assert soup.find("li", class_="model-row") is None
    assert soup.find(id="model-list").get_text(strip=True) == "No models configured."
    assert _card(soup, "models") == (0, "Listed models")
    assert _card(soup, "healthy") == (0, "All healthy")
