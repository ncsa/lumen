"""The Model Dashboard (/models): stat cards, collapsed rows, expanded panels and access."""
import re
from datetime import datetime
from decimal import Decimal

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import event

from lumen.blueprints.models_page import routes as models_page_routes
from lumen.blueprints.models_page.routes import _token_count
from lumen.extensions import db
from lumen.models.entity_model_consent import EntityModelConsent
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.services import llm


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
        model = ModelConfig(model_name=name, **{"input_cost_per_million": 1.0, "output_cost_per_million": 2.0, **fields})
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
    ([(True, False), ()], "1 degraded · 1 no endpoints"),
    ([(False,), (True, False), ()], "1 down · 1 no endpoints"),
    ([(True,), (True, True)], "All healthy"),
])
def test_healthy_card_subtitle(app, auth_client, health, subtitle):
    for i, endpoints in enumerate(health):
        _add_model(app, f"model-{i}", *endpoints)
    _add_model(app, "always-ok", True)

    assert _card(_soup(auth_client), "healthy")[1] == subtitle


@pytest.mark.parametrize(("health", "status"), [
    ([(False,), (True, False)], "down"),
    ([(False,)], "down"),
    ([(True, False), (True, False)], "degraded"),
    ([(True, False), ()], "degraded"),
    ([()], None),
    ([(True,), (True, True)], None),
])
def test_healthy_card_health_class(app, auth_client, health, status):
    """Red while any model is down, yellow while some are only degraded; no-endpoint models alone color nothing."""
    for i, endpoints in enumerate(health):
        _add_model(app, f"model-{i}", *endpoints)
    _add_model(app, "always-ok", True)

    soup = _soup(auth_client)
    classes = soup.find(class_="model-stat", attrs={"data-stat": "healthy"})["class"]
    assert [c for c in classes if c.startswith("model-stat-health-")] == ([f"model-stat-health-{status}"] if status else [])
    for key in ("models", "early-access", "needs-ack"):
        card = soup.find(class_="model-stat", attrs={"data-stat": key})
        assert not any(c.startswith("model-stat-health-") for c in card["class"])


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


def _ack_detail(soup):
    detail = soup.find(class_="model-stat", attrs={"data-stat": "needs-ack"}).find(class_="model-stat-detail")
    return detail.get_text(strip=True) if detail else None


def test_needs_ack_card_counts_acknowledged_models(app, auth_client, test_user):
    _add_model(app, "plain", True)
    first = _add_model(app, "ack-1", True, needs_ack=True)
    second = _add_model(app, "ack-2", True, early_access=True)

    soup = _soup(auth_client)
    assert _card(soup, "needs-ack") == (2, "Acknowledge before use")
    assert _ack_detail(soup) == "0 of 2 acknowledged"

    _consent(app, test_user["id"], first, consented_at=datetime(2026, 1, 1))
    soup = _soup(auth_client)
    assert _card(soup, "needs-ack") == (1, "Acknowledge before use")
    assert _ack_detail(soup) == "1 of 2 acknowledged"

    _consent(app, test_user["id"], second, early_access_at=datetime(2026, 1, 1))
    soup = _soup(auth_client)
    assert _card(soup, "needs-ack") == (0, "All 2 acknowledged")
    assert _ack_detail(soup) is None


def test_needs_ack_card_with_nothing_to_acknowledge(auth_client, test_model):
    soup = _soup(auth_client)
    assert _card(soup, "needs-ack") == (0, "Nothing to acknowledge")
    assert _ack_detail(soup) is None


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
    assert row.find(class_="model-context").get_text(strip=True) == "128K context"
    assert row.find(class_="model-max-output").get_text(strip=True) == "8K max output"
    assert row.find(class_="model-price").find(string=True, recursive=False).strip() == "1.00 / 2.00"
    health = row.find(class_="model-health")
    assert health.get_text(strip=True) == "Healthy"
    assert health.find("i", class_="bi-check-circle-fill") is not None
    row.find(class_="collapse").extract()  # what is left is the collapsed row
    assert "0.25" not in row.get_text()
    assert "1.50" not in row.get_text()


def test_collapsed_row_dashes_for_unknown_values(auth_client, test_model):
    row = _row(_soup(auth_client), "test-model")
    assert row.find(class_="model-modalities").get_text(strip=True) == "—"
    assert row.find(class_="model-context").get_text(strip=True) == "— context"
    assert row.find(class_="model-max-output").get_text(strip=True) == "— max output"


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


def _flags(row):
    """The lock / flask icons beside the model name, as (icon, accessible name)."""
    icons = row.find(class_="model-row-name").find_all("i", class_="model-flag")
    return [(next(c for c in i["class"] if c in {"bi-lock", "bi-unlock", "bi-flask"}), i["aria-label"]) for i in icons]


