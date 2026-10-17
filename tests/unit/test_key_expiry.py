"""API key expiry: api.keys.ttl parsing, resolve_key_ttl, and the expirer."""
import logging
from datetime import timedelta

import pytest
from sqlalchemy import select

from lumen.extensions import db
from lumen.models.api_key import APIKey
from lumen.services.config_watcher import apply_hot_config, validate_config_structure
from lumen.services.key_expiry import (
    MAX_KEY_TTL,
    expire_keys,
    key_expires_at,
    key_ttl_choices,
    parse_key_ttl,
    resolve_key_ttl,
    revoke_if_expired,
)
from lumen.timeutils import utcnow

DAY = 86400


@pytest.mark.parametrize("value, seconds", [
    (0, 0), ("0", 0), ("12h", 12 * 3600), ("30d", 30 * DAY), ("8w", 8 * 7 * DAY), (" 1d ", DAY),
])
def test_parse_key_ttl(value, seconds):
    assert parse_key_ttl(value) == seconds


@pytest.mark.parametrize("value", ["abc", "-1d", "-1", "", "  ", None, "30", 3600, "1.5d", "1m", "1D", True, [], {},
                                   "5219w", "36501d", "999999999999999999999w"])
def test_parse_key_ttl_rejects(value):
    with pytest.raises(ValueError):
        parse_key_ttl(value)


@pytest.mark.parametrize("ttl, seconds", [(0, 0), ("0", 0), ("12h", 12 * 3600), ("30d", 30 * DAY), ("8w", 56 * DAY)])
def test_hot_config_sets_key_ttl(app, restore_config, caplog, ttl, seconds):
    with caplog.at_level(logging.WARNING, logger="lumen.services.config_watcher"):
        apply_hot_config(app, {"api": {"keys": {"ttl": ttl}}})
    assert app.config["KEY_TTL"] == seconds
    assert "api.keys.ttl" not in caplog.text


def test_parse_key_ttl_accepts_up_to_100_years():
    assert parse_key_ttl("36500d") == MAX_KEY_TTL


@pytest.mark.parametrize("ttl", ["garbage", "-1d", "-5", "", None, "999999999999999999999w"])
def test_hot_config_invalid_ttl_warns_and_falls_back(app, restore_config, caplog, ttl):
    app.config["KEY_TTL"] = 99
    with caplog.at_level(logging.WARNING, logger="lumen.services.config_watcher"):
        apply_hot_config(app, {"api": {"keys": {"ttl": ttl}}})
    assert app.config["KEY_TTL"] == 0
    assert "api.keys.ttl" in caplog.text


def test_hot_config_absent_ttl_is_zero_without_warning(app, restore_config, caplog):
    with caplog.at_level(logging.WARNING, logger="lumen.services.config_watcher"):
        apply_hot_config(app, {})
    assert app.config["KEY_TTL"] == 0
    assert "api.keys.ttl" not in caplog.text


@pytest.mark.parametrize("api", ["oops", ["keys"], {"keys": "oops"}])
def test_hot_config_malformed_api_section_is_zero(app, restore_config, api):
    app.config["KEY_TTL"] = 99
    apply_hot_config(app, {"api": api})
    assert app.config["KEY_TTL"] == 0


def test_hot_reload_updates_key_ttl(app, restore_config):
    apply_hot_config(app, {"api": {"keys": {"ttl": "30d"}}})
    assert app.config["KEY_TTL"] == 30 * DAY
    apply_hot_config(app, {"api": {"keys": {"ttl": "12h"}}})
    assert app.config["KEY_TTL"] == 12 * 3600
    apply_hot_config(app, {"api": {"keys": {"ttl": "0"}}})
    assert app.config["KEY_TTL"] == 0


def _valid_config(**api):
    return {"version": 3, "models": [], "api": api}


@pytest.mark.parametrize("ttl", ["abc", "-1d", "", None, "30", 5, "999999999999999999999w"])
def test_validate_config_structure_rejects_bad_ttl(ttl):
    errors = validate_config_structure(_valid_config(keys={"ttl": ttl}))
    assert any("api.keys.ttl" in e for e in errors)


@pytest.mark.parametrize("ttl", [0, "0", "12h", "30d", "8w"])
def test_validate_config_structure_accepts_good_ttl(ttl):
    assert validate_config_structure(_valid_config(keys={"ttl": ttl})) == []


def test_validate_config_structure_accepts_missing_ttl():
    assert validate_config_structure(_valid_config()) == []


# resolve_key_ttl truth table: cap 0 or N, against requested none, 0, below,
# at and above the cap.
CAP = 30 * DAY


