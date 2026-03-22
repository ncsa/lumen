"""Create N load-test service accounts in Lumen, each with a unique API key
and 1,000,000 tokens for the specified model.

Run from the project root (where config.yaml lives):
  uv run python loadtesting/setup_users.py 10
  uv run python loadtesting/setup_users.py 10 --model dummy --tokens 1000000

The generated API keys are printed to stdout and optionally written into
loadtesting/config.yaml (replacing the api_keys list).
"""
import argparse
import secrets
import sys
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser(description="Create Lumen load-test users")
    p.add_argument("count", type=int, help="Number of users to create")
    p.add_argument("--model", default="dummy", help="Model name to grant tokens for (default: dummy)")
    p.add_argument("--tokens", type=int, default=1_000_000, help="Tokens to grant per user (default: 1000000)")
    p.add_argument("--prefix", default="loadtest", help="Entity name prefix (default: loadtest)")
    p.add_argument("--write-config", action="store_true", help="Update loadtesting/config.yaml with the new keys")
    return p.parse_args()


def main():
    args = parse_args()

    # Import after arg parsing so --help works without Flask deps
    from lumen import create_app
    from lumen.extensions import db
    from lumen.models.api_key import APIKey
    from lumen.models.entity import Entity
    from lumen.models.entity_model_balance import EntityModelBalance
    from lumen.models.entity_model_limit import EntityModelLimit
    from lumen.models.model_config import ModelConfig
    from lumen.services.crypto import hash_api_key

    app = create_app()

    with app.app_context():
        model_config = ModelConfig.query.filter_by(model_name=args.model, active=True).first()
        if model_config is None:
            print(f"ERROR: No active model named '{args.model}' found in the database.", file=sys.stderr)
            print("Make sure the model is in config.yaml and Lumen has been started at least once.", file=sys.stderr)
            sys.exit(1)

        raw_keys = []
        for i in range(1, args.count + 1):
            name = f"{args.prefix}-{i}"

            entity = Entity(
                entity_type="service",
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

            limit = EntityModelLimit(
                entity_id=entity.id,
                model_config_id=model_config.id,
                max_tokens=args.tokens,
                refresh_tokens=0,
                starting_tokens=args.tokens,
                config_managed=False,
            )
            db.session.add(limit)

            balance = EntityModelBalance(
                entity_id=entity.id,
                model_config_id=model_config.id,
                tokens_left=args.tokens,
            )
            db.session.add(balance)

            raw_keys.append(raw_key)
            print(f"  {name}: {raw_key}")

        db.session.commit()

    print(f"\nCreated {args.count} users with {args.tokens:,} tokens each for model '{args.model}'.")

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
