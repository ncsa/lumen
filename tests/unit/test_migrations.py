"""Sanity checks on the Alembic migration graph, and SQLite round trips of single revisions."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory


def _script_dir():
    cfg = Config("migrations/alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    return ScriptDirectory.from_config(cfg)


def test_single_migration_head():
    heads = _script_dir().get_heads()
    assert len(heads) == 1, f"Expected 1 migration head, got {len(heads)}: {heads}"


# ---------------------------------------------------------------------------
# e7f8a9b0c1d2: entity_managers.is_owner -> entities.owner_entity_id
#
# The full chain is PostgreSQL-only (migrations/env.py blocks SQLite), so the
# revision's upgrade()/downgrade() run directly against a SQLite database
# shaped like the previous head. FK behaviour is covered on PostgreSQL in
# tests/integration/test_project_owner_postgres.py.
# ---------------------------------------------------------------------------

_OWNER_REVISION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations" / "versions" / "e7f8a9b0c1d2_project_owner_pointer.py"
)


def _owner_revision():
    spec = importlib.util.spec_from_file_location("owner_pointer_revision", _OWNER_REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_pre_owner_pointer_db(path, owners):
    """entities/entity_managers as the previous head left them.

    ``owners`` maps project id -> owning user id (None = no owner row).
    Users 1-3 manage every project.
    """
    engine = sa.create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE entities (id INTEGER PRIMARY KEY, entity_type VARCHAR(8) NOT NULL)"
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE entity_managers (
                id INTEGER PRIMARY KEY,
                user_entity_id INTEGER NOT NULL,
                project_entity_id INTEGER NOT NULL,
                is_owner BOOLEAN DEFAULT false NOT NULL,
                CONSTRAINT entity_managers_user_entity_id_project_entity_id_key
                    UNIQUE (user_entity_id, project_entity_id)
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_entity_managers_owner ON entity_managers"
            " (project_entity_id) WHERE is_owner"
        )
        for uid in (1, 2, 3):
            conn.exec_driver_sql(f"INSERT INTO entities VALUES ({uid}, 'user')")
        for pid, owner in owners.items():
            conn.exec_driver_sql(f"INSERT INTO entities VALUES ({pid}, 'project')")
            for uid in (1, 2, 3):
                conn.exec_driver_sql(
                    "INSERT INTO entity_managers (user_entity_id, project_entity_id, is_owner)"
                    f" VALUES ({uid}, {pid}, {1 if uid == owner else 0})"
                )
    return engine


def _run_revision(engine, fn):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with ctx.begin_transaction():
            with Operations.context(ctx):
                fn()
        conn.commit()


def test_owner_pointer_upgrade_backfills_and_drops_is_owner(tmp_path):
    engine = _make_pre_owner_pointer_db(tmp_path / "owner.db", {10: 2, 11: 3})
    _run_revision(engine, _owner_revision().upgrade)

    insp = sa.inspect(engine)
    assert "is_owner" not in {c["name"] for c in insp.get_columns("entity_managers")}
    assert "uq_entity_managers_owner" not in {i["name"] for i in insp.get_indexes("entity_managers")}
    assert "ck_entities_project_owner" in {c["name"] for c in insp.get_check_constraints("entities")}
    fk = next(fk for fk in insp.get_foreign_keys("entities") if fk["name"] == "fk_entities_owner_membership")
    assert fk["referred_table"] == "entity_managers"
    assert fk["constrained_columns"] == ["owner_entity_id", "id"]
    assert fk["referred_columns"] == ["user_entity_id", "project_entity_id"]

    with engine.connect() as conn:
        rows = dict(conn.execute(sa.text("SELECT id, owner_entity_id FROM entities")).all())
        assert rows == {1: None, 2: None, 3: None, 10: 2, 11: 3}
        # The CHECK now rejects a project with no owner.
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text("INSERT INTO entities (id, entity_type) VALUES (12, 'project')"))
    engine.dispose()


def test_owner_pointer_upgrade_aborts_listing_ownerless_projects(tmp_path):
    engine = _make_pre_owner_pointer_db(tmp_path / "owner.db", {10: 2, 11: None, 12: None})
    with pytest.raises(RuntimeError, match=r"no owner: 11, 12\."):
        _run_revision(engine, _owner_revision().upgrade)

    # Nothing changed: is_owner is still there and no pointer column was added.
    insp = sa.inspect(engine)
    assert "is_owner" in {c["name"] for c in insp.get_columns("entity_managers")}
    assert "owner_entity_id" not in {c["name"] for c in insp.get_columns("entities")}
    engine.dispose()


def test_owner_pointer_downgrade_restores_is_owner(tmp_path):
    engine = _make_pre_owner_pointer_db(tmp_path / "owner.db", {10: 2, 11: 3})
    module = _owner_revision()
    _run_revision(engine, module.upgrade)
    _run_revision(engine, module.downgrade)

    insp = sa.inspect(engine)
    assert "owner_entity_id" not in {c["name"] for c in insp.get_columns("entities")}
    assert not insp.get_check_constraints("entities")
    assert not insp.get_foreign_keys("entities")
    assert "uq_entity_managers_owner" in {i["name"] for i in insp.get_indexes("entity_managers")}
    with engine.connect() as conn:
        owners = conn.execute(sa.text(
            "SELECT project_entity_id, user_entity_id FROM entity_managers"
            " WHERE is_owner ORDER BY project_entity_id"
        )).all()
        assert [tuple(r) for r in owners] == [(10, 2), (11, 3)]
        assert conn.execute(sa.text("SELECT COUNT(*) FROM entity_managers")).scalar() == 6
    engine.dispose()


# ---------------------------------------------------------------------------
# f8a9b0c1d2e3: entity_managers.role
# ---------------------------------------------------------------------------

_ROLE_REVISION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations" / "versions" / "f8a9b0c1d2e3_entity_managers_role.py"
)


def _role_revision():
    spec = importlib.util.spec_from_file_location("member_role_revision", _ROLE_REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_pre_role_db(path):
    """entity_managers as e7f8a9b0c1d2 left it: users 1-3 are members of project 10, owned by user 1."""
    engine = sa.create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE entities (id INTEGER PRIMARY KEY, entity_type VARCHAR(8) NOT NULL,"
            " owner_entity_id INTEGER)"
        )
        conn.exec_driver_sql("INSERT INTO entities VALUES (10, 'project', 1)")
        conn.exec_driver_sql(
            """
            CREATE TABLE entity_managers (
                id INTEGER PRIMARY KEY,
                user_entity_id INTEGER NOT NULL,
                project_entity_id INTEGER NOT NULL,
                CONSTRAINT entity_managers_user_entity_id_project_entity_id_key
                    UNIQUE (user_entity_id, project_entity_id)
            )
            """
        )
        for uid in (1, 2, 3):
            conn.exec_driver_sql(
                f"INSERT INTO entity_managers (user_entity_id, project_entity_id) VALUES ({uid}, 10)"
            )
    return engine


def test_role_upgrade_makes_existing_rows_managers(tmp_path):
    engine = _make_pre_role_db(tmp_path / "role.db")
    _run_revision(engine, _role_revision().upgrade)

    insp = sa.inspect(engine)
    role = next(c for c in insp.get_columns("entity_managers") if c["name"] == "role")
    assert role["nullable"] is False
    assert "ck_entity_managers_role" in {c["name"] for c in insp.get_check_constraints("entity_managers")}

    with engine.connect() as conn:
        roles = conn.execute(sa.text("SELECT role FROM entity_managers")).scalars().all()
        assert roles == ["manager"] * 3
        # New rows default to manager; 'user' is allowed; anything else is rejected.
        conn.execute(sa.text("INSERT INTO entity_managers (user_entity_id, project_entity_id) VALUES (4, 10)"))
        conn.execute(sa.text(
            "INSERT INTO entity_managers (user_entity_id, project_entity_id, role) VALUES (5, 10, 'user')"
        ))
        assert conn.execute(sa.text("SELECT role FROM entity_managers WHERE user_entity_id = 4")).scalar() == "manager"
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text(
                "INSERT INTO entity_managers (user_entity_id, project_entity_id, role) VALUES (6, 10, 'owner')"
            ))
    engine.dispose()


def test_role_downgrade_drops_column_and_user_rows(tmp_path):
    engine = _make_pre_role_db(tmp_path / "role.db")
    module = _role_revision()
    _run_revision(engine, module.upgrade)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO entity_managers (user_entity_id, project_entity_id, role) VALUES (5, 10, 'user')"
        ))
        # The owner's own row is kept even as 'user': fk_entities_owner_membership
        # (RESTRICT) would reject deleting it on PostgreSQL.
        conn.execute(sa.text("UPDATE entity_managers SET role = 'user' WHERE user_entity_id = 1"))
    _run_revision(engine, module.downgrade)

    insp = sa.inspect(engine)
    assert "role" not in {c["name"] for c in insp.get_columns("entity_managers")}
    assert not insp.get_check_constraints("entity_managers")
    with engine.connect() as conn:
        users = conn.execute(sa.text("SELECT user_entity_id FROM entity_managers ORDER BY user_entity_id")).scalars().all()
        # The non-owner 'user' member is removed, not promoted; the owner stays.
        assert users == [1, 2, 3]
    engine.dispose()