def test_row_columns_in_order(app, auth_client, test_model):
    soup = _soup(auth_client)
    grid = _row(soup, "test-model").find(class_="model-row-grid")
    columns = grid.find_all(recursive=False)
    assert [name in c["class"] for c, name in zip(columns, ["model-row-main", "model-limits", "model-row-side"])] == [True] * 3
    assert len(columns) == 3
    header = soup.find(class_="model-list-columns")
    assert [c.get_text(" ", strip=True) for c in header.find_all(recursive=False)] == [
        "Model ▲ ▼ / Capabilities", "Context ▲ ▼ Max output ▲ ▼", "Price ▲ ▼ Coins / 1M tokens"]


def test_price_names_its_unit_for_screen_readers(auth_client, test_model):
    # The price is read row by row, away from the header, so it carries its own unit.
    price = _row(_soup(auth_client), "test-model").find(class_="model-price")
    assert price.find(class_="visually-hidden").get_text(strip=True) == "coins per 1M input / output tokens"


def test_closed_lock_while_ack_pending_then_open_lock(app, auth_client, test_user, test_model):
    _set_model(app, test_model["id"], needs_ack=True, early_access=True)
    row = _row(_soup(auth_client), "test-model")
    assert _flags(row) == [("bi-lock", "Needs acknowledgment"), ("bi-flask", "Early access")]
    assert row.find(class_="model-row-grid").find(class_="badge") is None  # icons replace the pills

    # Once acknowledged the lock opens; the model is still early access.
    _consent(app, test_user["id"], test_model["id"], consented_at=datetime(2026, 1, 1), early_access_at=datetime(2026, 1, 1))
    assert _flags(_row(_soup(auth_client), "test-model")) == [("bi-unlock", "Access granted"), ("bi-flask", "Early access")]


def test_closed_lock_for_needs_ack_model(app, auth_client, test_model):
    _set_model(app, test_model["id"], needs_ack=True)
    assert _flags(_row(_soup(auth_client), "test-model")) == [("bi-lock", "Needs acknowledgment")]


def test_early_access_only_model_needs_acknowledgment_too(app, auth_client, test_model):
    # Early access alone still has to be acknowledged before use, so it is locked as well.
    _set_model(app, test_model["id"], early_access=True)
    assert _flags(_row(_soup(auth_client), "test-model")) == [("bi-lock", "Needs acknowledgment"), ("bi-flask", "Early access")]


def test_no_icons_for_plain_model(auth_client, test_model):
    assert _flags(_row(_soup(auth_client), "test-model")) == []


def test_row_click_selector_matches_only_rows_with_a_toggle(app, auth_client, test_model):
    # The row-click script calls .click() on each matched node's chevron, so
    # every node its selector matches must have one; the column header
    # shares the grid class but has no toggle. Browser behaviour is in
    # test_models_row_click.py.
    _add_model(app, "second-model")
    soup = _soup(auth_client)
    script = "".join(s.get_text() for s in soup.find_all("script") if not s.get("src"))
    selectors = re.findall(r"""querySelectorAll\(\s*(['"`])([^'"`]*model-row-grid[^'"`]*)\1\s*\)""", script)
    assert selectors, "the row-click script no longer binds .model-row-grid via querySelectorAll; update this test"
    for _, selector in selectors:
        matched = soup.select(selector)
        assert len(matched) == 2, selector
        assert all(node.find("button", class_="model-row-toggle") for node in matched), selector
        assert soup.find(class_="model-list-columns") not in matched, selector


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


def _access_rows(row):
    """``{label: (state, text)}`` for the Access & Acknowledgments list, in page order."""
    dl = _access(row).find("dl", class_="model-access-list")
    return {
        dt.get_text(strip=True): (dd["data-state"], dd.find("span").get_text(strip=True))
        for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd"))
    }


def _accepted_span(row, label):
    dl = _access(row).find("dl", class_="model-access-list")
    dd = dl.find("dt", string=label).find_next_sibling("dd")
    return dd.find("span", class_="local-datetime")


def test_access_heading_and_rows_for_every_model(app, auth_client, test_model):
    _add_model(app, "ack-model", True, needs_ack=True)
    soup = _soup(auth_client)
    for name in ("test-model", "ack-model"):
        access = _access(_row(soup, name))
        assert access.find("h3").get_text(strip=True) == "Access & Acknowledgments"
        assert list(_access_rows(_row(soup, name))) == ["Acknowledgment", "Early access"]
        # Left-aligned now: the column no longer right-aligns on wider screens.
        assert "text-sm-end" not in access["class"]


