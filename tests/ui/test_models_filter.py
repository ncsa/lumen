"""Stat cards as filters on the Model Dashboard (/models): markup and the ?show= view.

The server renders the active card, ``aria-pressed`` and the rows outside the
view as hidden, so a reload or a shared link shows the view before any JS runs.
The click behaviour itself is run in a browser in test_models_filter_browser.py.
"""
from datetime import datetime

import pytest
from bs4 import BeautifulSoup

from lumen.extensions import db
from tests.ui.test_models_dashboard import _add_model, _consent


def _soup(auth_client, query=""):
    db.session.expire_all()  # see test_models_dashboard._soup
    return BeautifulSoup(auth_client.get(f"/models{query}").data, "html.parser")


def _cards(soup):
    return soup.find_all(class_="model-stat")


def _pressed(soup):
    return [c["data-stat"] for c in _cards(soup) if c["aria-pressed"] == "true"]


def _visible_rows(soup):
    return [r["data-model"] for r in soup.find_all("li", class_="model-row") if not r.has_attr("hidden")]


def _empty_line(soup):
    return soup.find("li", class_="model-filter-empty")


@pytest.fixture
def mixed_models(app, test_user):
    """One model per state; ``acked`` needs acknowledgment that this user already gave."""
    _add_model(app, "ok-model", True)
    _add_model(app, "degraded-model", True, False)
    _add_model(app, "down-model", False, early_access=True)
    _add_model(app, "bare-model", needs_ack=True)
    acked = _add_model(app, "acked-model", True, needs_ack=True)
    _consent(app, test_user["id"], acked, consented_at=datetime(2026, 1, 1))


def test_cards_are_buttons_with_data_show(auth_client, test_model):
    cards = _cards(_soup(auth_client))
    assert [(c.name, c.get("type"), c["data-stat"], c["data-show"]) for c in cards] == [
        ("button", "button", "models", "all"),
        ("button", "button", "healthy", "healthy"),
        ("button", "button", "early-access", "early_access"),
        ("button", "button", "needs-ack", "needs_ack"),
    ]
    assert all(c["aria-controls"] == "model-list" for c in cards)
    assert cards[0].find_parent(attrs={"role": "group"})["aria-label"] == "Filter models"


def test_rows_carry_filter_attributes(auth_client, mixed_models):
    rows = {r["data-model"]: r for r in _soup(auth_client).find_all("li", class_="model-row")}
    assert {name: r["data-health"] for name, r in rows.items()} == {
        "acked-model": "ok", "bare-model": "no_endpoints", "degraded-model": "degraded",
        "down-model": "down", "ok-model": "ok",
    }
    assert [n for n, r in rows.items() if r.has_attr("data-early-access")] == ["down-model"]
    # The early-access model needs acknowledgment too; the consented one does not.
    assert [n for n, r in rows.items() if r.has_attr("data-needs-ack")] == ["bare-model", "down-model"]


def test_needs_ack_attribute_drops_after_consent(app, auth_client, test_user):
    model_id = _add_model(app, "ack-model", True, needs_ack=True)
    assert _soup(auth_client).find("li", attrs={"data-model": "ack-model"}).has_attr("data-needs-ack")

    _consent(app, test_user["id"], model_id, consented_at=datetime(2026, 1, 1))
    assert not _soup(auth_client).find("li", attrs={"data-model": "ack-model"}).has_attr("data-needs-ack")


@pytest.mark.parametrize(("query", "pressed", "rows"), [
    ("", "models", ["acked-model", "bare-model", "degraded-model", "down-model", "ok-model"]),
    ("?show=healthy", "healthy", ["acked-model", "degraded-model", "ok-model"]),
    ("?show=early_access", "early-access", ["down-model"]),
    ("?show=needs_ack", "needs-ack", ["bare-model", "down-model"]),
    ("?show=down", "models", ["acked-model", "bare-model", "degraded-model", "down-model", "ok-model"]),
    ("?show=", "models", ["acked-model", "bare-model", "degraded-model", "down-model", "ok-model"]),
])
def test_show_param_sets_active_card_and_view(auth_client, mixed_models, query, pressed, rows):
    soup = _soup(auth_client, query)
    assert _pressed(soup) == [pressed]
    assert _visible_rows(soup) == rows
    assert _empty_line(soup).has_attr("hidden")


def test_filtering_never_changes_card_numbers(auth_client, mixed_models):
    counts = [[c.find(class_="model-stat-count").get_text(strip=True) for c in _cards(_soup(auth_client, q))]
              for q in ("", "?show=healthy", "?show=needs_ack")]
    assert counts == [["5", "3", "1", "2"]] * 3


def test_empty_view_shows_empty_line(auth_client, test_model):
    soup = _soup(auth_client, "?show=needs_ack")
    assert _visible_rows(soup) == []
    empty = _empty_line(soup)
    assert not empty.has_attr("hidden")
    assert empty.get_text(strip=True) == "No models in this view."


def test_no_empty_line_without_models(auth_client):
    soup = _soup(auth_client, "?show=healthy")
    assert _empty_line(soup) is None
    assert soup.find(id="model-list").get_text(strip=True) == "No models configured."
