"""Pin the coin-pool cap enforced by _parse_coin_pool_edit.

The admin pool editors reject any coin value whose magnitude reaches
_COIN_MAX_MAGNITUDE. Both sides of that boundary are pinned here so a
change to the cap fails the suite.
"""

from decimal import Decimal

import pytest

from lumen.blueprints.admin.routes import _COIN_MAX_MAGNITUDE, _parse_coin_pool_edit

_CAP_ERROR = "coin values must be less than 1,000,000"


def test_cap_matches_coin_column_precision():
    # The coin columns are Numeric(12, 6) — 6 integer digits — in
    # lumen/models/group_limit.py, entity_limit.py and entity_balance.py.
    # The cap must stay at 10**6 so every accepted value fits them.
    assert _COIN_MAX_MAGNITUDE == Decimal(10) ** 6


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"max_coins": "999999"}, (Decimal("999999"), None)),
        # Largest value Numeric(12, 6) can hold.
        ({"max_coins": "999999.999999"}, (Decimal("999999.999999"), None)),
        ({"refresh_coins": "999999"}, (None, Decimal("999999"))),
        # -2 (unlimited) bypasses the cap.
        ({"max_coins": -2}, (Decimal(-2), None)),
    ],
)
def test_values_below_cap_are_accepted(data, expected):
    assert _parse_coin_pool_edit(data) == (expected, None)


@pytest.mark.parametrize(
    "data",
    [
        {"max_coins": "1000000"},
        {"refresh_coins": "1000000"},
        {"max_coins": 10, "refresh_coins": "1000000"},
    ],
)
def test_values_at_cap_are_rejected(data):
    assert _parse_coin_pool_edit(data) == (None, _CAP_ERROR)