def test_access_neither_required(auth_client, test_model):
    row = _row(_soup(auth_client), "test-model")
    assert _access_rows(row) == {
        "Acknowledgment": ("not-required", "Not required"),
        "Early access": ("not-required", "Not required"),
    }
    assert _access(row).find("button") is None
    assert "Open" not in _access(row).get_text()


def test_access_acknowledge_pending(app, auth_client, test_model):
    _set_model(app, test_model["id"], needs_ack=True, ack_message="Read **this**.")
    row = _row(_soup(auth_client), "test-model")
    assert _access_rows(row) == {
        "Acknowledgment": ("pending", "Acknowledgment required"),
        "Early access": ("not-required", "Not required"),
    }
    buttons = _access_buttons(row)
    assert list(buttons) == ["acknowledge"]
    btn = buttons["acknowledge"]
    assert {"ack-btn", "bg-warning", "text-dark"} <= set(btn["class"])
    assert btn["aria-haspopup"] == "dialog"
    assert btn["data-name"] == "test-model"
    assert btn["data-notice"] == "Read **this**."
    assert btn.find("span", class_="visually-hidden").get_text() == " for test-model"
    # The button sits in its own requirement's row.
    assert btn.find_parent("dd").find_previous_sibling("dt").get_text(strip=True) == "Acknowledgment"


def test_access_early_access_pending(app, auth_client, test_model):
    _set_model(app, test_model["id"], early_access=True)
    row = _row(_soup(auth_client), "test-model")
    assert _access_rows(row) == {
        "Acknowledgment": ("not-required", "Not required"),
        "Early access": ("pending", "Acceptance required"),
    }
    buttons = _access_buttons(row)
    assert list(buttons) == ["early access"]
    assert {"ack-btn", "bg-info", "text-dark"} <= set(buttons["early access"]["class"])
    assert buttons["early access"].find_parent("dd").find_previous_sibling("dt").get_text(strip=True) == "Early access"


def test_access_both_pending(app, auth_client, test_model):
    _set_model(app, test_model["id"], needs_ack=True, early_access=True)
    row = _row(_soup(auth_client), "test-model")
    assert _access_rows(row) == {
        "Acknowledgment": ("pending", "Acknowledgment required"),
        "Early access": ("pending", "Acceptance required"),
    }
    buttons = _access_buttons(row)
    assert list(buttons) == ["acknowledge", "early access"]
    # Both open the same dialog with both notices.
    assert buttons["acknowledge"]["data-early-notice"] == buttons["early access"]["data-early-notice"]
    assert buttons["acknowledge"]["data-notice"] == buttons["early access"]["data-notice"]


def test_access_ack_accepted_early_access_pending(app, auth_client, test_user, test_model):
    _set_model(app, test_model["id"], needs_ack=True, early_access=True)
    _consent(app, test_user["id"], test_model["id"], consented_at=datetime(2026, 2, 3, 4, 5, 6))
    soup = _soup(auth_client)
    row = _row(soup, "test-model")
    assert _access_rows(row) == {"Acknowledgment": ("accepted", ""), "Early access": ("pending", "Acceptance required")}
    assert _accepted_span(row, "Acknowledgment")["data-utc"] == "2026-02-03T04:05:06Z"
    assert list(_access_buttons(row)) == ["early access"]
    # Still counts as needing acknowledgment until every requirement is accepted.
    assert _card(soup, "needs-ack")[0] == 1
    assert row.has_attr("data-needs-ack")


def test_access_both_accepted_show_their_own_dates(app, auth_client, test_user, test_model):
    _set_model(app, test_model["id"], needs_ack=True, early_access=True, ack_message="Read **this**.")
    _consent(app, test_user["id"], test_model["id"],
             consented_at=datetime(2026, 2, 3, 4, 5, 6), early_access_at=datetime(2026, 3, 4, 5, 6, 7))
    soup = _soup(auth_client)
    row = _row(soup, "test-model")
    assert _access_rows(row) == {"Acknowledgment": ("accepted", ""), "Early access": ("accepted", "")}
    # Each row carries its own requirement's timestamp, not the later of the two.
    assert _accepted_span(row, "Acknowledgment")["data-utc"] == "2026-02-03T04:05:06Z"
    assert _accepted_span(row, "Early access")["data-utc"] == "2026-03-04T05:06:07Z"
    for label in ("Acknowledgment", "Early access"):
        # Date-only text with the full local timestamp in a focusable tooltip, filled in by app.js.
        assert _accepted_span(row, label).has_attr("data-date")
    assert _access(row).find("button") is None
    assert _card(soup, "needs-ack")[0] == 0


