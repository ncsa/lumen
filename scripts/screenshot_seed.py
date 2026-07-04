#!/usr/bin/env python
"""Seed a Lumen database with a week of realistic usage plus a curated chat
conversation, so the help-doc screenshots (scripts/screenshots.py) show
populated charts, tables, and a genuine conversation instead of empty pages or
the echo backend mirroring the prompt.

Usage charts only render on PostgreSQL/TimescaleDB (the /api/usage/* endpoints
short-circuit to empty on SQLite), so run this against a Postgres instance. Use
the SAME CONFIG_YAML and DATABASE_URL as the app you capture from:

    DATABASE_URL=postgresql://user:pass@host:5432/lumen_shots \\
    CONFIG_YAML=./scripts/screenshot.config.yaml \\
    LUMEN_SECRET_KEY=dev LUMEN_ENCRYPTION_KEY=dev \\
    uv run python scripts/screenshot_seed.py

This script contains no secrets: DB credentials come from DATABASE_URL and app
secrets from LUMEN_SECRET_KEY / LUMEN_ENCRYPTION_KEY (or the config). Set MODEL
to the model the seeded conversation should use (defaults to the first active
model); it should match the MODEL passed to screenshots.py.
"""
import os
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, text

from lumen import create_app
from lumen.extensions import db
from lumen.models.entity import Entity
from lumen.models.model_config import ModelConfig
from lumen.models.entity_stat import EntityStat
from lumen.models.api_key import APIKey
from lumen.models.conversation import Conversation
from lumen.models.message import Message
from lumen.services.crypto import hash_api_key
from lumen.timeutils import utcnow

random.seed(42)  # deterministic-ish distribution across runs
DEV_EMAIL = os.environ.get("SEED_USER", "dev@example.com")

CHAT_TURNS = [
    ("user",
     "What is an AI gateway, and why would a university run its own instead "
     "of sending traffic straight to a commercial API?"),
    ("assistant",
     "An AI gateway is a single service that sits between your users and one or "
     "more large language model backends. It exposes an OpenAI-compatible API, so "
     "existing tools and libraries keep working unchanged, while the gateway "
     "handles authentication, spending limits, model routing, and logging in one "
     "place.\n\n"
     "Running your own keeps control on campus: you decide which models are "
     "available, enforce per-user and per-group budgets, keep prompt and usage "
     "data inside your own infrastructure, and mix commercial APIs with "
     "self-hosted models behind the same endpoint — with a single audit trail "
     "instead of scattered API keys billed to individual accounts."),
    ("user",
     "Can students and automated scripts share the same models through it?"),
    ("assistant",
     "Yes. People and automated tools are two kinds of accounts against the same "
     "catalog of models. Students sign in through the university identity provider "
     "and get a personal coin budget, while scripts and applications use a "
     "**Project** — a service account with its own API keys and coin pool, managed "
     "by one or more people.\n\n"
     "Both are subject to the same model-access rules, so you can allow a model for "
     "a research group's project while keeping it off the default student policy. "
     "Usage from every account flows into the same dashboards, so you can see "
     "exactly how much each person or project is spending."),
]
CHAT_META = {  # per assistant message: plausible token/latency metrics
    1: dict(input_tokens=34, output_tokens=168, time_to_first_token=0.41, duration=3.6, output_speed=46.7),
    3: dict(input_tokens=228, output_tokens=172, time_to_first_token=0.38, duration=3.4, output_speed=50.6),
}


def gen_logs(models, weights, entity_id, source, now, days=7, base_per_day=140):
    """Business-hours-weighted request_logs over the trailing `days`."""
    rows = []
    for d in range(days):
        day = now - timedelta(days=d)
        day_factor = 0.4 if day.weekday() >= 5 else 1.0  # weekends lighter
        n = int(base_per_day * day_factor * random.uniform(0.7, 1.3))
        for _ in range(n):
            hour = int(min(23, max(0, random.gauss(13, 4))))  # peak midday
            ts = day.replace(hour=hour, minute=random.randint(0, 59),
                             second=random.randint(0, 59), microsecond=0)
            if ts > now:
                ts = now - timedelta(minutes=random.randint(1, 120))
            mc = random.choices(models, weights=weights, k=1)[0]
            inp, out = random.randint(80, 2500), random.randint(40, 1800)
            rows.append({
                "time": ts, "entity_id": entity_id, "model_config_id": mc.id,
                "source": source, "input_tokens": inp, "output_tokens": out,
                "cost": Decimal(inp + out) * Decimal("0.0000012"),
                "audio_seconds": 0, "duration": round(random.uniform(0.3, 4.5), 3),
            })
    return rows


