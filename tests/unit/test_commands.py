"""Tests for YAML sync functions in lumen/commands.py."""
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg.errors
import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from lumen import commands
from lumen.commands import __file__ as commands_file
from lumen.commands import backfill_aggregate_cmd, check_aggregate_cmd, reassign_model_cmd, sync_models_from_yaml


def test_sync_models_creates_model_config(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        yaml_data = {
            "models": [
                {
                    "name": "synced-model",
                    "active": True,
                    "input_cost_per_million": 1.0,
                    "output_cost_per_million": 2.0,
                }
            ]
        }
        sync_models_from_yaml(yaml_data)
        mc = db.session.execute(select(ModelConfig).filter_by(model_name="synced-model")).scalar_one_or_none()
        assert mc is not None
        assert mc.active is True
        assert float(mc.input_cost_per_million) == 1.0


def test_sync_models_normalizes_bare_hf_repo_id(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        yaml_data = {
            "models": [
                {"name": "bare-id", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0,
                 "url": "meta-models/Muse-Glimmer-30B"},
                {"name": "full-url", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0,
                 "url": "https://example.com/docs/model"},
                {"name": "hf-com", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0,
                 "url": "https://huggingface.com/meta-models/Muse-Glimmer-30B"},
                {"name": "hf-www", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0,
                 "url": "https://www.huggingface.co/meta-models/Muse-Glimmer-30B"},
                {"name": "no-url", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0},
            ]
        }
        sync_models_from_yaml(yaml_data)
        by_name = {m.model_name: m for m in db.session.execute(select(ModelConfig)).scalars().all()}
        assert by_name["bare-id"].url == "https://huggingface.co/meta-models/Muse-Glimmer-30B"
        assert by_name["full-url"].url == "https://example.com/docs/model"
        assert by_name["hf-com"].url == "https://huggingface.co/meta-models/Muse-Glimmer-30B"
        assert by_name["hf-www"].url == "https://huggingface.co/meta-models/Muse-Glimmer-30B"
        assert by_name["no-url"].url is None


def test_sync_models_updates_existing(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        yaml_data = {
            "models": [
                {"name": "update-model", "active": True, "input_cost_per_million": 1.0, "output_cost_per_million": 1.0}
            ]
        }
        sync_models_from_yaml(yaml_data)
        yaml_data["models"][0]["input_cost_per_million"] = 5.0
        sync_models_from_yaml(yaml_data)
        mc = db.session.execute(select(ModelConfig).filter_by(model_name="update-model")).scalar_one_or_none()
        assert float(mc.input_cost_per_million) == 5.0


def test_sync_models_deactivates_removed(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        yaml1 = {"models": [{"name": "will-be-removed", "active": True, "input_cost_per_million": 1.0, "output_cost_per_million": 1.0}]}
        sync_models_from_yaml(yaml1)
        # Sync with empty models list — should deactivate
        sync_models_from_yaml({"models": []})
        mc = db.session.execute(select(ModelConfig).filter_by(model_name="will-be-removed")).scalar_one_or_none()
        assert mc is not None
        assert mc.active is False


def test_sync_models_with_endpoints(app):
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_config import ModelConfig
        yaml_data = {
            "models": [
                {
                    "name": "ep-model",
                    "active": True,
                    "input_cost_per_million": 1.0,
                    "output_cost_per_million": 1.0,
                    "endpoints": [{"url": "http://ep1/v1", "api_key": "key1"}],
                }
            ]
        }
        sync_models_from_yaml(yaml_data)
        mc = db.session.execute(select(ModelConfig).filter_by(model_name="ep-model")).scalar_one_or_none()
        assert mc is not None
        eps = list(mc.endpoints)
        assert len(eps) == 1
        assert eps[0].url == "http://ep1/v1"


def test_no_runtime_group_rules_import():
    """Rules an admin deletes must stay deleted: config.yaml group_rules is not
    read at all (Lumen 2.0 removed the section — rules are database rows managed
    on each group's Rules tab). If this fails, someone reintroduced a
    startup/reload import — which resurrected deleted rules and re-enabled
    auto_join every restart."""
    assert not hasattr(sys.modules["lumen.commands"], "import_group_rules_from_yaml")
    root = Path(commands_file).resolve().parent
    for rel in ("__init__.py", "services/config_watcher.py", "commands.py"):
        source = (root / rel).read_text()
        assert "import_group_rules_from_yaml" not in source, rel


# ---------------------------------------------------------------------------
# _apply_model_fields / _apply_model_access — ack/early-access/disabled flags
# ---------------------------------------------------------------------------

def test_apply_model_fields_sets_ack_flags(app):
    from lumen.commands import _apply_model_fields
    from lumen.models.model_config import ModelConfig
    with app.app_context():
        mc = ModelConfig(model_name="m")
        _apply_model_fields(mc, {
            "name": "m",
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 2.0,
            "needs_ack": True,
            "ack_message": "ack me",
        })
        assert mc.needs_ack is True
        assert mc.disabled is False
        assert mc.ack_message == "ack me"


def test_apply_model_access_defaults(app):
    """With no flags in the definition, needs_ack and disabled default to False."""
    from lumen.commands import _apply_model_fields
    from lumen.models.model_config import ModelConfig
    with app.app_context():
        mc = ModelConfig(model_name="m2")
        _apply_model_fields(mc, {"name": "m2", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0})
        assert mc.needs_ack is False
        assert mc.disabled is False


def test_apply_model_access_legacy_active_false_maps_to_disabled(app, caplog):
    import logging

    from lumen.commands import _apply_model_fields, _warned
    from lumen.models.model_config import ModelConfig
    with app.app_context():
        _warned.clear()
        mc = ModelConfig(model_name="m3")
        with caplog.at_level(logging.WARNING):
            _apply_model_fields(mc, {
                "name": "m3", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0,
                "active": False,
            })
        assert mc.disabled is True
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "active: false" in msgs


def test_apply_model_access_removed_access_key_is_ignored(app):
    """The removed per-model 'access:' key is ignored; ownership is never touched by config sync."""
    from lumen.commands import _apply_model_fields
    from lumen.models.model_config import ModelConfig
    with app.app_context():
        mc = ModelConfig(model_name="m4")
        _apply_model_fields(mc, {
            "name": "m4", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0,
            "access": "allowed",
        })
        assert mc.owner_entity_id is None
        assert mc.disabled is False


def test_apply_model_access_explicit_disabled(app):
    from lumen.commands import _apply_model_fields
    from lumen.models.model_config import ModelConfig
    with app.app_context():
        mc = ModelConfig(model_name="m5")
        _apply_model_fields(mc, {
            "name": "m5", "input_cost_per_million": 1.0, "output_cost_per_million": 1.0,
            "disabled": True,
        })
        assert mc.disabled is True


# ---------------------------------------------------------------------------
# _normalize_end_date / _normalize_knowledge_cutoff
# ---------------------------------------------------------------------------

def test_normalize_end_date_from_date(app):
    from datetime import date

    from lumen.commands import _normalize_end_date
    with app.app_context():
        assert _normalize_end_date(date(2026, 9, 1)) == datetime(2026, 9, 2)


def test_normalize_end_date_naive_datetime_passthrough(app):
    from lumen.commands import _normalize_end_date
    with app.app_context():
        dt = datetime(2026, 9, 1, 12, 30)
        assert _normalize_end_date(dt) == dt


def test_normalize_end_date_aware_datetime_to_naive_utc(app):
    from datetime import timedelta, timezone

    from lumen.commands import _normalize_end_date
    with app.app_context():
        aware = datetime(2026, 9, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
        assert _normalize_end_date(aware) == datetime(2026, 9, 1, 10, 0)


def test_normalize_end_date_from_iso_string(app):
    from lumen.commands import _normalize_end_date
    with app.app_context():
        assert _normalize_end_date("2026-09-01") == datetime(2026, 9, 2)
        assert _normalize_end_date("2026-09-01T08:15:00") == datetime(2026, 9, 1, 8, 15)


def test_normalize_end_date_garbage_is_none(app):
    from lumen.commands import _normalize_end_date
    with app.app_context():
        assert _normalize_end_date("not-a-date", "m") is None
    assert _normalize_end_date(None) is None
    assert _normalize_end_date("") is None


def test_normalize_knowledge_cutoff_truncates(app):
    from lumen.commands import _normalize_knowledge_cutoff
    with app.app_context():
        assert _normalize_knowledge_cutoff("2024-06-15") == "2024-06"
        assert _normalize_knowledge_cutoff("2024-06") == "2024-06"
        assert _normalize_knowledge_cutoff(None) is None
        assert _normalize_knowledge_cutoff("not-a-real-date", "m") is None
        assert _normalize_knowledge_cutoff("2024", "m") is None
        assert _normalize_knowledge_cutoff("2024-13", "m") is None


def test_apply_model_fields_sets_early_access_and_end_date(app):
    from datetime import date

    from lumen.commands import _apply_model_fields
    from lumen.models.model_config import ModelConfig
    with app.app_context():
        mc = ModelConfig(model_name="ea-model")
        _apply_model_fields(mc, {
            "name": "ea-model",
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 2.0,
            "early_access": True,
            "end_date": date(2026, 12, 31),
            "knowledge_cutoff": "2024-06-15",
        })
        assert mc.early_access is True
        assert mc.end_date == datetime(2027, 1, 1)
        assert mc.knowledge_cutoff == "2024-06"


def test_normalize_end_date_rfc822_from_config_editor(app):
    """The admin config editor round-trips YAML dates through JSON as RFC 822."""
    from lumen.commands import _normalize_end_date
    with app.app_context():
        assert _normalize_end_date("Thu, 31 Dec 2026 00:00:00 GMT") == datetime(2027, 1, 1)


def test_backfill_aggregate_is_a_clean_noop_on_sqlite(app):
    """SQLite has no continuous aggregates. Say so and exit 0 — this is not an error."""
    result = app.test_cli_runner().invoke(backfill_aggregate_cmd, [])
    assert result.exit_code == 0, result.output
    assert "requires PostgreSQL/TimescaleDB" in result.output
    assert "sqlite" in result.output


def test_check_aggregate_is_a_clean_noop_on_sqlite(app):
    result = app.test_cli_runner().invoke(check_aggregate_cmd, [])
    assert result.exit_code == 0, result.output
    assert "requires PostgreSQL/TimescaleDB" in result.output
    assert "sqlite" in result.output


class _FailingConn:
    """Raises ``errors`` from ``execute`` in turn, then succeeds."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.calls = 0

    def execute(self, *args, **kwargs):
        self.calls += 1
        if self.errors:
            orig = self.errors.pop(0)
            raise DBAPIError.instance("CALL refresh_continuous_aggregate", {}, orig, psycopg.Error)


def test_refresh_window_retries_lock_not_available(monkeypatch):
    monkeypatch.setattr(commands.time, "sleep", lambda _s: None)
    conn = _FailingConn(psycopg.errors.LockNotAvailable("refresh in progress"),
                        psycopg.errors.LockNotAvailable("refresh in progress"))

    retries = commands._refresh_window(conn, "agg", datetime(2026, 1, 1), datetime(2026, 2, 1))

    assert retries == 2
    assert conn.calls == 3


def test_refresh_window_raises_other_errors_immediately(monkeypatch):
    monkeypatch.setattr(commands.time, "sleep", lambda _s: pytest.fail("must not retry"))
    conn = _FailingConn(psycopg.errors.UndefinedTable("no such aggregate"))

    with pytest.raises(DBAPIError) as excinfo:
        commands._refresh_window(conn, "agg", datetime(2026, 1, 1), datetime(2026, 2, 1))

    assert isinstance(excinfo.value.orig, psycopg.errors.UndefinedTable)
    assert conn.calls == 1


@pytest.fixture
def reassign_data(app):
    """Two models, one user, and history under the first: a conversation, a stat that
    collides with one TO already has, a stat that does not, and two request_logs rows."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.conversation import Conversation
        from lumen.models.entity import Entity
        from lumen.models.model_config import ModelConfig
        from lumen.models.model_stat import ModelStat
        from lumen.models.request_log import RequestLog

        user = Entity(entity_type="user", email="reassign@example.com", name="Reassign", active=True)
        src = ModelConfig(model_name="old-model", input_cost_per_million=1, output_cost_per_million=1)
        dst = ModelConfig(model_name="new-model", input_cost_per_million=1, output_cost_per_million=1)
        db.session.add_all([user, src, dst])
        db.session.flush()
        db.session.add_all([
            Conversation(entity_id=user.id, model="old-model"),
            ModelStat(entity_id=user.id, model_config_id=src.id, source="chat",
                      requests=3, input_tokens=30, output_tokens=60, cost=Decimal("0.3")),
            ModelStat(entity_id=user.id, model_config_id=src.id, source="api",
                      requests=2, input_tokens=20, output_tokens=40, cost=Decimal("0.2")),
            ModelStat(entity_id=user.id, model_config_id=dst.id, source="chat",
                      requests=1, input_tokens=10, output_tokens=20, cost=Decimal("0.1")),
        ])
        for _ in range(2):
            db.session.add(RequestLog(time=datetime.now(timezone.utc), model_config_id=src.id, source="chat"))
        db.session.commit()
        return {"src": src.id, "dst": dst.id, "user": user.id}


def _stats(db, model_config_id):
    from lumen.models.model_stat import ModelStat
    return {
        s.source: (s.requests, s.input_tokens, s.output_tokens, s.cost)
        for s in db.session.execute(select(ModelStat).filter_by(model_config_id=model_config_id)).scalars()
    }


def test_reassign_model_moves_everything_on_sqlite(app, reassign_data):
    src, dst = reassign_data["src"], reassign_data["dst"]
    result = app.test_cli_runner().invoke(reassign_model_cmd, [str(src), str(dst)])
    assert result.exit_code == 0, result.output
    assert "conversations updated: 1" in result.output
    assert "model_stats moved: 1, merged: 1" in result.output
    assert "request_logs updated: 2" in result.output

    with app.app_context():
        from lumen.extensions import db
        from lumen.models.conversation import Conversation
        from lumen.models.model_config import ModelConfig
        from lumen.models.request_log import RequestLog

        models = db.session.execute(select(Conversation.model)).scalars().all()
        assert models == ["new-model"]
        assert _stats(db, src) == {}
        assert _stats(db, dst) == {
            "chat": (4, 40, 80, Decimal("0.4")),
            "api": (2, 20, 40, Decimal("0.2")),
        }
        owners = db.session.execute(select(RequestLog.model_config_id)).scalars().all()
        assert owners == [dst, dst]
        # The FROM row is left alone: still there, unchanged.
        old = db.session.get(ModelConfig, src)
        assert old is not None and old.model_name == "old-model"


def test_reassign_model_second_run_is_a_noop(app, reassign_data):
    src, dst = reassign_data["src"], reassign_data["dst"]
    runner = app.test_cli_runner()
    assert runner.invoke(reassign_model_cmd, [str(src), str(dst)]).exit_code == 0
    with app.app_context():
        from lumen.extensions import db
        after_first = _stats(db, dst)

    result = runner.invoke(reassign_model_cmd, [str(src), str(dst)])
    assert result.exit_code == 0, result.output
    assert "conversations updated: 0" in result.output
    assert "model_stats moved: 0, merged: 0" in result.output
    assert "request_logs updated: 0" in result.output
    with app.app_context():
        from lumen.extensions import db
        assert _stats(db, dst) == after_first


def test_reassign_model_unknown_id_exits_1(app, reassign_data):
    runner = app.test_cli_runner()
    result = runner.invoke(reassign_model_cmd, ["999999", str(reassign_data["dst"])])
    assert result.exit_code == 1
    assert "model_config id 999999 not found" in result.output
    result = runner.invoke(reassign_model_cmd, [str(reassign_data["src"]), "999999"])
    assert result.exit_code == 1
    assert "model_config id 999999 not found" in result.output


def test_reassign_model_to_itself_exits_1_and_keeps_stats(app, reassign_data):
    """The old merge loop matched every stat against itself, doubled it, then deleted it."""
    src = reassign_data["src"]
    with app.app_context():
        from lumen.extensions import db
        before = _stats(db, src)

    result = app.test_cli_runner().invoke(reassign_model_cmd, [str(src), str(src)])

    assert result.exit_code == 1
    assert "same model_config id" in result.output
    with app.app_context():
        from lumen.extensions import db
        assert _stats(db, src) == before
