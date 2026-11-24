"""API key expiry: api.keys TTL parsing, resolve_key_ttl per key type, and the expirer."""
import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from lumen.extensions import db
from lumen.models.api_key import KEY_TYPE_ADMIN, KEY_TYPE_MONITORING, KEY_TYPE_NORMAL, APIKey
from lumen.services import key_expiry
from lumen.services.config_watcher import apply_hot_config, validate_config_structure
from lumen.services.key_expiry import (
    ADMIN_KEY_MAX_TTL,
    KEY_TTL_PRESETS,
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
        apply_hot_config(app, {"api": {"keys": {"max_ttl": ttl}}})
    assert app.config["KEY_TTL"] == seconds
    assert caplog.text == ""


def test_hot_config_legacy_ttl_is_max_ttl_with_deprecation_warning(app, restore_config, caplog):
    with caplog.at_level(logging.WARNING, logger="lumen.services.config_watcher"):
        apply_hot_config(app, {"api": {"keys": {"ttl": "30d"}}})
    assert app.config["KEY_TTL"] == 30 * DAY
    assert "api.keys.ttl is deprecated" in caplog.text
    assert "api.keys.max_ttl" in caplog.text


def test_hot_config_max_ttl_wins_over_legacy_ttl(app, restore_config, caplog):
    with caplog.at_level(logging.WARNING, logger="lumen.services.config_watcher"):
        apply_hot_config(app, {"api": {"keys": {"ttl": "30d", "max_ttl": "90d"}}})
    assert app.config["KEY_TTL"] == 90 * DAY
    assert "api.keys.ttl is deprecated and ignored" in caplog.text


def test_hot_config_sets_per_type_and_default_ttl(app, restore_config, caplog):
    with caplog.at_level(logging.WARNING, logger="lumen.services.config_watcher"):
        apply_hot_config(app, {"api": {"keys": {"max_admin_ttl": "1d", "max_monitor_ttl": "30d",
                                                "default_ttl": "2w"}}})
    assert app.config["KEY_MAX_ADMIN_TTL"] == DAY
    assert app.config["KEY_MAX_MONITOR_TTL"] == 30 * DAY
    assert app.config["KEY_DEFAULT_TTL"] == 14 * DAY
    assert caplog.text == ""


def test_hot_config_default_ttl_zero_is_never(app, restore_config):
    apply_hot_config(app, {"api": {"keys": {"default_ttl": 0}}})
    assert app.config["KEY_DEFAULT_TTL"] == 0


@pytest.mark.parametrize("name, unset", [
    ("max_ttl", 0), ("max_admin_ttl", ADMIN_KEY_MAX_TTL), ("max_monitor_ttl", 0), ("default_ttl", None),
])
def test_hot_config_invalid_per_type_ttl_warns_and_falls_back(app, restore_config, caplog, name, unset):
    config_name = {"max_ttl": "KEY_TTL", "max_admin_ttl": "KEY_MAX_ADMIN_TTL",
                   "max_monitor_ttl": "KEY_MAX_MONITOR_TTL", "default_ttl": "KEY_DEFAULT_TTL"}[name]
    app.config[config_name] = 99
    with caplog.at_level(logging.WARNING, logger="lumen.services.config_watcher"):
        apply_hot_config(app, {"api": {"keys": {name: "garbage"}}})
    assert app.config[config_name] == unset
    assert f"api.keys.{name} must be 0 or a duration" in caplog.text


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
    assert app.config["KEY_MAX_ADMIN_TTL"] == ADMIN_KEY_MAX_TTL
    assert app.config["KEY_MAX_MONITOR_TTL"] == 0
    assert app.config["KEY_DEFAULT_TTL"] is None
    assert "api.keys" not in caplog.text


@pytest.mark.parametrize("api", ["oops", ["keys"], {"keys": "oops"}])
def test_hot_config_malformed_api_section_is_zero(app, restore_config, api):
    app.config["KEY_TTL"] = 99
    apply_hot_config(app, {"api": api})
    assert app.config["KEY_TTL"] == 0


def test_hot_reload_updates_key_ttl(app, restore_config):
    apply_hot_config(app, {"api": {"keys": {"max_ttl": "30d", "default_ttl": "1d"}}})
    assert (app.config["KEY_TTL"], app.config["KEY_DEFAULT_TTL"]) == (30 * DAY, DAY)
    apply_hot_config(app, {"api": {"keys": {"max_ttl": "12h"}}})
    assert (app.config["KEY_TTL"], app.config["KEY_DEFAULT_TTL"]) == (12 * 3600, None)
    apply_hot_config(app, {"api": {"keys": {"max_ttl": "0"}}})
    assert app.config["KEY_TTL"] == 0


def _valid_config(**api):
    return {"version": 3, "models": [], "api": api}


TTL_SETTINGS = ["ttl", "max_ttl", "max_admin_ttl", "max_monitor_ttl", "default_ttl"]


@pytest.mark.parametrize("name", TTL_SETTINGS)
@pytest.mark.parametrize("ttl", ["abc", "-1d", "", None, "30", 5, "999999999999999999999w"])
def test_validate_config_structure_rejects_bad_ttl(name, ttl):
    errors = validate_config_structure(_valid_config(keys={name: ttl}))
    assert errors == [f"api.keys.{name} must be 0 or a duration such as 12h, 30d or 8w"]


@pytest.mark.parametrize("name", TTL_SETTINGS)
@pytest.mark.parametrize("ttl", [0, "0", "12h", "30d", "8w"])
def test_validate_config_structure_accepts_good_ttl(name, ttl):
    assert validate_config_structure(_valid_config(keys={name: ttl})) == []


def test_validate_config_structure_has_no_pair_checks():
    """A default larger than a maximum is lowered at use, never rejected."""
    keys = {"max_ttl": "1d", "max_admin_ttl": "1h", "max_monitor_ttl": "1d", "default_ttl": 0, "ttl": "1w"}
    assert validate_config_structure(_valid_config(keys=keys)) == []


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


def test_expire_keys_revokes_a_key_expiring_exactly_now(app, test_user, monkeypatch):
    # A key is expired the moment the clock reaches expires_at, not only after it.
    now = utcnow()
    monkeypatch.setattr("lumen.services.key_expiry.utcnow", lambda: now)
    with app.app_context():
        _add_key("boundary", test_user["id"], expires_at=now)
        _add_key("just_future", test_user["id"], expires_at=now + timedelta(seconds=1))
        db.session.commit()

        assert expire_keys() == 1
        assert _state("boundary") == (now, None)
        assert _state("just_future") == (None, None)


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


def test_revoke_if_expired_at_exact_expiry(app, test_user, monkeypatch):
    """A key is expired once the clock reaches ``expires_at``."""
    now = utcnow().replace(microsecond=0)
    monkeypatch.setattr("lumen.services.key_expiry.utcnow", lambda: now)
    with app.app_context():
        uid = test_user["id"]
        boundary = _add_key("boundary", uid, expires_at=now)
        just_future = _add_key("just_future", uid, expires_at=now + timedelta(seconds=1))
        db.session.commit()

        assert revoke_if_expired(boundary) is True
        assert _state("boundary") == (now, None)
        assert revoke_if_expired(boundary) is False  # already revoked
        assert boundary.revoked_at == now
        assert revoke_if_expired(just_future) is False
        assert _state("just_future") == (None, None)


@pytest.mark.parametrize("cap, expected", [
    (0, [(DAY, "1 day", False), (7 * DAY, "7 days", False), (30 * DAY, "30 days", False),
         (90 * DAY, "90 days", False), (365 * DAY, "1 year", False), (0, "Never", True)]),
    (30 * DAY, [(DAY, "1 day", False), (7 * DAY, "7 days", False), (30 * DAY, "30 days", True)]),
    (8 * 7 * DAY, [(DAY, "1 day", False), (7 * DAY, "7 days", False), (30 * DAY, "30 days", False),
                   (56 * DAY, "8 weeks", True)]),
    (12 * 3600, [(12 * 3600, "12 hours", True)]),
    (DAY, [(DAY, "1 day", True)]),
    # A cap equal to the 7-day preset keeps the preset's own label; _ttl_label() would give "1 week".
    (7 * DAY, [(DAY, "1 day", False), (7 * DAY, "7 days", True)]),
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


@pytest.mark.parametrize("seconds, label", KEY_TTL_PRESETS)
def test_key_ttl_choices_cap_equal_to_preset(app, restore_config, seconds, label):
    """A cap equal to a preset offers that preset, with its own label, as the only selected choice."""
    app.config["KEY_TTL"] = seconds
    with app.app_context():
        choices = key_ttl_choices()
    assert choices[-1] == (seconds, label, True)
    assert [c for c in choices if c[2]] == [(seconds, label, True)]
    offered = [s for s, _, _ in choices]
    assert offered == sorted(set(offered))


@pytest.mark.parametrize("cap, selected, expected", [
    # An allowed preset is pre-selected instead of the cap.
    (30 * DAY, 7 * DAY, [(DAY, "1 day", False), (7 * DAY, "7 days", True), (30 * DAY, "30 days", False)]),
    # A non-preset is offered in order and pre-selected.
    (30 * DAY, 2 * 3600, [(2 * 3600, "2 hours", True), (DAY, "1 day", False), (7 * DAY, "7 days", False),
                          (30 * DAY, "30 days", False)]),
    (0, 2 * DAY, [(DAY, "1 day", False), (2 * DAY, "2 days", True), (7 * DAY, "7 days", False),
                  (30 * DAY, "30 days", False), (90 * DAY, "90 days", False), (365 * DAY, "1 year", False),
                  (0, "Never", False)]),
    # Nothing, or something the cap no longer allows, falls back to the cap.
    (DAY, None, [(DAY, "1 day", True)]),
    (DAY, 7 * DAY, [(DAY, "1 day", True)]),
    (DAY, 0, [(DAY, "1 day", True)]),
])
def test_key_ttl_choices_selected(app, restore_config, cap, selected, expected):
    app.config["KEY_TTL"] = cap
    with app.app_context():
        assert key_ttl_choices(selected) == expected


def test_revoke_if_expired_leaves_a_key_refreshed_after_it_was_loaded(app, test_user):
    """An OAuth overwrite that refreshes the row after a request loaded it overdue must survive.

    The refresh commits through a separate connection, so the request's object
    still holds the old, overdue expiry.
    """
    with app.app_context():
        key = _add_key("race", test_user["id"], expires_at=utcnow() - timedelta(minutes=1))
        db.session.commit()
        future = utcnow() + timedelta(days=1)
        with db.engine.begin() as conn:
            conn.execute(update(APIKey).where(APIKey.id == key.id)
                         .values(expires_at=future, key_hash="new".ljust(64, "0")))

        # The old request is still rejected...
        assert revoke_if_expired(key) is True
        # ...but the refreshed key stays live, with its new expiry.
        db.session.refresh(key)
        assert (key.revoked_at, key.expires_at) == (None, future)


def test_revoke_if_expired_keeps_a_manual_revoke_committed_after_it_was_loaded(app, test_user):
    with app.app_context():
        uid = test_user["id"]
        key = _add_key("raced", uid, expires_at=utcnow() - timedelta(minutes=1))
        db.session.commit()
        revoked_at = utcnow()
        with db.engine.begin() as conn:
            conn.execute(update(APIKey).where(APIKey.id == key.id)
                         .values(revoked_at=revoked_at, revoked_by_entity_id=uid))

        assert revoke_if_expired(key) is True
        db.session.refresh(key)
        assert (key.revoked_at, key.revoked_by_entity_id) == (revoked_at, uid)


def test_key_ttl_choices_offers_an_odd_selected_ttl_in_seconds(app, restore_config):
    """A selected TTL that is not a whole number of hours is labelled in seconds."""
    app.config["KEY_TTL"] = 0
    with app.app_context():
        choices = key_ttl_choices(5400)
    assert (5400, "5400 seconds", True) in choices
    assert [s for s, _, _ in choices] == sorted(s for s, _, _ in choices if s) + [0]


# ── start_key_expirer: the background loop, one tick at a time ────────────────

class _StopLoop(Exception):
    """Raised from the patched sleep to end the loop."""


def _run_expirer(app, monkeypatch, ticks):
    """Start the expirer with a fake thread, run its loop for ``ticks`` ticks; return the sleeps."""
    threads, sleeps = [], []

    class FakeThread:
        def __init__(self, target, daemon):
            self.target, self.daemon, self.started = target, daemon, False
            threads.append(self)

        def start(self):
            self.started = True

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == ticks:
            raise _StopLoop

    monkeypatch.setattr(key_expiry, "threading", SimpleNamespace(Thread=FakeThread))
    monkeypatch.setattr(key_expiry, "time", SimpleNamespace(sleep=sleep))
    key_expiry.start_key_expirer(app)
    (thread,) = threads
    assert thread.daemon is True and thread.started is True
    with pytest.raises(_StopLoop):
        thread.target()
    return sleeps


def test_start_key_expirer_revokes_overdue_keys_each_tick(app, test_user, monkeypatch):
    overdue_at = utcnow() - timedelta(hours=1)
    with app.app_context():
        _add_key("overdue", test_user["id"], expires_at=overdue_at)
        db.session.commit()

    assert _run_expirer(app, monkeypatch, ticks=1) == [key_expiry.EXPIRE_INTERVAL]

    with app.app_context():
        assert _state("overdue") == (overdue_at, None)


def test_start_key_expirer_logs_a_failed_tick_and_keeps_running(app, monkeypatch, caplog):
    calls = []

    def expire():
        calls.append(1)
        raise RuntimeError("db down")

    monkeypatch.setattr(key_expiry, "expire_keys", expire)
    with caplog.at_level(logging.ERROR, logger="lumen.services.key_expiry"):
        sleeps = _run_expirer(app, monkeypatch, ticks=2)
    assert len(calls) == 2
    assert sleeps == [key_expiry.EXPIRE_INTERVAL] * 2
    assert caplog.text.count("key expirer error") == 2


# --- per key type ----------------------------------------------------------

WEEK = 7 * DAY


def test_admin_key_max_ttl_is_one_week():
    assert ADMIN_KEY_MAX_TTL == WEEK


def _limits(app, max_ttl=0, max_admin=ADMIN_KEY_MAX_TTL, max_monitor=0, default=None):
    app.config.update(KEY_TTL=max_ttl, KEY_MAX_ADMIN_TTL=max_admin, KEY_MAX_MONITOR_TTL=max_monitor,
                      KEY_DEFAULT_TTL=default)


@pytest.mark.parametrize("key_type, limits, requested, expected", [
    # Nothing set: normal and monitoring never expire by default, admin keys get a week.
    (KEY_TYPE_NORMAL, {}, None, 0),
    (KEY_TYPE_ADMIN, {}, None, WEEK),
    (KEY_TYPE_MONITORING, {}, None, 0),
    (KEY_TYPE_ADMIN, {}, WEEK, WEEK),
    (KEY_TYPE_ADMIN, {}, 3600, 3600),
    # Each type follows only its own maximum.
    (KEY_TYPE_NORMAL, {"max_ttl": DAY}, None, DAY),
    (KEY_TYPE_ADMIN, {"max_ttl": DAY}, None, WEEK),
    (KEY_TYPE_ADMIN, {"max_ttl": DAY}, WEEK, WEEK),
    (KEY_TYPE_MONITORING, {"max_ttl": DAY}, None, 0),
    (KEY_TYPE_MONITORING, {"max_ttl": DAY}, 0, 0),
    (KEY_TYPE_MONITORING, {"max_ttl": DAY}, 365 * DAY, 365 * DAY),
    (KEY_TYPE_MONITORING, {}, MAX_KEY_TTL, MAX_KEY_TTL),
    (KEY_TYPE_ADMIN, {"max_admin": DAY}, None, DAY),
    (KEY_TYPE_ADMIN, {"max_admin": 0}, None, 0),
    (KEY_TYPE_ADMIN, {"max_admin": 0}, 0, 0),
    (KEY_TYPE_ADMIN, {"max_admin": 0}, 365 * DAY, 365 * DAY),
    (KEY_TYPE_MONITORING, {"max_monitor": 30 * DAY}, None, 30 * DAY),
    (KEY_TYPE_MONITORING, {"max_monitor": 30 * DAY}, DAY, DAY),
    # default_ttl 30d with the rest unset: 30 days, admin lowered to its week.
    (KEY_TYPE_NORMAL, {"default": 30 * DAY}, None, 30 * DAY),
    (KEY_TYPE_NORMAL, {"default": 30 * DAY}, 0, 0),
    (KEY_TYPE_MONITORING, {"default": 30 * DAY}, None, 30 * DAY),
    (KEY_TYPE_MONITORING, {"default": 30 * DAY}, 0, 0),
    (KEY_TYPE_ADMIN, {"default": 30 * DAY}, None, WEEK),
    (KEY_TYPE_ADMIN, {"default": DAY}, None, DAY),
    # A default larger than the maximum is lowered to it.
    (KEY_TYPE_NORMAL, {"max_ttl": 90 * DAY, "default": 365 * DAY}, None, 90 * DAY),
    (KEY_TYPE_MONITORING, {"max_monitor": DAY, "default": 30 * DAY}, None, DAY),
    # A default of never counts as larger than any finite maximum.
    (KEY_TYPE_NORMAL, {"max_ttl": 30 * DAY, "default": 0}, None, 30 * DAY),
    (KEY_TYPE_ADMIN, {"default": 0}, None, WEEK),
    (KEY_TYPE_NORMAL, {"default": 0}, None, 0),
    (KEY_TYPE_ADMIN, {"max_admin": 0, "default": 0}, None, 0),
])
def test_resolve_key_ttl_per_type(app, restore_config, key_type, limits, requested, expected):
    _limits(app, **limits)
    with app.app_context():
        assert resolve_key_ttl(requested, key_type) == expected


@pytest.mark.parametrize("key_type, limits, requested, message", [
    (KEY_TYPE_ADMIN, {}, 0, "admin keys must expire"),
    (KEY_TYPE_ADMIN, {}, WEEK + 1, "cannot exceed 604800"),
    (KEY_TYPE_ADMIN, {"max_ttl": 30 * DAY}, 30 * DAY, "cannot exceed 604800"),
    (KEY_TYPE_ADMIN, {"max_admin": DAY}, DAY + 1, "cannot exceed 86400"),
    (KEY_TYPE_ADMIN, {"default": 30 * DAY}, 30 * DAY, "cannot exceed 604800"),
    (KEY_TYPE_MONITORING, {}, MAX_KEY_TTL + 1, "cannot exceed"),
    (KEY_TYPE_MONITORING, {}, -1, "whole number"),
    (KEY_TYPE_MONITORING, {"max_monitor": 30 * DAY}, 0, "keys must expire"),
    (KEY_TYPE_MONITORING, {"max_monitor": 30 * DAY}, 30 * DAY + 1, "cannot exceed 2592000"),
    (KEY_TYPE_NORMAL, {"max_ttl": DAY}, 0, "keys must expire"),
    (KEY_TYPE_NORMAL, {"max_ttl": 90 * DAY, "default": 365 * DAY}, 365 * DAY, "cannot exceed 7776000"),
])
def test_resolve_key_ttl_per_type_rejected(app, restore_config, key_type, limits, requested, message):
    _limits(app, **limits)
    with app.app_context():
        with pytest.raises(ValueError, match=message):
            resolve_key_ttl(requested, key_type)


@pytest.mark.parametrize("limits, expected", [
    ({}, [(DAY, "1 day", False), (WEEK, "7 days", True)]),
    ({"max_ttl": 30 * DAY}, [(DAY, "1 day", False), (WEEK, "7 days", True)]),
    ({"max_ttl": DAY}, [(DAY, "1 day", False), (WEEK, "7 days", True)]),
    ({"max_admin": DAY}, [(DAY, "1 day", True)]),
    ({"max_admin": 2 * DAY}, [(DAY, "1 day", False), (2 * DAY, "2 days", True)]),
    ({"default": DAY}, [(DAY, "1 day", True), (WEEK, "7 days", False)]),
    ({"max_admin": 0}, [(s, label, False) for s, label in KEY_TTL_PRESETS] + [(0, "Never", True)]),
    ({"max_admin": 0, "default": 30 * DAY},
     [(s, label, s == 30 * DAY) for s, label in KEY_TTL_PRESETS] + [(0, "Never", False)]),
])
def test_key_ttl_choices_admin(app, restore_config, limits, expected):
    _limits(app, **limits)
    with app.app_context():
        assert key_ttl_choices(key_type=KEY_TYPE_ADMIN) == expected


@pytest.mark.parametrize("cap", [0, DAY])
def test_key_ttl_choices_monitoring_ignores_the_normal_cap(app, restore_config, cap):
    _limits(app, max_ttl=cap)
    with app.app_context():
        assert key_ttl_choices(key_type=KEY_TYPE_MONITORING) == (
            [(s, label, False) for s, label in KEY_TTL_PRESETS] + [(0, "Never", True)])


def test_key_ttl_choices_monitoring_with_a_max_offers_no_never(app, restore_config):
    _limits(app, max_monitor=30 * DAY)
    with app.app_context():
        assert key_ttl_choices(key_type=KEY_TYPE_MONITORING) == [
            (DAY, "1 day", False), (WEEK, "7 days", False), (30 * DAY, "30 days", True)]


@pytest.mark.parametrize("key_type", [KEY_TYPE_NORMAL, KEY_TYPE_MONITORING])
def test_key_ttl_choices_default_ttl_preselected_and_never_offered(app, restore_config, key_type):
    _limits(app, default=30 * DAY)
    with app.app_context():
        assert key_ttl_choices(key_type=key_type) == (
            [(s, label, s == 30 * DAY) for s, label in KEY_TTL_PRESETS] + [(0, "Never", False)])


def test_key_ttl_choices_default_lowered_to_max(app, restore_config):
    _limits(app, max_ttl=90 * DAY, default=365 * DAY)
    with app.app_context():
        assert key_ttl_choices()[-1] == (90 * DAY, "90 days", True)


def test_key_ttl_choices_admin_selected(app, restore_config):
    _limits(app)
    with app.app_context():
        assert key_ttl_choices(DAY, KEY_TYPE_ADMIN) == [(DAY, "1 day", True), (WEEK, "7 days", False)]
        # Not allowed for an admin key: falls back to the default.
        assert key_ttl_choices(0, KEY_TYPE_ADMIN)[-1] == (WEEK, "7 days", True)


@pytest.mark.parametrize("cap", [0, 30 * DAY])
def test_key_ttl_choices_normal_unchanged(app, restore_config, cap):
    app.config["KEY_TTL"] = cap
    with app.app_context():
        assert key_ttl_choices(key_type=KEY_TYPE_NORMAL) == key_ttl_choices()