def main():
    app = create_app()
    with app.app_context():
        if db.engine.dialect.name != "postgresql":
            print("WARNING: not PostgreSQL — usage charts will render empty. "
                  "Seed a Postgres/TimescaleDB DB for populated usage screenshots.")

        # Dev admin user (devlogin reuses this by email) and a demo project + key.
        user = db.session.execute(
            select(Entity).filter_by(email=DEV_EMAIL, entity_type="user")
        ).scalar_one_or_none()
        if not user:
            user = Entity(entity_type="user", email=DEV_EMAIL, name="Dev User",
                          initials="DU", active=True)
            db.session.add(user)
            db.session.commit()

        proj = db.session.execute(
            select(Entity).filter_by(name="example-bot", entity_type="project")
        ).scalar_one_or_none()
        if not proj:
            proj = Entity(entity_type="project", name="example-bot", initials="EB", active=True)
            db.session.add(proj)
            db.session.flush()
            k = "sk_example_0123456789abcdef"
            db.session.add(APIKey(entity_id=proj.id, name="example-key",
                                  key_hash=hash_api_key(k),
                                  key_hint=f"{k[:7]}...{k[-4:]}", active=True))
            db.session.commit()

        models = db.session.execute(
            select(ModelConfig).where(ModelConfig.active).order_by(ModelConfig.model_name)
        ).scalars().all()
        if not models:
            raise SystemExit("No active models — sync a config with models first.")
        weights = list(range(len(models), 0, -1))  # first model most popular

        now = datetime.now(timezone.utc)
        ins = text("""
            INSERT INTO request_logs
                (time, entity_id, model_config_id, source, input_tokens,
                 output_tokens, cost, audio_seconds, duration)
            VALUES
                (:time, :entity_id, :model_config_id, :source, :input_tokens,
                 :output_tokens, :cost, :audio_seconds, :duration)
        """)
        for eid, src, per in [(user.id, "chat", 140), (proj.id, "api", 90)]:
            rows = gen_logs(models, weights, eid, src, now, base_per_day=per)
            db.session.execute(ins, rows)
            stat = db.session.get(EntityStat, eid)
            totals = dict(
                requests=len(rows),
                input_tokens=sum(r["input_tokens"] for r in rows),
                output_tokens=sum(r["output_tokens"] for r in rows),
                cost=sum((r["cost"] for r in rows), Decimal(0)),
                last_used_at=utcnow(),
            )
            if stat:
                for key, val in totals.items():
                    setattr(stat, key, val)
            else:
                db.session.add(EntityStat(entity_id=eid, audio_seconds=0, **totals))
            print(f"entity {eid} ({src}): {len(rows)} request_logs")
        db.session.commit()

        # Per-model and per-key counters, derived from the request_logs above,
        # so the detail/profile pages match the list/usage pages.
        db.session.execute(text("DELETE FROM model_stats"))
        db.session.execute(text("""
            INSERT INTO model_stats
                (entity_id, model_config_id, source, requests, input_tokens,
                 output_tokens, audio_seconds, cost, last_used_at)
            SELECT entity_id, model_config_id, source, COUNT(*), SUM(input_tokens),
                   SUM(output_tokens), 0, SUM(cost), MAX(time) AT TIME ZONE 'UTC'
            FROM request_logs
            WHERE entity_id IS NOT NULL AND model_config_id IS NOT NULL
            GROUP BY entity_id, model_config_id, source
        """))
        for key in db.session.execute(select(APIKey)).scalars().all():
            r = db.session.execute(text("""
                SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                       COALESCE(SUM(cost),0), MAX(time) AT TIME ZONE 'UTC'
                FROM request_logs WHERE entity_id = :eid AND source = 'api'
            """), {"eid": key.entity_id}).one()
            key.requests, key.input_tokens, key.output_tokens = int(r[0]), int(r[1]), int(r[2])
            key.cost, key.last_used_at = r[3], r[4]
        db.session.commit()

        # Curated conversation so chat.png shows a real exchange, not echo output.
        chat_model = os.environ.get("MODEL") or models[0].model_name
        if not db.session.execute(
            select(Conversation).filter_by(entity_id=user.id)
        ).scalars().first():
            conv = Conversation(entity_id=user.id, title="What is an AI gateway?",
                                model=chat_model,
                                created_at=now.replace(tzinfo=None) - timedelta(minutes=6),
                                updated_at=now.replace(tzinfo=None) - timedelta(minutes=4))
            db.session.add(conv)
            db.session.flush()
            for i, (role, content) in enumerate(CHAT_TURNS):
                m = Message(conversation_id=conv.id, role=role, content=content,
                            created_at=now.replace(tzinfo=None) - timedelta(minutes=6) + timedelta(seconds=30 * i))
                if role == "assistant":
                    for key, val in CHAT_META[i].items():
                        setattr(m, key, val)
                db.session.add(m)
            db.session.commit()
            print(f"seeded conversation for entity {user.id} using model '{chat_model}'")

        print("done")


if __name__ == "__main__":
    main()
