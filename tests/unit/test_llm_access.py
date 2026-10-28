from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from lumen.extensions import db
from lumen.models.model_config import ModelConfig
from lumen.services.llm import (
    _consent_satisfied,
    _resolve_single_access,
    ack_notice_text,
    bulk_model_access_info,
    consent_state,
    entity_has_unlimited_pool,
    get_model_access,
    get_model_access_status,
    get_pool_limit,
    has_model_consent,
)
from tests.conftest import make_group_with_member, make_project, set_model_owner


@pytest.fixture
def ids(app, test_user, test_model):
    return test_user["id"], test_model["id"]


def _make_entity(entity_type="user", email="other@example.com", name="Other"):
    from lumen.extensions import db
    from lumen.models.entity import Entity
    entity = Entity(entity_type=entity_type, email=email, name=name, active=True)
    db.session.add(entity)
    db.session.commit()
    return entity.id


def _set_needs_ack(app, model_id):
    from lumen.extensions import db
    from lumen.models.model_config import ModelConfig
    mc = db.session.get(ModelConfig, model_id)
    mc.needs_ack = True
    db.session.commit()


def _grant_pool(app, entity_id):
    """Give the entity a usable pool: a needs_consent denial presupposes one."""
    from lumen.extensions import db
    from lumen.models.entity_limit import EntityLimit
    db.session.add(EntityLimit(entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0))
    db.session.commit()


def _set_early_access(app, model_id):
    from lumen.extensions import db
    from lumen.models.model_config import ModelConfig
    mc = db.session.get(ModelConfig, model_id)
    mc.early_access = True
    db.session.commit()


# ---------------------------------------------------------------------------
# Ownerless (public) models are visible to everyone
# ---------------------------------------------------------------------------

