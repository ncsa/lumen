"""Sortable columns on the Model Dashboard (/models): the server order for ?sort= / ?dir=.

The server renders the rows in the requested order with the active button's
arrow and label, so a reload or a shared link shows the same order before any
JS runs. The click behaviour itself is run in a browser in
test_models_filter_browser.py.
"""
import pytest
from bs4 import BeautifulSoup

from lumen.extensions import db
from tests.ui.test_models_dashboard import _add_model


def _soup(auth_client, query=""):
    db.session.expire_all()  # see test_models_dashboard._soup
    return BeautifulSoup(auth_client.get(f"/models{query}").data, "html.parser")


def _order(auth_client, query=""):
    return [r["data-model"] for r in _soup(auth_client, query).find_all("li", class_="model-row")]


@pytest.fixture
def priced_models(app):
    """Prices and limits chosen so each sort gives a different order."""
    _add_model(app, "b-mid", input_cost_per_million=2, output_cost_per_million=5, context_window=64_000, max_output_tokens=8_000)
    _add_model(app, "a-cheap-out", input_cost_per_million=1, output_cost_per_million=1, context_window=128_000)
    _add_model(app, "c-cheap-in", input_cost_per_million=1, output_cost_per_million=3, max_output_tokens=32_000)
    _add_model(app, "d-dear", input_cost_per_million=9, output_cost_per_million=9, context_window=1_000_000, max_output_tokens=16_000)


def test_default_order_is_name(auth_client, priced_models):
    assert _order(auth_client) == ["a-cheap-out", "b-mid", "c-cheap-in", "d-dear"]


def test_price_puts_cheapest_input_first_then_cheaper_output(auth_client, priced_models):
    # a-cheap-out and c-cheap-in share the input price; the cheaper output comes first.
    assert _order(auth_client, "?sort=price") == ["a-cheap-out", "c-cheap-in", "b-mid", "d-dear"]


def test_price_desc_reverses(auth_client, priced_models):
    assert _order(auth_client, "?sort=price&dir=desc") == ["d-dear", "b-mid", "c-cheap-in", "a-cheap-out"]


def test_context_puts_biggest_first(auth_client, priced_models):
    assert _order(auth_client, "?sort=context") == ["d-dear", "a-cheap-out", "b-mid", "c-cheap-in"]


@pytest.mark.parametrize(("query", "order"), [
    ("?sort=context&dir=asc", ["b-mid", "a-cheap-out", "d-dear", "c-cheap-in"]),
    ("?sort=context&dir=desc", ["d-dear", "a-cheap-out", "b-mid", "c-cheap-in"]),
    ("?sort=max_output", ["c-cheap-in", "d-dear", "b-mid", "a-cheap-out"]),
    ("?sort=max_output&dir=asc", ["b-mid", "d-dear", "c-cheap-in", "a-cheap-out"]),
])
def test_missing_values_sort_last_in_both_directions(auth_client, priced_models, query, order):
    assert _order(auth_client, query) == order


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_ties_fall_back_to_name(app, auth_client, direction):
    for name in ("zeta", "Alpha", "mid"):
        _add_model(app, name, context_window=8_000, max_output_tokens=None)
    _add_model(app, "beta")  # no context window: last
    assert _order(auth_client, f"?sort=context&dir={direction}") == ["Alpha", "mid", "zeta", "beta"]
    assert _order(auth_client, f"?sort=price&dir={direction}") == ["Alpha", "beta", "mid", "zeta"]


def test_name_desc_reverses(auth_client, priced_models):
    assert _order(auth_client, "?sort=name&dir=desc") == ["d-dear", "c-cheap-in", "b-mid", "a-cheap-out"]


@pytest.mark.parametrize("query", ["?sort=bogus", "?sort=price&dir=up", "?dir=desc", "?sort="])
def test_unknown_key_or_dir_gives_name_order(auth_client, priced_models, query):
    assert _order(auth_client, query) == ["a-cheap-out", "b-mid", "c-cheap-in", "d-dear"]


def test_sort_keeps_the_card_filter(app, auth_client, priced_models):
    _add_model(app, "e-early", early_access=True, input_cost_per_million=0)
    soup = _soup(auth_client, "?show=early_access&sort=price")
    rows = soup.find_all("li", class_="model-row")
    assert rows[0]["data-model"] == "e-early"
    assert [r["data-model"] for r in rows if not r.has_attr("hidden")] == ["e-early"]


def _buttons(soup):
    return {b["data-sort"]: b for b in soup.find_all("button", class_="model-sort-btn")}