def test_access_granted_pill_and_popover_are_gone(app, auth_client, test_user, test_model):
    _set_model(app, test_model["id"], needs_ack=True)
    _consent(app, test_user["id"], test_model["id"], consented_at=datetime(2026, 2, 3, 4, 5, 6))
    soup = _soup(auth_client)
    row = _row(soup, "test-model")
    assert _access_rows(row) == {"Acknowledgment": ("accepted", ""), "Early access": ("not-required", "Not required")}
    assert soup.find(class_="consent-info-btn") is None
    assert "granted" not in _access(row).get_text().lower()
    assert "toggleConsentPopover" not in str(soup)


def test_access_requirement_added_after_consent(app, auth_client, test_user, test_model):
    _set_model(app, test_model["id"], needs_ack=True)
    _consent(app, test_user["id"], test_model["id"], consented_at=datetime(2026, 1, 1))
    _set_model(app, test_model["id"], early_access=True)
    row = _row(_soup(auth_client), "test-model")
    assert _access_rows(row) == {"Acknowledgment": ("accepted", ""), "Early access": ("pending", "Acceptance required")}
    assert list(_access_buttons(row)) == ["early access"]


def test_access_requirement_removed_hides_its_old_date(app, auth_client, test_user, test_model):
    # A timestamp counts only while its requirement is in force.
    _set_model(app, test_model["id"], needs_ack=True)
    _consent(app, test_user["id"], test_model["id"],
             consented_at=datetime(2026, 1, 1), early_access_at=datetime(2026, 1, 2))
    row = _row(_soup(auth_client), "test-model")
    assert _access_rows(row) == {"Acknowledgment": ("accepted", ""), "Early access": ("not-required", "Not required")}


def test_access_dates_use_one_consent_query(app, auth_client, test_user):
    # Per-requirement dates come from one bulk read, not a query per row.
    ids = [_add_model(app, f"ack-{i}", True, needs_ack=True, early_access=True) for i in range(3)]
    for i in ids:
        _consent(app, test_user["id"], i, consented_at=datetime(2026, 1, 1))
    statements = []
    engine = db.engine

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        _soup(auth_client)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    # One read feeds the panel dates, the Needs Ack card and its filter alike.
    assert sum("entity_model_consents" in s for s in statements) == 1


def test_access_panel_and_needs_ack_agree_when_consent_lands_mid_render(app, auth_client, test_user, test_model, monkeypatch):
    # A first acceptance that lands right after the page reads the consent rows
    # must not show up in some parts of the page and not others: the panel, the
    # Needs Ack card and the row's filter attribute all come from the same read.
    # Every name the rows can be read through is wrapped, so a second read
    # anywhere in the render would see the new row and disagree.
    _set_model(app, test_model["id"], needs_ack=True)
    real = llm.consent_rows
    reads = []

    def read_then_accept(entity_id, model_config_ids):
        rows = real(entity_id, model_config_ids)
        if not reads:
            db.session.add(EntityModelConsent(entity_id=test_user["id"], model_config_id=test_model["id"],
                                              consented_at=datetime(2026, 2, 3, 4, 5, 6)))
            db.session.commit()
        reads.append(model_config_ids)
        return rows

    monkeypatch.setattr(llm, "consent_rows", read_then_accept)
    monkeypatch.setattr(models_page_routes, "consent_rows", read_then_accept)
    soup = _soup(auth_client)
    row = _row(soup, "test-model")
    # The render saw the model before the acceptance, everywhere on the page.
    assert _access_rows(row)["Acknowledgment"] == ("pending", "Acknowledgment required")
    assert _card(soup, "needs-ack")[0] == 1
    assert row.has_attr("data-needs-ack")
    assert _flags(row)[0] == ("bi-lock", "Needs acknowledgment")
    assert len(reads) == 1
    # The next page load sees the acceptance, again everywhere.
    monkeypatch.undo()
    soup = _soup(auth_client)
    row = _row(soup, "test-model")
    assert _access_rows(row)["Acknowledgment"][0] == "accepted"
    assert _card(soup, "needs-ack")[0] == 0
    assert not row.has_attr("data-needs-ack")
    assert _flags(row)[0] == ("bi-unlock", "Access granted")


def test_page_includes_ack_dialog(auth_client, test_model):
    soup = _soup(auth_client)
    assert soup.find(id="ackModal") is not None
    assert any("ack-consent.js" in (s.get("src") or "") for s in soup.find_all("script"))


# ── Empty state ───────────────────────────────────────────────────────────


def test_empty_state(auth_client):
    soup = _soup(auth_client)
    assert soup.find("li", class_="model-row") is None
    visible = [li.get_text(strip=True) for li in soup.find(id="model-list").find_all("li") if not li.has_attr("hidden")]
    assert visible == ["No models configured."]
    assert _card(soup, "models") == (0, "Listed models")
    assert _card(soup, "healthy") == (0, "All healthy")