def test_public_model_allowed_for_user(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        assert get_model_access_status(entity_id, model_id) == "allowed"


def test_public_model_allowed_for_project(app, ids):
    _, model_id = ids
    with app.app_context():
        project_id = make_project("Proj", email="proj@example.com").id
        assert get_model_access_status(project_id, model_id) == "allowed"


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------

def test_owner_sees_own_model(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        set_model_owner(model_id, entity_id)
        assert get_model_access_status(entity_id, model_id) == "allowed"
        assert get_model_access(entity_id, model_id) is True


def test_owned_model_blocked_for_non_owner(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        owner_id = _make_entity()
        set_model_owner(model_id, owner_id)
        assert get_model_access_status(entity_id, model_id) == "blocked"
        assert get_model_access(entity_id, model_id) is False


def test_group_member_blocked_from_owned_model(app, ids):
    """Group membership never grants an owned model."""
    entity_id, model_id = ids
    with app.app_context():
        owner_id = _make_entity()
        set_model_owner(model_id, owner_id)
        make_group_with_member(entity_id)
        assert get_model_access_status(entity_id, model_id) == "blocked"
        assert get_model_access(entity_id, model_id) is False


def test_project_group_member_blocked_from_owned_model(app, ids):
    _, model_id = ids
    with app.app_context():
        owner_id = _make_entity()
        project_id = make_project("Proj", email="proj@example.com").id
        set_model_owner(model_id, owner_id)
        make_group_with_member(project_id)
        assert get_model_access_status(project_id, model_id) == "blocked"


# ---------------------------------------------------------------------------
# disabled / end_date always block, even for the owner
# ---------------------------------------------------------------------------

def test_disabled_model_blocked_even_for_owner(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        set_model_owner(model_id, entity_id)
        db.session.get(ModelConfig, model_id).disabled = True
        db.session.commit()
        assert get_model_access_status(entity_id, model_id) == "blocked"
        assert get_model_access(entity_id, model_id) is False


def test_expired_model_blocked_even_for_owner(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from datetime import timedelta

        from lumen.timeutils import utcnow
        set_model_owner(model_id, entity_id)
        _set_end_date(app, model_id, utcnow() - timedelta(days=1))
        assert get_model_access_status(entity_id, model_id) == "blocked"
        assert get_model_access(entity_id, model_id) is False


# ---------------------------------------------------------------------------
# needs_ack / early_access resolve to needs_ack for every visible entity
# ---------------------------------------------------------------------------

def test_needs_ack_on_public_model(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        _set_needs_ack(app, model_id)
        assert get_model_access_status(entity_id, model_id) == "needs_ack"


def test_needs_ack_for_owner(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        set_model_owner(model_id, entity_id)
        _set_needs_ack(app, model_id)
        assert get_model_access_status(entity_id, model_id) == "needs_ack"


def test_early_access_resolves_to_needs_ack(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        _set_early_access(app, model_id)
        assert get_model_access_status(entity_id, model_id) == "needs_ack"


def test_early_access_for_owner(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        set_model_owner(model_id, entity_id)
        _set_early_access(app, model_id)
        assert get_model_access_status(entity_id, model_id) == "needs_ack"


# ---------------------------------------------------------------------------
# get_model_access (chat gate) and consent
# ---------------------------------------------------------------------------

def test_public_model_allows_chat(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        assert get_model_access(entity_id, model_id) is True


def test_needs_ack_blocks_chat_without_consent(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        _set_needs_ack(app, model_id)
        assert get_model_access(entity_id, model_id) is False


def test_needs_ack_allows_chat_with_consent(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        from lumen.timeutils import utcnow
        _set_needs_ack(app, model_id)
        db.session.add(EntityModelConsent(
            entity_id=entity_id,
            model_config_id=model_id,
            consented_at=utcnow(),
        ))
        db.session.commit()
        assert get_model_access(entity_id, model_id) is True


def test_has_model_consent_unknown_model_fails_closed(app, ids):
    entity_id, _ = ids
    with app.app_context():
        assert has_model_consent(entity_id, 999999) is False


def test_get_model_access_reuses_bulk_consent_result(monkeypatch):
    entity_id, model_id = 1, 7
    monkeypatch.setattr(
        "lumen.services.llm.bulk_model_access_info",
        lambda *_args, **_kwargs: ({model_id: "needs_ack"}, {model_id: object()}),
    )

    def unexpected_second_consent_lookup(*_args, **_kwargs):
        raise AssertionError("get_model_access performed a second consent lookup")

    monkeypatch.setattr(
        "lumen.services.llm.has_model_consent",
        unexpected_second_consent_lookup,
    )
    assert get_model_access(entity_id, model_id) is True


# ---------------------------------------------------------------------------
# early_access consent — second acknowledgement requirement, per-requirement
# ---------------------------------------------------------------------------

def test_early_access_blocks_chat_without_consent(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        _set_early_access(app, model_id)
        assert get_model_access(entity_id, model_id) is False


def test_early_access_allows_chat_with_early_ack(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        from lumen.timeutils import utcnow
        _set_early_access(app, model_id)
        db.session.add(EntityModelConsent(entity_id=entity_id, model_config_id=model_id, early_access_at=utcnow()))
        db.session.commit()
        assert get_model_access(entity_id, model_id) is True


def test_needs_ack_consent_does_not_cover_later_early_access(app, ids):
    """A model that gains early_access after the user consented must re-prompt."""
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        from lumen.timeutils import utcnow
        _set_needs_ack(app, model_id)
        db.session.add(EntityModelConsent(entity_id=entity_id, model_config_id=model_id, consented_at=utcnow()))
        db.session.commit()
        assert has_model_consent(entity_id, model_id) is True
        _set_early_access(app, model_id)
        assert has_model_consent(entity_id, model_id) is False
        _, consent_map = bulk_model_access_info(entity_id, [model_id])
        assert model_id not in consent_map


def test_both_requirements_satisfied(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_model_consent import EntityModelConsent
        from lumen.timeutils import utcnow
        _set_needs_ack(app, model_id)
        _set_early_access(app, model_id)
        db.session.add(EntityModelConsent(entity_id=entity_id, model_config_id=model_id,
                                          consented_at=utcnow(), early_access_at=utcnow()))
        db.session.commit()
        assert has_model_consent(entity_id, model_id) is True
        _, consent_map = bulk_model_access_info(entity_id, [model_id])
        assert model_id in consent_map


# ---------------------------------------------------------------------------
# get_model_access_status parity with bulk_model_access_info
# ---------------------------------------------------------------------------

def test_single_status_matches_bulk(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        owner_id = _make_entity()
        owned = ModelConfig(model_name="owned-model", input_cost_per_million=1.0,
                            output_cost_per_million=2.0, owner_entity_id=owner_id)
        acked = ModelConfig(model_name="ack-model", input_cost_per_million=1.0,
                            output_cost_per_million=2.0, needs_ack=True)
        db.session.add_all([owned, acked])
        db.session.commit()
        all_ids = [model_id, owned.id, acked.id]
        statuses, _ = bulk_model_access_info(entity_id, all_ids)
        for mid in all_ids:
            assert get_model_access_status(entity_id, mid) == statuses[mid]
        assert statuses[model_id] == "allowed"
        assert statuses[owned.id] == "blocked"
        assert statuses[acked.id] == "needs_ack"


# ---------------------------------------------------------------------------
# Pool limits (unchanged by the ownership rework)
# ---------------------------------------------------------------------------

def test_get_pool_limit_no_limit(app, ids):
    entity_id, _ = ids
    with app.app_context():
        assert get_pool_limit(entity_id) is None


def test_get_pool_limit_with_limit(app, ids):
    entity_id, _ = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=100, refresh_coins=10, starting_coins=100))
        db.session.commit()
        result = get_pool_limit(entity_id)
        assert result is not None
        assert result[0] == 100.0


def test_get_pool_limit_unlimited(app, ids):
    entity_id, _ = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0))
        db.session.commit()
        assert get_pool_limit(entity_id) == (-2, 0, 0)


def test_get_pool_limit_blocked(app, ids):
    entity_id, _ = ids
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        db.session.add(EntityLimit(entity_id=entity_id, max_coins=0, refresh_coins=0, starting_coins=0))
        db.session.commit()
        assert get_pool_limit(entity_id) is None


def test_get_pool_limit_global_token_defaults_fallback(app, ids):
    """With no entity or group limit, get_pool_limit falls back to global TOKEN_DEFAULTS."""
    entity_id, _ = ids
    with app.app_context():
        old = app.config.get("TOKEN_DEFAULTS")
        app.config["TOKEN_DEFAULTS"] = {"max": 250, "refresh": 25, "starting": 250}
        try:
            assert get_pool_limit(entity_id) == (250.0, 25.0, 250.0)
        finally:
            app.config["TOKEN_DEFAULTS"] = old


def _sql_unlimited(entity_id):
    from sqlalchemy import select

    from lumen.extensions import db
    from lumen.models.entity import Entity
    return db.session.execute(
        select(entity_has_unlimited_pool(Entity.id)).where(Entity.id == entity_id)
    ).scalar()


@pytest.mark.parametrize(
    "own_max, group_max, group_active, expected",
    [
        # No own limit, no group: the unlimited default applies.
        (None, None, True, True),
        # A positive group limit overrides the unlimited default.
        (None, 100, True, False),
        # A zero group limit is not usable, so the default still applies.
        (None, 0, True, True),
        # An own unlimited limit wins over a positive group limit.
        (-2, 100, True, True),
        # An inactive group's positive limit is ignored, so the default still applies.
        (None, 100, False, True),
    ],
)
def test_entity_has_unlimited_pool_under_unlimited_default(app, ids, monkeypatch, own_max, group_max, group_active, expected):
    """SQL unlimited-pool probe matches get_pool_limit when TOKEN_DEFAULTS is unlimited."""
    entity_id, _ = ids
    monkeypatch.setitem(app.config, "TOKEN_DEFAULTS", {"max": -2, "refresh": 0, "starting": 0})
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.entity_limit import EntityLimit
        from lumen.models.group_limit import GroupLimit
        if own_max is not None:
            db.session.add(EntityLimit(entity_id=entity_id, max_coins=own_max, refresh_coins=0, starting_coins=0))
        if group_max is not None:
            group_id = make_group_with_member(entity_id, active=group_active)
            db.session.add(GroupLimit(group_id=group_id, max_coins=group_max, refresh_coins=0, starting_coins=0))
        db.session.commit()
        assert _sql_unlimited(entity_id) is expected
        assert ((get_pool_limit(entity_id) or (None,))[0] == -2) is expected


# ---------------------------------------------------------------------------
# end_date — model hidden and rejected after the date (exclusive)
# ---------------------------------------------------------------------------

def _set_end_date(app, model_id, end_date):
    from lumen.extensions import db
    from lumen.models.model_config import ModelConfig
    mc = db.session.get(ModelConfig, model_id)
    mc.end_date = end_date
    db.session.commit()


#: Fixed naive-UTC instant the end_date boundary tests freeze the clock at.
NOW = datetime(2030, 1, 1, 12, 0, 0)


@pytest.fixture
def frozen_llm_now(monkeypatch):
    monkeypatch.setattr("lumen.services.llm.utcnow", lambda: NOW)


@pytest.mark.parametrize("end_date, expected", [
    (NOW, "blocked"),
    (NOW + timedelta(microseconds=1), "allowed"),
])
def test_resolve_single_access_end_date_boundary(frozen_llm_now, end_date, expected):
    """The model is blocked from the exact end instant, not one tick later."""
    assert _resolve_single_access(False, None, model_end_date=end_date) == expected


def test_model_blocked_at_exact_end_date(app, ids, frozen_llm_now):
    entity_id, model_id = ids
    with app.app_context():
        _set_end_date(app, model_id, NOW)
        assert get_model_access_status(entity_id, model_id) == "blocked"
        assert get_model_access(entity_id, model_id) is False


def test_future_end_date_still_allowed(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        from datetime import timedelta

        from lumen.timeutils import utcnow
        _set_end_date(app, model_id, utcnow() + timedelta(days=1))
        assert get_model_access_status(entity_id, model_id) == "allowed"


def test_active_sql_filter_excludes_expired(app, ids):
    """ModelConfig.active hybrid excludes expired models in SQL and Python."""
    entity_id, model_id = ids
    with app.app_context():
        from datetime import timedelta

        from sqlalchemy import select

        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        from lumen.timeutils import utcnow
        _set_end_date(app, model_id, utcnow() - timedelta(minutes=1))
        active_ids = db.session.execute(select(ModelConfig.id).where(ModelConfig.active)).scalars().all()
        assert model_id not in active_ids
        assert db.session.get(ModelConfig, model_id).active is False
        _set_end_date(app, model_id, utcnow() + timedelta(minutes=5))
        active_ids = db.session.execute(select(ModelConfig.id).where(ModelConfig.active)).scalars().all()
        assert model_id in active_ids


def test_active_end_date_boundary(app, ids, monkeypatch):
    """ModelConfig.active is False from the exact end instant, in SQL and Python."""
    monkeypatch.setattr("lumen.models.model_config.utcnow", lambda: NOW)
    entity_id, model_id = ids
    with app.app_context():
        _set_end_date(app, model_id, NOW)
        active_ids = db.session.execute(select(ModelConfig.id).where(ModelConfig.active)).scalars().all()
        assert model_id not in active_ids
        assert db.session.get(ModelConfig, model_id).active is False
        # 1 s rather than 1 µs: not every database keeps sub-second precision.
        _set_end_date(app, model_id, NOW + timedelta(seconds=1))
        active_ids = db.session.execute(select(ModelConfig.id).where(ModelConfig.active)).scalars().all()
        assert model_id in active_ids
        assert db.session.get(ModelConfig, model_id).active is True


def test_bulk_unknown_model_id_raises(app, ids):
    """An id that resolves to no model is a caller bug and must not fail open."""
    import pytest

    from lumen.services.llm import bulk_model_access_info
    entity_id, model_id = ids
    with app.app_context():
        with pytest.raises(ValueError, match="unknown model_config_id"):
            bulk_model_access_info(entity_id, [model_id, 999999])


def test_bulk_empty_input_returns_dicts(app, ids):
    """Empty input returns ({}, {}) — consent_map is always a dict."""
    from lumen.services.llm import bulk_model_access_info
    entity_id, _ = ids
    with app.app_context():
        statuses, consent_map = bulk_model_access_info(entity_id, [])
        assert statuses == {} and consent_map == {}


# ---------------------------------------------------------------------------
# Rejection taxonomy: which 403 this is
# ---------------------------------------------------------------------------

def _recorder(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "lumen.blueprints.metrics.middleware.observe_rejection",
        lambda reason, source, model="": recorded.append((reason, source, model)),
    )
    return recorded


def test_blocked_model_is_counted_as_no_access(app, ids, test_model, monkeypatch):
    from http import HTTPStatus

    from lumen.services.llm import check_coin_budget
    entity_id, model_id = ids
    with app.app_context():
        owner_id = _make_entity()
        set_model_owner(model_id, owner_id)
        recorded = _recorder(monkeypatch)
        ok, code, _msg, _eff, reason, _retry_after = check_coin_budget(
            entity_id, model_id, source="api", model_name=test_model["model_name"],
        )
        assert ok is False
        assert code == HTTPStatus.FORBIDDEN
        assert reason == "no_access"
        assert recorded == [("no_access", "api", test_model["model_name"])]


def test_unacknowledged_model_is_counted_as_needs_consent(app, ids, test_model, monkeypatch):
    """Both refusals are one None from get_effective_limit, and they are not the same.

    "Acknowledge this model" is the user's to fix from the model detail page;
    "no access" is not. A taxonomy that merged them would hide a support queue.
    """
    from http import HTTPStatus

    from lumen.services.llm import check_coin_budget
    entity_id, model_id = ids
    with app.app_context():
        _grant_pool(app, entity_id)
        _set_needs_ack(app, model_id)
        recorded = _recorder(monkeypatch)
        ok, code, _msg, _eff, reason, _retry_after = check_coin_budget(
            entity_id, model_id, source="chat", model_name=test_model["model_name"],
        )
        assert ok is False
        assert code == HTTPStatus.FORBIDDEN
        assert reason == "needs_consent"
        assert recorded == [("needs_consent", "chat", test_model["model_name"])]


def test_zero_pool_unacknowledged_model_is_counted_as_no_access(app, ids, test_model, monkeypatch):
    """Pool eligibility outranks the acknowledgement gate in the taxonomy.

    An entity with a 0-coin pool is refused by the acknowledge endpoint too, so
    labelling its denial needs_consent would send it chasing a 404.
    """
    from http import HTTPStatus

    from lumen.extensions import db
    from lumen.models.entity_limit import EntityLimit
    from lumen.services.llm import check_coin_budget
    entity_id, model_id = ids
    with app.app_context():
        db.session.add(EntityLimit(
            entity_id=entity_id, max_coins=0, refresh_coins=0, starting_coins=0,
        ))
        _set_needs_ack(app, model_id)
        db.session.commit()
        recorded = _recorder(monkeypatch)
        ok, code, _msg, _eff, reason, _retry_after = check_coin_budget(
            entity_id, model_id, source="api", model_name=test_model["model_name"],
        )
        assert ok is False
        assert code == HTTPStatus.FORBIDDEN
        assert reason == "no_access"
        assert recorded == [("no_access", "api", test_model["model_name"])]


def test_acknowledged_blocked_model_is_counted_as_no_access(app, ids, test_model, monkeypatch):
    """Consent on a model the entity cannot see is still a no_access rejection."""
    from lumen.extensions import db
    from lumen.models.entity_model_consent import EntityModelConsent
    from lumen.services.llm import check_coin_budget
    from lumen.timeutils import utcnow
    entity_id, model_id = ids
    with app.app_context():
        _set_needs_ack(app, model_id)
        db.session.add(EntityModelConsent(entity_id=entity_id, model_config_id=model_id, consented_at=utcnow()))
        db.session.commit()
        owner_id = _make_entity()
        set_model_owner(model_id, owner_id)
        recorded = _recorder(monkeypatch)
        check_coin_budget(entity_id, model_id, source="api", model_name=test_model["model_name"])
        assert recorded == [("no_access", "api", test_model["model_name"])]


def test_access_counting_never_breaks_the_rejection(app, ids, test_model, monkeypatch):
    """A broken counter must not turn a clean 403 into a 500."""
    from http import HTTPStatus

    from lumen.services.llm import check_coin_budget
    entity_id, model_id = ids

    def boom(*a, **kw):
        raise RuntimeError("prometheus is unhappy")

    with app.app_context():
        _set_needs_ack(app, model_id)
        monkeypatch.setattr("lumen.blueprints.metrics.middleware.observe_rejection", boom)
        ok, code, msg, _eff, _reason, _retry_after = check_coin_budget(
            entity_id, model_id, source="chat", model_name=test_model["model_name"],
        )
        assert ok is False
        assert code == HTTPStatus.FORBIDDEN
        assert msg == "No access to this model"


# ---------------------------------------------------------------------------
# consent_state: the single "consent satisfied" rule
# ---------------------------------------------------------------------------

_T1 = datetime(2026, 1, 1, 12, 0)
_T2 = datetime(2026, 2, 1, 12, 0)


def _mc(needs_ack=False, early_access=False):
    return SimpleNamespace(needs_ack=needs_ack, early_access=early_access)


def _row(consented_at=None, early_access_at=None):
    return SimpleNamespace(consented_at=consented_at, early_access_at=early_access_at)


@pytest.mark.parametrize(
    "mc, row, expected",
    [
        # needs_ack only
        (_mc(needs_ack=True), None, (["needs_ack"], None)),
        (_mc(needs_ack=True), _row(), (["needs_ack"], None)),
        (_mc(needs_ack=True), _row(consented_at=_T1), ([], _T1)),
        # early_access only
        (_mc(early_access=True), None, (["early_access"], None)),
        (_mc(early_access=True), _row(), (["early_access"], None)),
        (_mc(early_access=True), _row(early_access_at=_T1), ([], _T1)),
        # both requirements: none, partial (either half), full
        (_mc(True, True), None, (["needs_ack", "early_access"], None)),
        (_mc(True, True), _row(), (["needs_ack", "early_access"], None)),
        (_mc(True, True), _row(consented_at=_T1), (["early_access"], None)),
        (_mc(True, True), _row(early_access_at=_T1), (["needs_ack"], None)),
        (_mc(True, True), _row(consented_at=_T1, early_access_at=_T2), ([], _T2)),
        (_mc(True, True), _row(consented_at=_T2, early_access_at=_T1), ([], _T2)),
        # no requirements
        (_mc(), None, ([], None)),
        (_mc(), _row(consented_at=_T1), ([], _T1)),
    ],
)
def test_consent_state(mc, row, expected):
    assert consent_state(mc, row) == expected


@pytest.mark.parametrize(
    "mc, row, expected",
    [
        (_mc(True, True), None, False),
        (_mc(True, True), _row(consented_at=_T1), False),
        (_mc(True, True), _row(consented_at=_T1, early_access_at=_T2), True),
        (_mc(needs_ack=True), _row(consented_at=_T1), True),
        # No row is never "consented", even for a model with no requirements.
        (_mc(), None, False),
    ],
)
def test_consent_satisfied_follows_consent_state(mc, row, expected):
    assert _consent_satisfied(row, mc) is expected


def test_ack_notice_text_joins_both_notices(app):
    mc = SimpleNamespace(needs_ack=True, early_access=True, ack_message="Be good.")
    with app.app_context():
        app.config["MODEL_DEFAULTS"] = {"early_access_message": "Early bird."}
        assert ack_notice_text(mc) == "Be good.\n\nEarly bird."
        assert ack_notice_text(SimpleNamespace(needs_ack=False, early_access=False, ack_message=None)) is None