@pytest.mark.parametrize(("query", "active", "arrow", "labels"), [
    ("", "name", "sort-asc", {
        "name": "Sort by model, sorted A to Z", "context": "Sort by context, high to low",
        "max_output": "Sort by max output, high to low", "price": "Sort by price, cheapest first"}),
    ("?sort=price", "price", "sort-asc", {
        "name": "Sort by model, A to Z", "context": "Sort by context, high to low",
        "max_output": "Sort by max output, high to low", "price": "Sort by price, sorted cheapest first"}),
    ("?sort=price&dir=desc", "price", "sort-desc", {"price": "Sort by price, sorted most expensive first"}),
    ("?sort=context", "context", "sort-desc", {"context": "Sort by context, sorted high to low"}),
    ("?sort=max_output&dir=asc", "max_output", "sort-asc", {"max_output": "Sort by max output, sorted low to high"}),
    ("?sort=name&dir=desc", "name", "sort-desc", {"name": "Sort by model, sorted Z to A"}),
])
def test_active_button_shows_arrow_and_label(auth_client, test_model, query, active, arrow, labels):
    soup = _soup(auth_client, query)
    buttons = _buttons(soup)
    assert list(buttons) == ["name", "context", "max_output", "price"]
    assert [b.get_text(strip=True).rstrip("▲▼") for b in buttons.values()] == ["Model", "Context", "Max output", "Price"]
    assert [k for k, b in buttons.items() if {"sort-asc", "sort-desc"} & set(b["class"])] == [active]
    assert arrow in buttons[active]["class"]
    for key, label in labels.items():
        assert buttons[key]["aria-label"] == label
    assert [b["data-first"] for b in buttons.values()] == ["asc", "desc", "desc", "asc"]
    header = soup.find(class_="model-list-columns")
    assert not header.has_attr("aria-hidden")


def test_rows_carry_sort_values(app, auth_client):
    _add_model(app, "Some-Model", context_window=128_000, input_cost_per_million=1.5, output_cost_per_million=3)
    _add_model(app, "another", max_output_tokens=0, input_cost_per_million=0, output_cost_per_million=0)
    rows = {r["data-model"]: r for r in _soup(auth_client, "?sort=price").find_all("li", class_="model-row")}
    row = rows["Some-Model"]
    assert row["data-sort-name"] == "1"  # its place in the name order, after "another"
    assert row["data-sort-context"] == "128000"
    assert row["data-sort-max-output"] == ""
    assert float(row["data-sort-input"]) == 1.5
    assert float(row["data-sort-output"]) == 3
    # A token limit of 0 is missing, like the dash it shows; a price of 0 is free.
    zero = rows["another"]
    assert zero["data-sort-name"] == "0"
    assert zero["data-sort-max-output"] == ""
    assert float(zero["data-sort-input"]) == 0
    assert float(zero["data-sort-output"]) == 0


@pytest.mark.parametrize(("key", "field"), [("context", "context_window"), ("max_output", "max_output_tokens")])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_zero_token_limit_sorts_last_like_missing(app, auth_client, key, field, direction):
    _add_model(app, "a-zero", **{field: 0})
    _add_model(app, "b-small", **{field: 8_000})
    _add_model(app, "c-unset", **{field: None})
    _add_model(app, "d-big", **{field: 128_000})
    present = ["b-small", "d-big"] if direction == "asc" else ["d-big", "b-small"]
    assert _order(auth_client, f"?sort={key}&dir={direction}") == [*present, "a-zero", "c-unset"]


@pytest.mark.parametrize(("direction", "order"), [("asc", ["b-free", "a-paid"]), ("desc", ["a-paid", "b-free"])])
def test_zero_price_sorts_as_free(app, auth_client, direction, order):
    _add_model(app, "a-paid", input_cost_per_million=1, output_cost_per_million=1)
    _add_model(app, "b-free", input_cost_per_million=0, output_cost_per_million=0)
    assert _order(auth_client, f"?sort=price&dir={direction}") == order


def test_name_order_by_code_point(app, auth_client):
    # U+FF41 (full-width a) is one UTF-16 unit above U+D801 (the lead surrogate of
    # U+10428), so JavaScript would order them the other way round.
    for name in ("\U00010428-model", "\uff41-model", "a-model"):
        _add_model(app, name)
    assert _order(auth_client) == ["a-model", "\uff41-model", "\U00010428-model"]
    assert _order(auth_client, "?sort=name&dir=desc") == ["\U00010428-model", "\uff41-model", "a-model"]
