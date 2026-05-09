"""Seed analytics test data: 100 users + 6 months of random request_logs.

Usage:
    uv run python seed_analytics.py
"""

import random
from datetime import datetime, timedelta

from sqlalchemy import select

from lumen import create_app
from lumen.extensions import db
from lumen.models.entity import Entity
from lumen.models.model_config import ModelConfig
from lumen.models.request_log import RequestLog

MONTHS = 6
NUM_USERS = 100
NUM_REQUESTS = 5000

NOW = datetime.utcnow()
START = NOW - timedelta(days=MONTHS * 30)

SOURCES = ["chat", "api"]
MODELS = [
    {"name": "gpt-4o",        "input_cost": 5.0,  "output_cost": 15.0},
    {"name": "gpt-4o-mini",   "input_cost": 0.15, "output_cost": 0.6},
    {"name": "claude-3-opus", "input_cost": 15.0, "output_cost": 75.0},
    {"name": "llama3",        "input_cost": 0.0,  "output_cost": 0.0},
]


def random_time(lo: datetime, hi: datetime) -> datetime:
    delta = (hi - lo).total_seconds()
    return lo + timedelta(seconds=random.random() * delta)


def seed():
    app = create_app()
    with app.app_context():
        # ------------------------------------------------------------------ #
        # Ensure model_configs exist                                           #
        # ------------------------------------------------------------------ #
        model_ids = []
        for m in MODELS:
            cfg = db.session.execute(select(ModelConfig).filter_by(model_name=m["name"])).scalar_one_or_none()
            if cfg is None:
                cfg = ModelConfig(
                    model_name=m["name"],
                    input_cost_per_million=m["input_cost"],
                    output_cost_per_million=m["output_cost"],
                    active=True,
                )
                db.session.add(cfg)
                db.session.flush()
                print(f"  created model_config: {m['name']}")
            model_ids.append(cfg.id)
        db.session.commit()

        # ------------------------------------------------------------------ #
        # Insert 100 users spread over the past 6 months                      #
        # ------------------------------------------------------------------ #
        print(f"Inserting {NUM_USERS} test users...")
        user_ids = []
        for i in range(NUM_USERS):
            joined = random_time(START, NOW)
            email = f"seed-user-{i:03d}@example.com"
            entity = db.session.execute(select(Entity).filter_by(email=email)).scalar_one_or_none()
            if entity is None:
                entity = Entity(
                    entity_type="user",
                    email=email,
                    name=f"Seed User {i:03d}",
                    initials=f"S{i:03d}"[:4],
                    active=True,
                    created_at=joined,
                )
                db.session.add(entity)
                db.session.flush()
            user_ids.append(entity.id)
        db.session.commit()
        print(f"  done ({len(user_ids)} users)")

        # ------------------------------------------------------------------ #
        # Insert random request_logs                                           #
        # ------------------------------------------------------------------ #
        print(f"Inserting {NUM_REQUESTS} request_log rows...")

        # Requests are not uniformly distributed: more recent = busier
        # Achieved by weighting toward the end of the range.
        logs = []
        for _ in range(NUM_REQUESTS):
            # Bias toward recent dates using a square of a uniform random
            t = random.random() ** 0.6
            req_time = START + timedelta(seconds=t * (NOW - START).total_seconds())

            input_tokens = random.randint(100, 8000)
            output_tokens = random.randint(50, 2000)
            model_id = random.choice(model_ids)
            model_cfg = db.session.get(ModelConfig, model_id)
            cost = (
                input_tokens * float(model_cfg.input_cost_per_million) / 1_000_000
                + output_tokens * float(model_cfg.output_cost_per_million) / 1_000_000
            )

            logs.append(RequestLog(
                time=req_time,
                entity_id=random.choice(user_ids),
                model_config_id=model_id,
                model_endpoint_id=None,
                source=random.choice(SOURCES),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost=round(cost, 6),
                duration=round(random.uniform(0.3, 8.0), 3),
            ))

            if len(logs) % 500 == 0:
                db.session.add_all(logs)
                db.session.commit()
                print(f"  {len(logs)}/{NUM_REQUESTS}")
                logs = []

        if logs:
            db.session.add_all(logs)
            db.session.commit()

        print(f"  done ({NUM_REQUESTS} rows)")

        # ------------------------------------------------------------------ #
        # Refresh the continuous aggregate so analytics page shows data        #
        # Must run outside a transaction block (AUTOCOMMIT).                   #
        # ------------------------------------------------------------------ #
        print("Refreshing continuous aggregate...")
        with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(db.text(
                "CALL refresh_continuous_aggregate('request_counts_hourly', :start, :end)"
            ), {"start": START, "end": NOW + timedelta(hours=2)})
        print("  done")

        print("\nSeeding complete.")
        print(f"  Users:    {NUM_USERS}")
        print(f"  Requests: {NUM_REQUESTS}")
        print(f"  Period:   {START.date()} → {NOW.date()}")


if __name__ == "__main__":
    seed()
