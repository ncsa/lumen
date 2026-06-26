#!/usr/bin/env python
"""Regenerate the help-doc screenshots in docs/img/ from a running dev instance.

Captures Chat, Models, Model detail, Clients, Client detail, and Profile with the
footer and skip-to-content link cropped out. The model-detail shot is taken as a
non-admin user so admin-only endpoint URLs are not exposed.

Prerequisites (see scripts/README.md):
  - `uv run dummy` (echo backend) running, or real model backends reachable
  - the app running with a dev config that sets `app.dev_user` (an admin):
        CONFIG_YAML=./dev.config.yaml uv run python -c \
          "from lumen import create_app; create_app().run(port=5001, debug=True, threaded=True)"
  - `uv run python -m playwright install chromium`

Then, with the SAME CONFIG_YAML:
    CONFIG_YAML=./dev.config.yaml uv run python scripts/screenshots.py

Env vars: BASE_URL (default http://localhost:5001), OUTPUT_DIR (default docs/img),
MODEL (default: first active model), CHROME_PATH (fallback browser executable).
"""
import os
import time

from sqlalchemy import select
from playwright.sync_api import sync_playwright

from lumen import create_app
from lumen.extensions import db
from lumen.models.entity import Entity
from lumen.models.model_config import ModelConfig
from lumen.services.crypto import hash_api_key
from lumen.models.api_key import APIKey

BASE = os.environ.get("BASE_URL", "http://localhost:5001").rstrip("/")
OUT = os.environ.get("OUTPUT_DIR", "docs/img")
DEMO_USER = "screenshot-demo@example.com"

HIDE_CSS = """
ilw-footer, footer, [slot='footer'] { display:none !important; }
"""


def ensure_demo_data(app):
    """Idempotently create a non-admin demo user (for the URL-free model-detail
    shot) and a demo client with a key, then return (model_name, user_cookie)."""
    with app.app_context():
        model = os.environ.get("MODEL")
        if not model:
            mc = db.session.execute(
                select(ModelConfig).where(ModelConfig.active).order_by(ModelConfig.model_name)
            ).scalars().first()
            model = mc.model_name if mc else ""

        user = db.session.execute(
            select(Entity).filter_by(email=DEMO_USER, entity_type="user")
        ).scalar_one_or_none()
        if not user:
            user = Entity(entity_type="user", email=DEMO_USER, name="Demo User",
                          initials="DU", active=True)
            db.session.add(user)
        user.model_access_default = "allowed"  # see every model on the detail page

        client = db.session.execute(
            select(Entity).filter_by(name="example-bot", entity_type="client")
        ).scalar_one_or_none()
        if not client:
            client = Entity(entity_type="client", name="example-bot", initials="EB", active=True)
            db.session.add(client)
            db.session.flush()
            key = "sk_example_0123456789abcdef"
            db.session.add(APIKey(entity_id=client.id, name="example-key",
                                  key_hash=hash_api_key(key),
                                  key_hint=f"{key[:7]}...{key[-4:]}", active=True))
        db.session.commit()

        cookie = app.session_interface.get_signing_serializer(app).dumps({
            "entity_id": user.id, "entity_name": user.name, "initials": user.initials,
            "entity_email": DEMO_USER, "gravatar_hash": ""})
        return model, cookie


def launch(pw):
    try:
        return pw.chromium.launch()
    except Exception:
        exe = os.environ.get("CHROME_PATH")
        if exe:
            return pw.chromium.launch(executable_path=exe)
        raise


def hide_chrome(page):
    page.add_style_tag(content=HIDE_CSS)
    page.evaluate("() => { const s = document.querySelector('skip-to-content'); if (s) s.remove(); }")
    page.wait_for_timeout(200)


def main():
    app = create_app()
    model, cookie = ensure_demo_data(app)
    os.makedirs(OUT, exist_ok=True)

    with sync_playwright() as pw:
        b = launch(pw)

        # ---- admin views ----
        actx = b.new_context(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
        page = actx.new_page()
        page.goto(BASE + "/devlogin", wait_until="networkidle")

        page.goto(BASE + "/chat", wait_until="networkidle")
        if model:
            try:
                page.select_option("#model-picker", label=model)
            except Exception:
                pass
        for msg in ["What is the capital of Illinois?",
                    "In two sentences, what is an AI gateway?"]:
            page.fill("#chat-input", msg)
            page.click("#send-btn")
            try:
                page.wait_for_function(
                    "!document.getElementById('send-btn').disabled", timeout=60000)
            except Exception:
                pass
            time.sleep(1.5)
        hide_chrome(page)
        page.screenshot(path=f"{OUT}/chat.png")
        print("chat.png")

        def shot(path, name, full=False):
            page.goto(BASE + path, wait_until="networkidle")
            page.wait_for_timeout(1200)
            hide_chrome(page)
            page.screenshot(path=f"{OUT}/{name}.png", full_page=full)
            print(f"{name}.png")

        shot("/models", "models")
        shot("/clients", "clients")
        page.goto(BASE + "/clients", wait_until="networkidle")
        href = page.eval_on_selector("a[href*='/clients/']", "e => e.getAttribute('href')")
        shot(href, "client-detail", full=True)
        shot("/profile", "profile", full=True)
        actx.close()

        # ---- non-admin view (no admin-only endpoint URLs) ----
        uctx = b.new_context(viewport={"width": 1280, "height": 760}, device_scale_factor=2)
        uctx.add_cookies([{"name": "session", "value": cookie, "domain": urlhost(), "path": "/"}])
        up = uctx.new_page()
        up.goto(f"{BASE}/models/{model}", wait_until="networkidle")
        up.wait_for_timeout(1500)
        hide_chrome(up)
        up.screenshot(path=f"{OUT}/model-detail.png")  # viewport only; HF READMEs are long
        print("model-detail.png (non-admin)")
        uctx.close()
        b.close()


def urlhost():
    from urllib.parse import urlparse
    return urlparse(BASE).hostname


if __name__ == "__main__":
    main()
