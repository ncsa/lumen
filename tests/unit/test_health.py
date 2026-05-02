"""Tests for check_all_endpoints — the per-tick logic of the health checker."""
import pytest
from unittest.mock import MagicMock, patch


def _add_endpoint(db, model_config_id, url, api_key, model_name=None, healthy=True):
    from lumen.models.model_endpoint import ModelEndpoint
    ep = ModelEndpoint(
        model_config_id=model_config_id,
        url=url,
        api_key=api_key,
        model_name=model_name,
        healthy=healthy,
    )
    db.session.add(ep)
    db.session.flush()
    return ep


def _make_openai_mock(model_ids):
    """Return a context-manager mock that lists the given model IDs."""
    model_obj = lambda mid: MagicMock(id=mid)
    client = MagicMock()
    client.models.list.return_value = MagicMock(data=[model_obj(m) for m in model_ids])
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=client)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_no_endpoints_returns_zero(app):
    with app.app_context():
        from lumen.services.health import check_all_endpoints
        assert check_all_endpoints() == 0


def test_endpoint_healthy_when_model_found(app, test_model, test_model_endpoint):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.services.health import check_all_endpoints

        ep = db.session.get(ModelEndpoint, test_model_endpoint["id"])
        ep.model_name = "dummy"
        ep.healthy = False
        db.session.commit()

        mock_cm = _make_openai_mock(["dummy", "other"])
        with patch("lumen.services.health.openai.OpenAI", return_value=mock_cm):
            assert check_all_endpoints() == 1

        db.session.refresh(ep)
        assert ep.healthy is True
        assert ep.last_checked_at is not None


def test_endpoint_unhealthy_when_model_missing(app, test_model, test_model_endpoint):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.services.health import check_all_endpoints

        ep = db.session.get(ModelEndpoint, test_model_endpoint["id"])
        ep.model_name = "expected-model"
        ep.healthy = True
        db.session.commit()

        mock_cm = _make_openai_mock(["some-other-model"])
        with patch("lumen.services.health.openai.OpenAI", return_value=mock_cm):
            check_all_endpoints()

        db.session.refresh(ep)
        assert ep.healthy is False


def test_endpoint_unhealthy_on_connection_error(app, test_model, test_model_endpoint):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.services.health import check_all_endpoints

        ep = db.session.get(ModelEndpoint, test_model_endpoint["id"])
        ep.healthy = True
        db.session.commit()

        failing_cm = MagicMock()
        failing_cm.__enter__ = MagicMock(side_effect=ConnectionError("refused"))
        failing_cm.__exit__ = MagicMock(return_value=False)

        with patch("lumen.services.health.openai.OpenAI", return_value=failing_cm):
            check_all_endpoints()

        db.session.refresh(ep)
        assert ep.healthy is False


def test_endpoint_uses_parent_model_name_when_no_override(app, test_model, test_model_endpoint):
    """When ep.model_name is None, the parent ModelConfig.model_name is used."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.services.health import check_all_endpoints

        ep = db.session.get(ModelEndpoint, test_model_endpoint["id"])
        ep.model_name = None  # fall back to parent model_name = "test-model"
        ep.healthy = False
        db.session.commit()

        mock_cm = _make_openai_mock(["test-model"])
        with patch("lumen.services.health.openai.OpenAI", return_value=mock_cm):
            check_all_endpoints()

        db.session.refresh(ep)
        assert ep.healthy is True


def test_last_checked_at_updated_on_failure(app, test_model, test_model_endpoint):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint
        from lumen.services.health import check_all_endpoints

        ep = db.session.get(ModelEndpoint, test_model_endpoint["id"])
        ep.last_checked_at = None
        db.session.commit()

        failing_cm = MagicMock()
        failing_cm.__enter__ = MagicMock(side_effect=RuntimeError("down"))
        failing_cm.__exit__ = MagicMock(return_value=False)

        with patch("lumen.services.health.openai.OpenAI", return_value=failing_cm):
            check_all_endpoints()

        db.session.refresh(ep)
        assert ep.last_checked_at is not None