@pytest.mark.parametrize("cap, requested, expected", [
    (0, None, 0),
    (0, 0, 0),
    (0, DAY, DAY),
    (0, CAP, CAP),
    (0, 10 * CAP, 10 * CAP),
    (0, MAX_KEY_TTL, MAX_KEY_TTL),
    (CAP, None, CAP),
    (CAP, 1, 1),
    (CAP, DAY, DAY),
    (CAP, CAP, CAP),
])
def test_resolve_key_ttl_allowed(app, restore_config, cap, requested, expected):
    app.config["KEY_TTL"] = cap
    with app.app_context():
        assert resolve_key_ttl(requested) == expected


@pytest.mark.parametrize("cap, requested", [
    (CAP, 0),          # never is not allowed under a cap
    (CAP, CAP + 1),    # above the cap
    (CAP, -1),
    (0, -1),
    (0, MAX_KEY_TTL + 1),  # no cap still has the overflow ceiling
    (0, "30"),
    (0, 1.5),
    (CAP, True),
])
def test_resolve_key_ttl_rejected(app, restore_config, cap, requested):
    app.config["KEY_TTL"] = cap
    with app.app_context():
        with pytest.raises(ValueError):
            resolve_key_ttl(requested)


def test_key_expires_at():
    assert key_expires_at(0) is None
    expires = key_expires_at(DAY)
    assert abs(expires - (utcnow() + timedelta(days=1))) < timedelta(seconds=5)
    assert expires.tzinfo is None


def _add_key(name, entity_id, expires_at=None, revoked_at=None, revoked_by=None):
    key = APIKey(entity_id=entity_id, name=name, key_hash=name.ljust(64, "0"),
                 expires_at=expires_at, revoked_at=revoked_at, revoked_by_entity_id=revoked_by)
    db.session.add(key)
    return key


def _state(name):
    key = db.session.execute(select(APIKey).where(APIKey.name == name)).scalar_one()
    return key.revoked_at, key.revoked_by_entity_id


def test_expire_keys(app, test_user):
    now = utcnow()
    overdue_at = now - timedelta(hours=1)
    earlier = now - timedelta(days=2)
    with app.app_context():
        uid = test_user["id"]
        _add_key("overdue", uid, expires_at=overdue_at)
        _add_key("future", uid, expires_at=now + timedelta(hours=1))
        _add_key("never", uid)
        _add_key("revoked_overdue", uid, expires_at=overdue_at, revoked_at=earlier, revoked_by=uid)
        db.session.commit()

        assert expire_keys() == 1
        assert _state("overdue") == (overdue_at, None)
        assert _state("future") == (None, None)
        assert _state("never") == (None, None)
        assert _state("revoked_overdue") == (earlier, uid)

        # Idempotent: a second run changes nothing.
        assert expire_keys() == 0
        assert _state("overdue") == (overdue_at, None)


def test_revoke_if_expired(app, test_user):
    now = utcnow()
    overdue_at = now - timedelta(minutes=1)
    with app.app_context():
        uid = test_user["id"]
        overdue = _add_key("overdue", uid, expires_at=overdue_at)
        future = _add_key("future", uid, expires_at=now + timedelta(hours=1))
        never = _add_key("never", uid)
        db.session.commit()

        assert revoke_if_expired(overdue) is True
        assert (overdue.revoked_at, overdue.revoked_by_entity_id) == (overdue_at, None)
        assert revoke_if_expired(overdue) is False  # already revoked
        assert revoke_if_expired(future) is False
        assert revoke_if_expired(never) is False
        assert _state("future") == (None, None)
        assert _state("never") == (None, None)


@pytest.mark.parametrize("cap, expected", [
    (0, [(DAY, "1 day", False), (7 * DAY, "7 days", False), (30 * DAY, "30 days", False),
         (90 * DAY, "90 days", False), (365 * DAY, "1 year", False), (0, "Never", True)]),
    (30 * DAY, [(DAY, "1 day", False), (7 * DAY, "7 days", False), (30 * DAY, "30 days", True)]),
    (8 * 7 * DAY, [(DAY, "1 day", False), (7 * DAY, "7 days", False), (30 * DAY, "30 days", False),
                   (56 * DAY, "8 weeks", True)]),
    (12 * 3600, [(12 * 3600, "12 hours", True)]),
    (DAY, [(DAY, "1 day", True)]),
    (10 * DAY, [(DAY, "1 day", False), (7 * DAY, "7 days", False), (10 * DAY, "10 days", True)]),
    (730 * DAY, [(DAY, "1 day", False), (7 * DAY, "7 days", False), (30 * DAY, "30 days", False),
                 (90 * DAY, "90 days", False), (365 * DAY, "1 year", False), (730 * DAY, "2 years", True)]),
    (3650 * DAY, [(DAY, "1 day", False), (7 * DAY, "7 days", False), (30 * DAY, "30 days", False),
                  (90 * DAY, "90 days", False), (365 * DAY, "1 year", False), (3650 * DAY, "10 years", True)]),
])
def test_key_ttl_choices(app, restore_config, cap, expected):
    app.config["KEY_TTL"] = cap
    with app.app_context():
        assert key_ttl_choices() == expected
