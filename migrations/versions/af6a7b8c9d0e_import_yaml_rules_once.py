"""Import config.yaml group_rules once and heal rule-less auto-join groups

Revision ID: af6a7b8c9d0e
Revises: ae5f6a7b8c9d
Create Date: 2026-08-20 00:00:00.000000

The deprecated config.yaml group_rules section is imported here — in a
migration, exactly once — instead of at every startup. A startup import can
never know whether it already ran, so it kept resurrecting rules an admin had
deleted in the UI and re-enabling auto_join; a rename also desynced the group
from its name-keyed yaml entry, spawning a duplicate. Running the import as a
migration gives it the one-time semantics it always needed.

For each yaml rule-group: a missing group is created with its rules and
auto_join on; an existing group with no rules gets the rules attached and
auto_join enabled; a group that already has rules in the database is left
alone. Reads the file named by $CONFIG_YAML (default ./config.yaml); when the
file or section is absent nothing is imported and rules can be created in the
UI instead.

Finally, auto_join is cleared on any group left with zero rules — the ac3
carry-over (auto_join = config_managed) marked groups that never had yaml
rules (the old "ensure the group exists" idiom); those matched nobody, lost
members at each login, and refused manual membership edits.

Downgrade only reverts the heal (it cannot know which groups it healed, so it
is a no-op); imported rules are ordinary group_rules rows the operator can
edit or delete in the UI.
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "af6a7b8c9d0e"
down_revision = "ae5f6a7b8c9d"
branch_labels = None
depends_on = None

_RULE_MATCHES = {"contains", "equals"}


def _parse_yaml_rules(yaml_data):
    """{group name: [(field, match, value), ...]} from a config.yaml payload,
    dropping malformed entries (no field, no matcher, or empty value)."""
    out = {}
    for name, rules in (yaml_data.get("group_rules") or {}).items():
        rows = []
        for rule in rules or []:
            if not isinstance(rule, dict) or not rule.get("field"):
                continue
            if "contains" in rule:
                match, value = "contains", rule.get("contains")
            elif "equals" in rule:
                match, value = "equals", rule.get("equals")
            else:
                continue
            if value in (None, ""):
                continue
            rows.append((str(rule["field"]), match, str(value)))
        out[str(name)] = rows
    return out


def _import_group_rules(bind, yaml_data):
    """One-time import of yaml group_rules into the group_rules table."""
    for name, rows in _parse_yaml_rules(yaml_data).items():
        group = bind.execute(
            sa.text("SELECT id FROM groups WHERE name = :name"), {"name": name}
        ).first()
        if group is None:
            bind.execute(
                sa.text("INSERT INTO groups (name, active, auto_join) VALUES (:name, true, :auto)"),
                {"name": name, "auto": bool(rows)},
            )
            gid = bind.execute(
                sa.text("SELECT id FROM groups WHERE name = :name"), {"name": name}
            ).first()[0]
        else:
            gid = group[0]
            existing = bind.execute(
                sa.text("SELECT count(*) FROM group_rules WHERE group_id = :gid"), {"gid": gid}
            ).scalar()
            if existing:
                continue  # database rules win
        for field, match, value in rows:
            bind.execute(
                sa.text(
                    "INSERT INTO group_rules (group_id, field, match, value) "
                    "VALUES (:gid, :field, :match, :value)"
                ),
                {"gid": gid, "field": field, "match": match, "value": value},
            )
        if rows:
            bind.execute(
                sa.text("UPDATE groups SET auto_join = true WHERE id = :gid"), {"gid": gid}
            )


def upgrade():
    bind = op.get_bind()

    config_path = os.environ.get("CONFIG_YAML", "config.yaml")
    if os.path.exists(config_path):
        import yaml

        with open(config_path) as fh:
            yaml_data = yaml.safe_load(fh) or {}
        _import_group_rules(bind, yaml_data)

    # Heal: an auto-join group with no rules matches nobody (fail-closed),
    # drains its members at each login, and blocks manual membership edits.
    bind.execute(sa.text(
        "UPDATE groups SET auto_join = false "
        "WHERE auto_join = true AND id NOT IN (SELECT DISTINCT group_id FROM group_rules)"
    ))


def downgrade():
    # Imported rules are ordinary rows the operator manages in the UI, and
    # the heal cannot be selectively undone.
    pass
