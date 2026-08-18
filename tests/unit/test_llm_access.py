import pytest

from lumen.services.llm import (
    bulk_model_access_info,
    get_model_access,
    get_model_access_status,
    get_pool_limit,
)
from tests.conftest import grant_model_to_group, make_group_with_member, set_model_owner


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
        project_id = _make_entity(entity_type="project", email="proj@example.com", name="Proj")
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


def test_group_grant_allows_user_member(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        owner_id = _make_entity()
        set_model_owner(model_id, owner_id)
        group_id = make_group_with_member(entity_id)
        grant_model_to_group(model_id, group_id)
        assert get_model_access_status(entity_id, model_id) == "allowed"
        assert get_model_access(entity_id, model_id) is True


def test_group_grant_allows_project_member(app, ids):
    _, model_id = ids
    with app.app_context():
        owner_id = _make_entity()
        project_id = _make_entity(entity_type="project", email="proj@example.com", name="Proj")
        set_model_owner(model_id, owner_id)
        group_id = make_group_with_member(project_id)
        grant_model_to_group(model_id, group_id)
        assert get_model_access_status(project_id, model_id) == "allowed"


def test_inactive_granted_group_blocked(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        owner_id = _make_entity()
        set_model_owner(model_id, owner_id)
        group_id = make_group_with_member(entity_id, active=False)
        grant_model_to_group(model_id, group_id)
        assert get_model_access_status(entity_id, model_id) == "blocked"


def test_non_member_of_granted_group_blocked(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        owner_id = _make_entity()
        member_id = _make_entity(email="member@example.com", name="Member")
        set_model_owner(model_id, owner_id)
        group_id = make_group_with_member(member_id)
        grant_model_to_group(model_id, group_id)
        assert get_model_access_status(entity_id, model_id) == "blocked"
        assert get_model_access(entity_id, model_id) is False


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


def test_needs_ack_for_granted_member(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        owner_id = _make_entity()
        set_model_owner(model_id, owner_id)
        group_id = make_group_with_member(entity_id)
        grant_model_to_group(model_id, group_id)
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


def test_early_access_for_granted_member(app, ids):
    entity_id, model_id = ids
    with app.app_context():
        owner_id = _make_entity()
        set_model_owner(model_id, owner_id)
        group_id = make_group_with_member(entity_id)
        grant_model_to_group(model_id, group_id)
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


# ---------------------------------------------------------------------------
# require_consent=False bypasses the consent gate for needs_ack models
# ---------------------------------------------------------------------------

def test_needs_ack_allows_access_when_require_consent_false(app, ids):
    """require_consent=False skips the consent DB check — needs_ack treated as allowed."""
    entity_id, model_id = ids
    with app.app_context():
        _set_needs_ack(app, model_id)
        assert get_model_access(entity_id, model_id, require_consent=False) is True


def test_needs_ack_still_blocked_when_require_consent_true(app, ids):
    """require_consent=True (default) still gates on consent for needs_ack models."""
    entity_id, model_id = ids
    with app.app_context():
        _set_needs_ack(app, model_id)
        assert get_model_access(entity_id, model_id, require_consent=True) is False


def test_blocked_model_still_blocked_when_require_consent_false(app, ids):
    """require_consent=False never overrides a hard block (ownership)."""
    entity_id, model_id = ids
    with app.app_context():
        owner_id = _make_entity()
        set_model_owner(model_id, owner_id)
        assert get_model_access(entity_id, model_id, require_consent=False) is False


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
        from lumen.services.llm import has_model_consent
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
        from lumen.services.llm import has_model_consent
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


# ---------------------------------------------------------------------------
# end_date — model hidden and rejected after the date (exclusive)
# ---------------------------------------------------------------------------

def _set_end_date(app, model_id, end_date):
    from lumen.extensions import db
    from lumen.models.model_config import ModelConfig
    mc = db.session.get(ModelConfig, model_id)
    mc.end_date = end_date
    db.session.commit()


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
