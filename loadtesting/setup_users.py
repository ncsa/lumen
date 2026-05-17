"""Create N load-test service accounts in Lumen, each with a unique API key
and 20 coins for the specified model.

Run from the project root (where config.yaml lives):
  uv run python loadtesting/setup_users.py 10
  uv run python loadtesting/setup_users.py 10 --model dummy --coins 20 --group staff

The generated API keys are printed to stdout and optionally written into
loadtesting/config.yaml (replacing the api_keys list).

Note: on macOS, use base_url: http://127.0.0.1:5001 in config.yaml — AirPlay Receiver
occupies localhost:5001 on Monterey+ and returns a bare 403.
"""
import argparse
import secrets
import sys
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser(description="Create Lumen load-test users")
    p.add_argument("count", type=int, help="Number of users to create")
    p.add_argument("--model", default="dummy", help="Model name to grant coins for (default: dummy)")
    p.add_argument("--coins", type=int, default=20, help="Coins to grant per user (default: 20)")
    p.add_argument("--prefix", default="loadtest", help="Entity name prefix (default: loadtest)")
    p.add_argument("--group", default=None, help="Add each entity to this group (e.g. staff) for model access")
    p.add_argument("--write-config", action="store_true", help="Update loadtesting/config.yaml with the new keys")
    return p.parse_args()


def main():
    args = parse_args()

    # Import after arg parsing so --help works without Flask deps
    from sqlalchemy import select

    from lumen import create_app
    from lumen.extensions import db
    from lumen.models.api_key import APIKey
    from lumen.models.entity import Entity
    from lumen.models.entity_balance import EntityBalance
    from lumen.models.entity_limit import EntityLimit
    from lumen.models.entity_model_access import EntityModelAccess
    from lumen.models.group import Group
    from lumen.models.group_member import GroupMember
    from lumen.models.model_config import ModelConfig
    from lumen.services.crypto import hash_api_key

    app = create_app()

    with app.app_context():
        model_config = db.session.execute(
            select(ModelConfig).filter_by(model_name=args.model, active=True)
        ).scalar_one_or_none()
        if model_config is None:
            print(f"ERROR: No active model named '{args.model}' found in the database.", file=sys.stderr)
            print("Make sure the model is in config.yaml and Lumen has been started at least once.", file=sys.stderr)
            sys.exit(1)

        group = None
        if args.group:
            group = db.session.execute(select(Group).filter_by(name=args.group)).scalar_one_or_none()
            if group is None:
                print(f"ERROR: No group named '{args.group}' found in the database.", file=sys.stderr)
                sys.exit(1)

        raw_keys = []
        for i in range(1, args.count + 1):
            name = f"{args.prefix}-{i}"

            entity = Entity(
                entity_type="client",
                name=name,
                initials=args.prefix[:4].upper(),
                active=True,
            )
            db.session.add(entity)
            db.session.flush()  # populate entity.id

            raw_key = "sk_" + secrets.token_urlsafe(32)
            api_key = APIKey(
                entity_id=entity.id,
                name=name,
                key_hash=hash_api_key(raw_key),
                key_hint=f"{raw_key[:7]}...{raw_key[-4:]}",
                active=True,
            )
            db.session.add(api_key)

            limit = EntityLimit(
                entity_id=entity.id,
                max_coins=args.coins,
                refresh_coins=0,
                starting_coins=args.coins,
                config_managed=False,
            )
            db.session.add(limit)

            balance = EntityBalance(
                entity_id=entity.id,
                coins_left=args.coins,
            )
            db.session.add(balance)

            access = EntityModelAccess(
                entity_id=entity.id,
                model_config_id=model_config.id,
                access_type="whitelist",
            )
            db.session.add(access)

            if group:
                db.session.add(GroupMember(group_id=group.id, entity_id=entity.id))

            raw_keys.append(raw_key)
            print(f"  {name}: {raw_key}")

        db.session.commit()

    group_note = f" in group '{args.group}'" if args.group else ""
    print(f"\nCreated {args.count} users with {args.coins:,} coins each for model '{args.model}'{group_note}.")

    if args.write_config:
        config_path = Path(__file__).parent / "config.yaml"
        import yaml

        config = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
        config["api_keys"] = raw_keys
        config["model"] = args.model
        config_path.write_text(yaml.dump(config, default_flow_style=False))
        print(f"Updated {config_path} with {len(raw_keys)} keys for model '{args.model}'.")
    else:
        print("\nPaste these keys into loadtesting/config.yaml under api_keys:,")
        print("or re-run with --write-config to update it automatically.")


if __name__ == "__main__":
    main()
